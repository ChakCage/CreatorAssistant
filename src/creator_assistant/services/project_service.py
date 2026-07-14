from __future__ import annotations

import shutil
import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from creator_assistant.domain.errors import AudioSeparatorRuntimeMissingError, DependencyMissingError, DiskSpaceError, JobCancelledError, ManualActionRequiredError, MediaRoleResolutionRequired, ValidationError, YouTubeMediaForbiddenError
from creator_assistant.domain.job import CancellationToken, JobState
from creator_assistant.domain.author_presets import AuthorPreset
from creator_assistant.domain.models import (
    FormatPlan,
    ProgressInfo,
    ProjectOptions,
    ProjectPaths,
    ProjectResult,
    VideoFormat,
    VideoMetadata,
)
from creator_assistant.domain.stages import JobStage
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.infrastructure.project_index import ProjectIndex, ProjectRoot
from creator_assistant.infrastructure.manifest_store import (
    MANIFEST_NAME,
    LegacyProjectScanner,
    ManifestLoader,
    ManifestMigrator,
    ManifestStatus,
    ManifestWriter,
    ProjectManifest,
    project_metadata_dir,
)
from creator_assistant.infrastructure.windows_paths import NamingTemplates, exact_directory_path, safe_file_name, sanitize_windows_component, unique_directory_path

from .ffmpeg_service import FfmpegService
from .format_selector import build_format_plan, is_reaper_compatible
from .legacy_media_inspector import LegacyProjectMediaInspector
from .media_validation_service import MediaValidationService
from .reaper_service import ReaperService
from .vegas_service import VegasService
from .stem_separation.base import StemSeparatorBackend
from .stem_separation.uvr_manual_fallback import UvrManualFallbackBackend
from .thumbnail_service import ThumbnailService
from .yt_dlp_service import YtDlpService
from .storage_service import JobTempManager, StorageService, classify_resource_failure, default_temp_root


ProgressCallback = Callable[[ProgressInfo], None]


class ProjectService:
    MANIFEST_NAME = MANIFEST_NAME

    def __init__(
        self,
        yt_dlp: YtDlpService,
        ffmpeg: FfmpegService,
        validator: MediaValidationService,
        thumbnail: ThumbnailService,
        reaper: ReaperService,
        direct_separator: StemSeparatorBackend,
        manual_separator: UvrManualFallbackBackend,
        job_store: JobStore,
        naming: NamingTemplates,
        initial_audio: str = "original",
        vegas: Optional[VegasService] = None,
        storage: Optional[StorageService] = None,
        project_index: Optional[ProjectIndex] = None,
        project_roots: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.yt_dlp = yt_dlp
        self.ffmpeg = ffmpeg
        self.validator = validator
        self.thumbnail = thumbnail
        self.reaper = reaper
        self.direct_separator = direct_separator
        self.manual_separator = manual_separator
        self.job_store = job_store
        self.naming = naming
        self.initial_audio = initial_audio
        self.vegas = vegas or VegasService()
        self.storage = storage or StorageService()
        self.project_index = project_index
        self.project_roots = project_roots or []
        self.manifest_loader = ManifestLoader()
        self.manifest_writer = ManifestWriter(self.manifest_loader)
        self.manifest_migrator = ManifestMigrator()
        self.last_scan_stats: Dict[str, int] = {}

    def planned_path(self, destination: Path, metadata: VideoMetadata) -> Path:
        return exact_directory_path(destination, metadata.title)

    def copy_path(self, destination: Path, metadata: VideoMetadata) -> Path:
        """Allocate a suffix only after the user explicitly selected a new copy."""
        return unique_directory_path(destination, metadata.title)

    def find_existing_projects(self, destination: Path, metadata: VideoMetadata) -> List[Dict[str, Any]]:
        """Read-only discovery ordered by strongest video-id evidence."""
        found: List[Dict[str, Any]] = []
        seen = set()

        def add(path: Path, source: str, confirmed: bool, record: Optional[Dict[str, Any]] = None) -> None:
            try:
                key = str(path.resolve()).casefold()
            except OSError:
                key = str(path).casefold()
            if key in seen or not path.is_dir():
                return
            seen.add(key)
            try:
                empty = not any(path.iterdir())
            except OSError:
                empty = False
            found.append({
                "path": path,
                "source": source,
                "confirmed": confirmed,
                "empty": empty,
                "status": str((record or {}).get("status", "unknown")),
                "stage": str((record or {}).get("stage", "")),
            })

        record = self.job_store.load(metadata.video_id)
        if isinstance(record, dict) and record.get("created_by") == "CreatorAssistant":
            recorded = Path(str(record.get("project_path", "")))
            if recorded.parent == destination:
                add(recorded, "job_store", True, record)
            for recorded_path in record.get("project_paths", []):
                historical = Path(str(recorded_path))
                if historical.parent == destination:
                    add(historical, "job_store_history", True, record)

        try:
            children = list(destination.iterdir()) if destination.is_dir() else []
        except OSError:
            children = []
        for child in children:
            manifest_path = child / self.MANIFEST_NAME
            if not child.is_dir():
                continue
            loaded = ManifestLoader().load(manifest_path)
            if loaded.status == ManifestStatus.MISSING:
                continue
            raw = loaded.raw if isinstance(loaded.raw, dict) else {}
            manifest_video_id = loaded.manifest.video_id if loaded.manifest else str(raw.get("video_id", ""))
            if manifest_video_id == metadata.video_id:
                add(child, f"manifest:{loaded.status.value}", True, raw)

        exact = self.planned_path(destination, metadata)
        exact_manifest = ManifestLoader().load(exact / self.MANIFEST_NAME)
        if exact.is_dir() and exact_manifest.status in {
            ManifestStatus.CORRUPTED, ManifestStatus.INCOMPLETE, ManifestStatus.UNSUPPORTED_VERSION
        }:
            add(exact, f"manifest:{exact_manifest.status.value}", True, exact_manifest.raw)
        else:
            add(exact, "title", False)
        return found

    def find_existing_projects_across(
        self,
        destinations: List[ProjectRoot],
        metadata: VideoMetadata,
        *,
        refresh_index: bool = False,
    ) -> List[Dict[str, Any]]:
        """Find a video by identity across configured roots; never scans the disk globally."""
        found: List[Dict[str, Any]] = []
        seen: set[str] = set()

        def configured_root(path: Path) -> Optional[ProjectRoot]:
            parent_key = str(path.parent).casefold()
            return next((item for item in destinations if str(item.path).casefold() == parent_key), None)

        def add(path: Path, source: str, confirmed: bool, extra: Optional[Dict[str, Any]] = None) -> None:
            key = str(path).casefold()
            root = configured_root(path)
            if key in seen or root is None or not path.is_dir():
                return
            seen.add(key)
            found.append({
                "path": path,
                "source": source,
                "confirmed": confirmed,
                "empty": False,
                "status": str((extra or {}).get("status", "unknown")),
                "stage": str((extra or {}).get("stage", "")),
                "preset_id": str((extra or {}).get("preset_id") or root.preset_id),
                "preset_name": str((extra or {}).get("display_name") or root.display_name),
                "root_path": str((extra or {}).get("root_path") or root.path),
            })

        record = self.job_store.load(metadata.video_id)
        if isinstance(record, dict) and record.get("created_by") == "CreatorAssistant":
            for value, source in [(record.get("project_path"), "job_store"), *[(item, "job_store_history") for item in record.get("project_paths", [])]]:
                if value:
                    add(Path(str(value)), source, True, record)

        index = getattr(self, "project_index", None)
        if index:
            if refresh_index:
                index.rescan(destinations)
            for item in index.lookup(metadata.video_id):
                if not item.get("stale"):
                    add(Path(str(item.get("project_path") or "")), "project_index", True, item)

        # A direct manifest pass makes the index a cache, never a source of truth.
        for root in destinations:
            for candidate in self.find_existing_projects(root.path, metadata):
                add(Path(candidate["path"]), str(candidate.get("source") or "manifest"), bool(candidate.get("confirmed")), candidate)
        return found

    def bind_existing(self, metadata: VideoMetadata, project_path: Path) -> None:
        """Compatibility wrapper; UI performs this operation in a dedicated worker."""
        self.migrate_existing(
            metadata, project_path, job_id="", author_preset=project_path.parent.parent.name,
            cancellation=CancellationToken(), on_progress=lambda _info: None,
        )

    def migrate_existing(
        self,
        metadata: VideoMetadata,
        project_path: Path,
        *,
        job_id: str,
        author_preset: str,
        cancellation: CancellationToken,
        on_progress: ProgressCallback,
        reaper_proxy_height: int = 720,
        preset_id: str = "",
    ):
        if not project_path.is_dir():
            raise ValidationError("Выбранная папка проекта не существует.")
        cancellation.raise_if_cancelled()
        on_progress(ProgressInfo("migration", "Чтение папки", 10.0))
        manifest_path = project_path / self.MANIFEST_NAME
        loader = getattr(self, "manifest_loader", None) or ManifestLoader()
        writer = getattr(self, "manifest_writer", None) or ManifestWriter(loader)
        migrator = getattr(self, "manifest_migrator", None) or ManifestMigrator()
        loaded = loader.load(manifest_path)
        cancellation.raise_if_cancelled()
        on_progress(ProgressInfo("migration", "Проверка manifest", 25.0))
        validator = getattr(self, "validator", None)
        scanner = LegacyProjectScanner(
            (lambda path: validator.probe(path, cancellation)) if validator and hasattr(validator, "probe") else None
        )
        on_progress(ProgressInfo("migration", "Проверка существующих материалов: быстрый индекс", 40.0))
        discovered = scanner.scan(project_path)
        self.last_scan_stats = dict(scanner.stats)
        on_progress(ProgressInfo(
            "migration",
            "Проверка существующих материалов: "
            f"файлов {scanner.stats.get('allowed_files', 0)}, "
            f"FFprobe {scanner.stats.get('ffprobe_calls', 0)}, "
            f"sidecar пропущено {scanner.stats.get('ignored_sidecar_files', 0)}",
            65.0,
        ))
        cancellation.raise_if_cancelled()
        on_progress(ProgressInfo("migration", "Сохранение manifest", 80.0))
        manifest = migrator.migrate(
            loaded,
            video_id=metadata.video_id,
            source_url=metadata.webpage_url,
            title=metadata.title,
            project_path=project_path,
            author_preset=author_preset,
            job_id=job_id,
            discovered_files=discovered,
            reaper_proxy_height=reaper_proxy_height,
            preset_id=preset_id,
            channel_id=metadata.channel_id,
            uploader_id=metadata.uploader_id,
            channel_name=metadata.channel or metadata.uploader,
        )
        saved = writer.write(manifest_path, manifest, backup_existing=True)
        self._write_scan_index(project_path, discovered, scanner.stats)
        record = self.job_store.load(metadata.video_id) or {}
        previous_path = str(record.get("project_path", ""))
        record.update({
            "created_by": "CreatorAssistant",
            "video_id": metadata.video_id,
            "url": metadata.webpage_url,
            "title": metadata.title,
            "project_path": str(project_path),
            "materials_path": str(project_path / "Материалы"),
            "author_path": str(project_path.parent),
            "status": record.get("status") or "bound",
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        history = [str(value) for value in record.get("project_paths", []) if value]
        if previous_path and previous_path not in history:
            history.append(previous_path)
        if str(project_path) not in history:
            history.append(str(project_path))
        record["project_paths"] = history
        self.job_store.save(metadata.video_id, record)
        self._record_in_index(project_path, metadata, preset_id=preset_id, display_name=author_preset)
        on_progress(ProgressInfo("migration", "Готово к продолжению", 100.0))
        return {
            "manifest": saved,
            "discovered_files": discovered,
            "project_path": project_path,
            "scan_stats": dict(scanner.stats),
        }

    @staticmethod
    def _write_scan_index(project_path: Path, discovered, stats: Dict[str, int]) -> None:
        target = project_metadata_dir(project_path) / "scan_index.json"
        payload = {
            "schema_version": 1,
            "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "stats": dict(stats),
            "files": [],
        }
        for item in discovered:
            path = Path(item.path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            payload["files"].append({
                "path": item.path,
                "classification": item.classification,
                "size": item.size,
                "mtime": mtime,
            })
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(target))

    @staticmethod
    def _update_scan_index_file(
        project_path: Path,
        path: Path,
        classification: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        target = project_metadata_dir(project_path) / "scan_index.json"
        try:
            payload = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        files = payload.get("files") if isinstance(payload.get("files"), list) else []
        normalized = str(path.resolve()).casefold()
        files = [
            item for item in files
            if not isinstance(item, dict) or str(Path(str(item.get("path") or "")).resolve()).casefold() != normalized
        ]
        stat = path.stat()
        files.append({
            "path": str(path),
            "classification": classification,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "details": details or {},
        })
        payload.update({
            "schema_version": 1,
            "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "files": files,
        })
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(target))

    @staticmethod
    def clear_media_index(project_path: Path) -> int:
        removed = 0
        metadata_dir = project_metadata_dir(project_path)
        for name in ("scan_index.json", "media_probe_cache.json"):
            path = metadata_dir / name
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    def _write_manifest(
        self,
        project_path: Path,
        metadata: VideoMetadata,
        status: str,
        proxy_height: int = 720,
        files: Optional[Dict[str, Any]] = None,
        stages: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not project_path.is_dir():
            return
        target = project_path / self.MANIFEST_NAME
        loaded = self.manifest_loader.load(target)
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        if loaded.status == ManifestStatus.VALID and loaded.manifest:
            manifest = loaded.manifest
            manifest.state = status.upper()
            manifest.updated_at = now
            manifest.reaper_proxy_height = proxy_height if proxy_height in {480, 720, 1080} else 720
            manifest.channel_id = manifest.channel_id or metadata.channel_id
            manifest.uploader_id = manifest.uploader_id or metadata.uploader_id
            manifest.channel_name = manifest.channel_name or metadata.channel or metadata.uploader
            preset_id, display_name = self._preset_identity(project_path.parent)
            manifest.preset_id = manifest.preset_id or preset_id
            manifest.author_preset = manifest.author_preset or display_name
        else:
            preset_id, display_name = self._preset_identity(project_path.parent)
            manifest = ProjectManifest(
                video_id=metadata.video_id,
                source_url=metadata.webpage_url,
                title=metadata.title,
                project_path=str(project_path),
                materials_path=str(project_path / "Материалы"),
                author_preset=display_name,
                preset_id=preset_id,
                channel_id=metadata.channel_id,
                uploader_id=metadata.uploader_id,
                channel_name=metadata.channel or metadata.uploader,
                state=status.upper(),
                created_at=now,
                updated_at=now,
                reaper_proxy_height=proxy_height if proxy_height in {480, 720, 1080} else 720,
            )
        if stages is not None:
            manifest.stages = stages
        if files is not None:
            manifest.files = self._manifest_files_from_record(files, stages or manifest.stages)
        manifest.reaper_proxies = self._merge_proxy_variants(
            manifest.reaper_proxies,
            stages or manifest.stages,
        )
        # Legacy/existing manifests are backed up by the explicit migration step.
        # Lifecycle checkpoints must not overwrite that original backup.
        self.manifest_writer.write(target, manifest, backup_existing=False)
        self._record_in_index(project_path, metadata, preset_id=manifest.preset_id, display_name=manifest.author_preset)

    @staticmethod
    def _merge_proxy_variants(existing: Dict[str, Any], stages: Dict[str, Any]) -> Dict[str, Any]:
        variants = dict(existing) if isinstance(existing, dict) else {}
        info = stages.get("proxy_variants", {}) if isinstance(stages, dict) else {}
        discovered = info.get("variants", {}) if isinstance(info, dict) else {}
        if isinstance(discovered, dict):
            for key, value in discovered.items():
                if isinstance(value, dict) and value.get("path"):
                    variants[str(key)] = dict(value)
        proxy = stages.get("proxy", {}) if isinstance(stages, dict) else {}
        if isinstance(proxy, dict) and proxy.get("status") == "VALID" and proxy.get("path"):
            height = proxy.get("effective_height") or proxy.get("height")
            if str(height) in {"480", "720", "1080"}:
                variants[str(height)] = dict(proxy)
        return variants

    @staticmethod
    def _manifest_files_from_record(files: Dict[str, Any], stages: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        role_map = {
            "maximum": "MAX_VIDEO",
            "proxy": "REAPER_PROXY",
            "audio": "ORIGINAL_AUDIO",
            "instrumental": "INSTRUMENTAL",
            "reaper": "REAPER_PROJECT",
            "vegas": "VEGAS_PROJECT",
            "thumbnail": "THUMBNAIL",
        }
        for key, value in files.items():
            path = str(value)
            details = stages.get(key, {}) if isinstance(stages, dict) else {}
            entry = {
                "path": path,
                "role": role_map.get(key, key.upper()),
                "source": "job_record",
            }
            if isinstance(details, dict):
                for field in (
                    "size", "confidence", "source", "validated_at", "probe",
                    "confirmed_by_user", "duration", "codec",
                    "video_path", "instrumental_path", "created_by_creator_assistant",
                    "created_at", "vegas_version", "video_only_from_max",
                    "video_track_count", "audio_track_count", "video_events", "audio_events",
                    "max_audio_events", "video_start_nanos", "audio_start_nanos",
                    "width", "height", "fps",
                ):
                    if details.get(field) is not None:
                        entry[field] = details[field]
            result[key] = entry
        return result

    def _preset_identity(self, root_path: Path) -> tuple[str, str]:
        key = str(root_path).casefold()
        for raw in getattr(self, "project_roots", []):
            if isinstance(raw, dict) and str(raw.get("root_path") or "").casefold() == key:
                preset = AuthorPreset.from_dict(raw)
                return preset.preset_id, preset.display_name
        preset = AuthorPreset.from_dict({"root_path": str(root_path)})
        return preset.preset_id, preset.display_name

    def _record_in_index(self, project_path: Path, metadata: VideoMetadata, *, preset_id: str = "", display_name: str = "") -> None:
        index = getattr(self, "project_index", None)
        if not index:
            return
        if not preset_id or not display_name:
            resolved_id, resolved_name = self._preset_identity(project_path.parent)
            preset_id = preset_id or resolved_id
            display_name = display_name or resolved_name
        index.record(
            video_id=metadata.video_id,
            title=metadata.title,
            project_path=project_path,
            root_path=project_path.parent,
            preset_id=preset_id,
            display_name=display_name,
        )

    def clear_job_auth(self, video_id: str) -> None:
        record = self.job_store.load(video_id)
        if not isinstance(record, dict):
            return
        record["auth_mode"] = "anonymous"
        record["youtube_auth"] = {
            "enabled": False,
            "mode": "anonymous",
            "browser": "",
            "browser_profile": "",
            "cookies_file": "",
            "user_consented": False,
        }
        record["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        self.job_store.save(video_id, record)

    def resumable_path(self, metadata: VideoMetadata) -> Optional[Path]:
        return self.job_store.resumable_path(metadata.video_id)

    @staticmethod
    def selected_output_roles(options: ProjectOptions) -> set[str]:
        roles: set[str] = set()
        if options.download_maximum:
            roles.add("maximum")
        if options.create_proxy:
            roles.add("proxy")
        if options.download_audio:
            roles.add("audio")
        if options.create_instrumental:
            roles.add("instrumental")
        if options.create_reaper_project:
            roles.add("reaper")
        if options.create_vegas_project:
            roles.add("vegas")
        return roles

    @classmethod
    def required_roles(
        cls,
        options: ProjectOptions,
        states: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> set[str]:
        """Resolve inputs without changing the user's selected output checkboxes."""
        roles = cls.selected_output_roles(options)
        if options.create_reaper_project:
            roles.update(("proxy", "instrumental"))
        if options.create_vegas_project:
            roles.update(("maximum", "instrumental"))
        if "instrumental" in roles and states is not None:
            if states.get("instrumental", {}).get("status") != "VALID":
                roles.add("audio")
        return roles

    @classmethod
    def plan_status(cls, options: ProjectOptions, states: Dict[str, Dict[str, Any]]) -> str:
        selected = cls.selected_output_roles(options)
        required = cls.required_roles(options, states)
        if selected and all(states.get(role, {}).get("status") == "VALID" for role in required):
            return "ALREADY_COMPLETE"
        return "ACTION_REQUIRED"

    @staticmethod
    def effective_proxy_height(plan: FormatPlan, requested_height: int) -> int:
        requested = requested_height if requested_height in {480, 720, 1080} else 720
        source_height = int(plan.maximum_video.height or requested)
        return min(requested, source_height)

    def inspect_existing(
        self,
        metadata: VideoMetadata,
        options: ProjectOptions,
        project_path: Optional[Path] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> Dict[str, Dict[str, Any]]:
        project_path = project_path or self.resumable_path(metadata)
        if not project_path:
            return {}
        paths = ProjectPaths(project_path, project_path / "Материалы", project_path.name)
        required = self.required_roles(options)
        plan = build_format_plan(metadata.formats, options.reaper_proxy_height)
        effective_proxy_height = self.effective_proxy_height(plan, options.reaper_proxy_height)
        inspection_roles = set(required)
        if "proxy" in required:
            inspection_roles.add("maximum")
        states: Dict[str, Dict[str, Any]] = {
            key: {"status": "NOT_REQUIRED"}
            for key in ("maximum", "proxy", "audio", "instrumental", "reaper", "vegas")
        }
        loaded_manifest = self.manifest_loader.load(paths.root / self.MANIFEST_NAME)
        assigned_paths: Dict[str, Path] = {}
        if loaded_manifest.manifest:
            for key, entry in loaded_manifest.manifest.files.items():
                if key in states and isinstance(entry, dict) and entry.get("path"):
                    assigned_paths[key] = Path(str(entry["path"]))
            variant = loaded_manifest.manifest.reaper_proxies.get(str(effective_proxy_height), {})
            if isinstance(variant, dict) and variant.get("path"):
                assigned_paths["proxy"] = Path(str(variant["path"]))

        def valid_project_file(path: Optional[Path]) -> Optional[Path]:
            if not path or not path.is_file() or path.stat().st_size <= 0:
                return None
            try:
                path.resolve().relative_to(paths.root.resolve())
            except (OSError, ValueError):
                return None
            return path

        def inspect_media(key: str, template: Path, validator) -> None:
            candidate = valid_project_file(assigned_paths.get(key)) or YtDlpService.find_created_file(template)
            partials = self._partial_files(template)
            if not candidate:
                states[key] = {"status": "PARTIAL" if partials else "MISSING", "partials": [str(path) for path in partials]}
                return
            try:
                probe = validator(candidate)
                states[key] = {
                    "status": "VALID",
                    "path": str(candidate),
                    "size": candidate.stat().st_size,
                    "probe": probe,
                }
                if key == "proxy":
                    video = next(
                        (item for item in probe.get("streams", []) if item.get("codec_type") == "video"),
                        {},
                    )
                    states[key].update({
                        "requested_height": options.reaper_proxy_height,
                        "effective_height": effective_proxy_height,
                        "width": int(video.get("width") or 0),
                        "height": int(video.get("height") or 0),
                        "fps": self._rate_value(video.get("avg_frame_rate") or video.get("r_frame_rate")),
                        "source": "manifest_profile" if assigned_paths.get("proxy") == candidate else "expected_name",
                    })
            except JobCancelledError:
                raise
            except Exception as exc:
                states[key] = {"status": "INVALID", "path": str(candidate), "size": candidate.stat().st_size, "reason": str(exc)}

        max_name = safe_file_name(paths.base_name, self.naming.maximum, "%(ext)s", height=plan.maximum_video.height or 0)
        max_name = max_name[: -len(".%(ext)s")] + ".%(ext)s"
        if "maximum" in inspection_roles:
            inspect_media(
                "maximum",
                paths.materials / max_name,
                lambda path: self.validator.validate_expected_video(
                    path,
                    cancellation,
                    duration=metadata.duration,
                    height=plan.maximum_video.height,
                    fps=plan.maximum_video.fps,
                    require_audio=True,
                    require_sdr=True,
                ),
            )
        proxy_name = safe_file_name(
            paths.base_name, self.naming.proxy, "mp4", proxy_height=effective_proxy_height
        )
        def validate_proxy(path: Path) -> Dict[str, Any]:
            probe = self.validator.validate_expected_video(
                path,
                cancellation,
                duration=metadata.duration,
                fps=plan.proxy_video.fps or plan.maximum_video.fps,
                require_audio=True,
                height=effective_proxy_height,
                require_sdr=True,
            )
            video = next(item for item in probe.get("streams", []) if item.get("codec_type") == "video")
            if int(video.get("height") or 0) > int(video.get("width") or 0):
                raise ValidationError("Вертикальное видео не подходит как REAPER proxy.")
            return probe
        if "proxy" in required:
            inspect_media(
                "proxy",
                paths.materials / (Path(proxy_name).stem + ".%(ext)s"),
                validate_proxy,
            )
        audio_name = safe_file_name(paths.base_name, self.naming.audio, "%(ext)s")
        audio_name = audio_name[: -len(".%(ext)s")] + ".%(ext)s"
        if "audio" in required:
            inspect_media(
                "audio",
                paths.materials / audio_name,
                lambda path: self.validator.validate_expected_audio(path, cancellation, duration=metadata.duration),
            )
        instrumental_name = safe_file_name(paths.base_name, self.naming.instrumental, "flac")
        if "instrumental" in required:
            inspect_media(
                "instrumental",
                paths.materials / (Path(instrumental_name).stem + ".%(ext)s"),
                lambda path: self.validator.validate_expected_audio(path, cancellation, duration=metadata.duration, require_flac=True),
            )
        for key, details in self._legacy_media_states(
            metadata, plan, paths.root, options, cancellation, inspection_roles, assigned_paths,
            proxy_height=effective_proxy_height,
        ).items():
            if states.get(key, {}).get("status") != "VALID":
                states[key] = details
        proxy_variants = dict(getattr(self, "last_proxy_variants", {}))
        if states.get("proxy", {}).get("status") == "VALID":
            proxy_variants[str(effective_proxy_height)] = dict(states["proxy"])
        states["proxy_variants"] = {
            "status": "INFO",
            "requested_height": options.reaper_proxy_height,
            "effective_height": effective_proxy_height,
            "variants": proxy_variants,
        }
        instrumental_status = states.get("instrumental", {}).get("status")
        if (
            "instrumental" in required
            and "audio" not in required
            and instrumental_status in {"MISSING", "INVALID", "PARTIAL", "NOT_REQUIRED"}
        ):
            required.add("audio")
            inspect_media(
                "audio",
                paths.materials / audio_name,
                lambda path: self.validator.validate_expected_audio(
                    path, cancellation, duration=metadata.duration
                ),
            )
            for key, details in self._legacy_media_states(
                metadata, plan, paths.root, options, cancellation, {"audio"}, assigned_paths
            ).items():
                if states.get(key, {}).get("status") != "VALID":
                    states[key] = details
        preview_candidates = [path for path in paths.root.glob(self.naming.preview + ".*") if path.is_file() and path.stat().st_size > 0]
        states["thumbnail"] = ({"status": "VALID", "path": str(preview_candidates[0]), "size": preview_candidates[0].stat().st_size} if preview_candidates else {"status": "MISSING"})
        if "reaper" in required:
            proxy = Path(states["proxy"]["path"]) if states["proxy"].get("status") == "VALID" else None
            instrumental = Path(states["instrumental"]["path"]) if states["instrumental"].get("status") == "VALID" else None
            default_rpp = paths.root / safe_file_name(paths.base_name, "{title}", "rpp")
            profile_rpp = paths.root / safe_file_name(
                paths.base_name, f"{{title}} [{effective_proxy_height}p]", "rpp"
            )
            rpp_candidates = []
            for candidate in (assigned_paths.get("reaper"), profile_rpp, default_rpp):
                valid = valid_project_file(candidate)
                if valid and valid not in rpp_candidates:
                    rpp_candidates.append(valid)
            rpp = next(
                (
                    candidate for candidate in rpp_candidates
                    if proxy and instrumental and self.reaper.validate_project(candidate, proxy, instrumental)
                ),
                None,
            )
            if rpp:
                states["reaper"] = {
                    "status": "VALID", "path": str(rpp), "size": rpp.stat().st_size,
                    "proxy_path": str(proxy), "proxy_height": effective_proxy_height,
                }
            elif rpp_candidates:
                existing_rpp = rpp_candidates[0]
                states["reaper"] = {
                    "status": "INVALID", "path": str(existing_rpp), "size": existing_rpp.stat().st_size,
                    "reason": f"Проект REAPER существует, но использует другой proxy вместо {effective_proxy_height}p.",
                }
            else:
                states["reaper"] = {"status": "MISSING"}
        if "vegas" in required:
            expected_veg = self.vegas.project_path(paths.root, paths.base_name)
            root_vegas = [
                item for item in paths.root.glob("*.veg")
                if valid_project_file(item) is not None
            ]
            veg = (
                valid_project_file(assigned_paths.get("vegas"))
                or valid_project_file(expected_veg)
                or (root_vegas[0] if len(root_vegas) == 1 else expected_veg)
            )
            if veg.is_file() and veg.stat().st_size > 0:
                states["vegas"] = {
                    "status": "VALID",
                    "path": str(veg),
                    "size": veg.stat().st_size,
                    "source": "existing_legacy_project",
                    "confirmed_by_user": True,
                }
            elif veg.is_file():
                states["vegas"] = {"status": "INVALID", "path": str(veg), "size": veg.stat().st_size, "reason": "VEGAS project file is empty."}
            else:
                states["vegas"] = {"status": "MISSING"}
        return states

    def _legacy_media_states(
        self,
        metadata: VideoMetadata,
        plan: FormatPlan,
        project_path: Path,
        options: ProjectOptions,
        cancellation: Optional[CancellationToken],
        required: set[str],
        assigned_paths: Dict[str, Path],
        proxy_height: Optional[int] = None,
    ) -> Dict[str, Dict[str, Any]]:
        if not getattr(self.validator, "probe", None):
            return {}

        def probe(path: Path, token: Optional[CancellationToken]) -> Dict[str, Any]:
            return self.validator.probe(path, token)

        inspector = LegacyProjectMediaInspector(
            probe,
            cache_path=project_metadata_dir(project_path) / "media_probe_cache.json",
        )
        matches = inspector.inspect(
            project_path,
            metadata,
            plan,
            proxy_height=proxy_height or options.reaper_proxy_height,
            cancellation=cancellation,
            required_roles=required & {"maximum", "proxy", "audio", "instrumental"},
            assigned_paths=assigned_paths,
        )
        self.last_scan_stats = dict(inspector.stats)
        self.last_proxy_variants = dict(inspector.proxy_variants)
        return {
            key: match.to_state()
            for key, match in matches.items()
            if match.status not in {"NOT_MATCHED", "NOT_REQUIRED"}
        }

    def reconcile_existing(
        self,
        metadata: VideoMetadata,
        options: ProjectOptions,
        project_path: Path,
        cancellation: Optional[CancellationToken] = None,
    ) -> Dict[str, Dict[str, Any]]:
        states = self.inspect_existing(metadata, options, project_path, cancellation)
        record = self.job_store.load(metadata.video_id) or {}
        files = {
            key: str(details["path"])
            for key, details in states.items()
            if details.get("status") == "VALID" and details.get("path")
        }
        record.update(
            {
                "created_by": "CreatorAssistant",
                "video_id": metadata.video_id,
                "url": metadata.webpage_url,
                "title": metadata.title,
                "project_path": str(project_path),
                "materials_path": str(project_path / "Материалы"),
                "author_path": str(project_path.parent),
                "created_at": record.get("created_at") or dt.datetime.now().isoformat(timespec="seconds"),
                "expected_duration": metadata.duration,
                "expected_height": build_format_plan(metadata.formats).maximum_video.height,
                "expected_fps": build_format_plan(metadata.formats).maximum_video.fps,
                "files": files,
                "stages": states,
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        if any(
            details.get("status") in {"AMBIGUOUS", "CONFIRMATION_REQUIRED"}
            for details in states.values()
        ):
            record["status"] = "waiting_for_media_selection"
        elif record.get("status") not in {"completed", "cancelled"}:
            record["status"] = "bound"
        self.job_store.save(metadata.video_id, record)
        self._write_manifest(
            project_path,
            metadata,
            str(record.get("status", "failed")),
            options.reaper_proxy_height,
            record.get("files", {}),
            record.get("stages", {}),
        )
        return states

    def resolve_media_role(
        self,
        metadata: VideoMetadata,
        project_path: Path,
        role: str,
        *,
        selected_path: Optional[Path] = None,
        action: str = "select",
        cancellation: Optional[CancellationToken] = None,
    ) -> Dict[str, Any]:
        if role not in {"maximum", "proxy", "audio", "instrumental"}:
            raise ValidationError("Неизвестная роль существующего материала: " + role)
        record = self.job_store.load(metadata.video_id) or {}
        stages = dict(record.get("stages", {})) if isinstance(record.get("stages"), dict) else {}
        files = dict(record.get("files", {})) if isinstance(record.get("files"), dict) else {}
        normalized_action = action.casefold()
        if normalized_action in {"skip"}:
            state = {"status": "NOT_REQUIRED", "source": "manual_legacy_resolution", "confirmed_by_user": True}
            files.pop(role, None)
        elif normalized_action in {"download", "download_new"}:
            state = {"status": "MISSING", "source": "manual_download_requested", "confirmed_by_user": True}
            files.pop(role, None)
        else:
            if not selected_path or not selected_path.is_file():
                raise ValidationError("Выбранный файл не существует.")
            token = cancellation or CancellationToken()
            if role in {"maximum", "proxy"}:
                probe = self.validator.validate_video(selected_path, token)
            else:
                probe = self.validator.validate_expected_audio(
                    selected_path,
                    token,
                    duration=metadata.duration,
                    require_flac=role == "instrumental",
                )
            state = {
                "status": "VALID",
                "path": str(selected_path),
                "size": selected_path.stat().st_size,
                "source": "manual_legacy_resolution",
                "confirmed_by_user": True,
                "probe": probe,
                "validated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            streams = probe.get("streams", []) if isinstance(probe, dict) else []
            audio_stream = next(
                (item for item in streams if item.get("codec_type") == "audio"), {}
            )
            fmt = probe.get("format", {}) if isinstance(probe, dict) else {}
            try:
                state["duration"] = float(fmt.get("duration") or audio_stream.get("duration") or 0)
            except (TypeError, ValueError):
                state["duration"] = 0.0
            state["codec"] = str(audio_stream.get("codec_name") or "")
            files[role] = str(selected_path)
        stages[role] = state
        record.update({
            "created_by": "CreatorAssistant",
            "video_id": metadata.video_id,
            "url": metadata.webpage_url,
            "title": metadata.title,
            "project_path": str(project_path),
            "materials_path": str(project_path / "Материалы"),
            "files": files,
            "stages": stages,
            "status": "bound",
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        self.job_store.save(metadata.video_id, record)
        self._write_manifest(project_path, metadata, "bound", files=files, stages=stages)
        return state

    @staticmethod
    def _partial_files(template: Path) -> List[Path]:
        base = template.name.replace(".%(ext)s", "").replace("%(ext)s", "").rstrip(".").casefold()
        try:
            return [path for path in template.parent.iterdir() if path.is_file() and path.name.casefold().startswith(base) and path.name.casefold().endswith((".part", ".ytdl"))]
        except OSError:
            return []

    @staticmethod
    def _non_destructive_target(target: Path) -> Path:
        if not target.exists():
            return target
        for index in range(2, 1000):
            candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
            if not candidate.exists():
                return candidate
        raise ValidationError("Не удалось подобрать безопасное имя для нового proxy-файла.")

    @staticmethod
    def _rate_value(value: Any) -> Optional[float]:
        try:
            text = str(value)
            if "/" in text:
                numerator, denominator = text.split("/", 1)
                return float(numerator) / float(denominator) if float(denominator) else None
            return float(text)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @staticmethod
    def _blocked_existing_stage(record: Dict[str, Any], key: str) -> Optional[str]:
        details = record.get("stages", {}).get(key, {}) if isinstance(record.get("stages"), dict) else {}
        if not isinstance(details, dict):
            return None
        status = str(details.get("status") or "")
        if status in {"AMBIGUOUS", "CONFIRMATION_REQUIRED"}:
            return "Existing content-compatible file for " + key + " requires confirmation before continuing."
        if status == "INVALID":
            return "Existing file for " + key + " did not pass validation and will not be overwritten: " + str(details.get("reason", "unknown reason"))
        return None

    def dry_run(self, destination: Path, metadata: VideoMetadata, options: ProjectOptions) -> ProjectResult:
        plan = build_format_plan(metadata.formats, options.reaper_proxy_height)
        root = self.planned_path(destination, metadata)
        paths = ProjectPaths(root, root / "Материалы", root.name)
        return ProjectResult(None, self._plan_lines(metadata, paths, plan, options))

    def execute(
        self,
        destination: Path,
        metadata: VideoMetadata,
        options: ProjectOptions,
        cancellation: CancellationToken,
        on_progress: ProgressCallback,
        resume_path: Optional[Path] = None,
        new_project_path: Optional[Path] = None,
    ) -> ProjectResult:
        if options.dry_run:
            return self.dry_run(destination, metadata, options)
        plan = build_format_plan(metadata.formats, options.reaper_proxy_height)
        source_height = int(plan.maximum_video.height or options.reaper_proxy_height)
        effective_proxy_height = self.effective_proxy_height(plan, options.reaper_proxy_height)
        state = JobState()
        state.start()
        files: Dict[str, Path] = {}
        resumed = bool(resume_path)
        if resume_path:
            paths = ProjectPaths(resume_path, resume_path / "Материалы", resume_path.name)
            if not paths.materials.is_dir():
                manifest_path = paths.root / self.MANIFEST_NAME
                loaded_manifest = self.manifest_loader.load(manifest_path)
                legacy_files = []
                try:
                    legacy_files = [item for item in paths.root.iterdir() if item.is_file() or item.is_dir()]
                except OSError:
                    legacy_files = []
                if loaded_manifest.status == ManifestStatus.VALID or legacy_files:
                    paths.materials.mkdir(exist_ok=True)
                else:
                    raise ValidationError("Папка возобновляемого проекта повреждена.")
        else:
            root = new_project_path or self.planned_path(destination, metadata)
            paths = ProjectPaths(root, root / "Материалы", root.name)
        saved_record = self.job_store.load(metadata.video_id) if resume_path else None
        saved_temp_value = str((saved_record or {}).get("temp_path", "")).strip() if saved_record else ""
        saved_temp_path = Path(saved_temp_value) if saved_temp_value else None
        saved_job_id = str((saved_record or {}).get("job_id", "")).strip()
        if saved_temp_path and saved_temp_path.is_dir():
            # Records created before per-job temp support may have a temp_path
            # but no job_id.  In that case the final directory name is the
            # only safe identity we can resume and later remove.
            job_id = saved_job_id or saved_temp_path.name
        else:
            job_id = options.job_id or uuid.uuid4().hex
        temp_root = Path(options.temp_root) if options.temp_root else default_temp_root(destination)
        if saved_temp_path and saved_temp_path.is_dir():
            temp_root = saved_temp_path.parent
        job_temp = JobTempManager(temp_root, job_id)
        job_temp_path = job_temp.create()
        job_environment = job_temp.environment()
        saved_auth = saved_record.get("youtube_auth", {}) if isinstance(saved_record, dict) else {}
        auth = getattr(self.yt_dlp, "auth", None)
        if auth is not None and saved_auth.get("enabled") and saved_auth.get("user_consented"):
            saved_mode = str(saved_auth.get("mode", "anonymous"))
            if saved_mode in {"firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi"}:
                auth.enable_one_time(
                    "browser",
                    saved_mode,
                    str(saved_auth.get("browser_profile", "")),
                )
            elif saved_mode == "cookies_file":
                auth.enable_one_time(
                    "cookies_file",
                    cookies_file=str(saved_auth.get("cookies_file", "")),
                )
        elif auth is not None and not getattr(auth, "user_consented", False):
            auth.disable_for_anonymous_job()
        if isinstance(saved_record, dict) and isinstance(saved_record.get("files"), dict):
            files.update(
                {key: Path(str(value)) for key, value in saved_record["files"].items() if value}
            )
        record: Dict[str, Any] = {
            "created_by": "CreatorAssistant",
            "video_id": metadata.video_id,
            "url": metadata.webpage_url,
            "title": metadata.title,
            "status": "running",
            "project_path": str(paths.root),
            "materials_path": str(paths.materials),
            "author_path": str(destination),
            "job_id": job_id,
            "temp_root": str(temp_root),
            "temp_path": str(job_temp_path),
            "reaper_proxy_height": options.reaper_proxy_height,
            "requested_reaper_proxy_height": options.reaper_proxy_height,
            "effective_reaper_proxy_height": effective_proxy_height,
            "created_at": (saved_record or {}).get("created_at") or dt.datetime.now().isoformat(timespec="seconds"),
            "expected_duration": metadata.duration,
            "expected_height": plan.maximum_video.height,
            "expected_fps": plan.maximum_video.fps,
            "files": dict(saved_record.get("files", {})) if isinstance(saved_record, dict) else {},
            "stages": dict(saved_record.get("stages", {})) if isinstance(saved_record, dict) else {},
            "stage": "",
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "auth_mode": str(getattr(auth, "effective_mode", "anonymous")),
            "youtube_auth": {
                "enabled": bool(getattr(auth, "enabled", False)),
                "mode": str(getattr(auth, "effective_mode", "anonymous")),
                "browser": str(getattr(auth, "browser", "")),
                "browser_profile": str(getattr(auth, "browser_profile", "")),
                "cookies_file": (
                    str(getattr(auth, "cookies_file", ""))
                    if getattr(auth, "mode", "") == "cookies_file"
                    else ""
                ),
                "user_consented": bool(getattr(auth, "user_consented", False)),
            },
        }
        identity_record = self.job_store.load(metadata.video_id) or {}
        history = [str(value) for value in identity_record.get("project_paths", []) if value]
        previous_path = str(identity_record.get("project_path", ""))
        if previous_path and previous_path not in history:
            history.append(previous_path)
        if str(paths.root) not in history:
            history.append(str(paths.root))
        record["project_paths"] = history

        if resume_path:
            inspected = self.inspect_existing(metadata, options, paths.root, cancellation)
            previous_stages = saved_record.get("stages", {}) if isinstance(saved_record, dict) else {}
            for key, details in inspected.items():
                previous = previous_stages.get(key, {}) if isinstance(previous_stages, dict) else {}
                if details.get("status") in {"NOT_REQUIRED", "INFO"} and isinstance(previous, dict) and previous:
                    inspected[key] = previous
                    details = previous
                if details.get("status") == "VALID" and isinstance(previous, dict):
                    same_path = str(previous.get("path") or "").casefold() == str(details.get("path") or "").casefold()
                    if same_path:
                        details = {**previous, **details}
                        inspected[key] = details
                if details.get("status") == "PARTIAL" and isinstance(previous, dict):
                    for field in ("video_id", "role", "format_id", "output_template"):
                        if previous.get(field):
                            details[field] = previous[field]
            record["stages"] = inspected
            for key, details in inspected.items():
                if details.get("status") == "VALID" and details.get("path"):
                    files[key] = Path(str(details["path"]))
                elif details.get("status") not in {"NOT_REQUIRED", "INFO"}:
                    files.pop(key, None)
            record["files"] = {key: str(value) for key, value in files.items()}
            ambiguous = [
                (key, details) for key, details in inspected.items()
                if key in self.required_roles(options, inspected)
                and details.get("status") in {"AMBIGUOUS", "CONFIRMATION_REQUIRED"}
            ]
            if ambiguous:
                key, details = ambiguous[0]
                raise MediaRoleResolutionRequired(key, details.get("candidates", []))

        def stage(job_stage: JobStage, message: str, percent: Optional[float] = None) -> None:
            cancellation.raise_if_cancelled()
            state.advance(job_stage)
            record["stage"] = job_stage.name
            record["status"] = "running"
            record["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            record["files"] = {key: str(value) for key, value in files.items()}
            self.job_store.save(metadata.video_id, record)
            on_progress(ProgressInfo(job_stage.value, message, percent))

        def checkpoint() -> None:
            record["files"] = {key: str(value) for key, value in files.items()}
            record["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            self.job_store.save(metadata.video_id, record)

        def require_space(operation: str, path: Path, estimated: int) -> None:
            info = self.storage.require(operation, path, estimated)
            record["storage_check"] = {
                "operation": operation,
                "path": str(path),
                "volume": info.volume,
                "required_bytes": estimated,
                "free_bytes": info.free,
                "reserve_bytes": self.storage.policy.reserve_bytes,
            }
            checkpoint()

        def save_waiting_for_disk(error: DiskSpaceError) -> None:
            if not error.path:
                error.path = str(job_temp_path)
            record["status"] = "waiting_for_disk_space"
            record["stage"] = record.get("stage") or JobStage.CHECK_DISK_SPACE.name
            record["files"] = {key: str(value) for key, value in files.items()}
            record["disk_space"] = {
                "operation": error.operation,
                "path": error.path,
                "required_bytes": error.required_bytes,
                "free_bytes": error.free_bytes,
                "reserve_bytes": error.reserve_bytes,
                "temp_path": str(job_temp_path),
                "temp_size": job_temp.size(),
                "details": error.details,
            }
            self.job_store.save(metadata.video_id, record)
            try:
                self._write_manifest(paths.root, metadata, "waiting_for_disk_space", options.reaper_proxy_height)
            except OSError:
                # If the destination itself is full, the JobStore checkpoint on
                # the system volume remains the authoritative resume record.
                pass

        try:
            stage(JobStage.CHECK_DEPENDENCIES, "Проверяю необходимые программы")
            self._check_dependencies(options, files)
            stage(JobStage.CHECK_DISK_SPACE, "Оцениваю свободное место")
            self._check_disk_space(destination, plan, options)
            require_space("Временные файлы задания", job_temp_path, 256 * 1024**2)
            stage(JobStage.CREATE_STRUCTURE, "Готовлю новую папку проекта")
            if not resume_path:
                paths.root.mkdir(parents=False, exist_ok=False)
                paths.materials.mkdir(exist_ok=False)
                record["project_path"] = str(paths.root)
                record["materials_path"] = str(paths.materials)
            self._write_manifest(paths.root, metadata, "running", options.reaper_proxy_height)
            self.job_store.save(metadata.video_id, record)
            if metadata.best_thumbnail:
                stage(JobStage.DOWNLOAD_THUMBNAIL, "Скачиваю лучшее превью")
                thumbnail = files.get("thumbnail")
                if not thumbnail or not thumbnail.is_file() or thumbnail.stat().st_size == 0:
                    files["thumbnail"] = self.thumbnail.download(
                        metadata.best_thumbnail.url,
                        paths.root / self.naming.preview,
                        cancellation,
                    )
                else:
                    on_progress(ProgressInfo(JobStage.DOWNLOAD_THUMBNAIL.value, "Готово ранее — пропущено", 100.0))
                checkpoint()
            if options.download_maximum:
                stage(JobStage.DOWNLOAD_MAXIMUM, "Скачиваю максимальное SDR-видео")
                existing = self._recorded_valid_file(record, "maximum", "video", cancellation)
                if existing:
                    files["maximum"] = existing
                    on_progress(ProgressInfo(JobStage.DOWNLOAD_MAXIMUM.value, "Найдено готовое максимальное видео — пропущено", 100.0))
                else:
                    require_space(
                        "Скачивание максимального видео", paths.materials,
                        self.storage.estimate_download(plan.maximum_video.size, plan.maximum_audio.size),
                    )
                    invalid = record.get("stages", {}).get("maximum", {})
                    if invalid.get("status") == "INVALID":
                        raise ValidationError("Существующее максимальное видео не прошло проверку и не будет перезаписано: " + str(invalid.get("reason", "неизвестная причина")))
                    blocked = self._blocked_existing_stage(record, "maximum")
                    if blocked:
                        raise ValidationError(blocked)
                    max_stem = safe_file_name(
                        paths.base_name,
                        self.naming.maximum,
                        "%(ext)s",
                        height=plan.maximum_video.height or 0,
                    )
                    max_stem = max_stem[: -len(".%(ext)s")] + ".%(ext)s"
                    max_template = paths.materials / max_stem
                    compatible_selectors = [
                        plan.maximum_selector,
                        *self._maximum_alternative_selectors(metadata.formats, plan),
                    ]
                    saved_selector = str(record.get("stages", {}).get("maximum", {}).get("format_id", ""))
                    primary_selector = saved_selector if saved_selector in compatible_selectors else plan.maximum_selector
                    alternative_selectors = [
                        value for value in compatible_selectors if value != primary_selector
                    ]
                    record.setdefault("stages", {})["maximum"] = {
                        "status": "DOWNLOADING",
                        "video_id": metadata.video_id,
                        "role": "MAX_VIDEO",
                        "format_id": primary_selector,
                        "output_template": str(max_template),
                        "partials": [str(path) for path in self._partial_files(max_template)],
                    }
                    self.job_store.save(metadata.video_id, record)
                    files["maximum"] = self.yt_dlp.download(
                        metadata.webpage_url,
                        primary_selector,
                        max_template,
                        cancellation,
                        JobStage.DOWNLOAD_MAXIMUM.value,
                        plan.maximum_container,
                        on_progress,
                        role="MAX_VIDEO",
                        alternative_selectors=alternative_selectors,
                        environment=job_environment,
                        cwd=job_temp_path,
                    )
                    self.validator.validate_expected_video(
                        files["maximum"], cancellation,
                        duration=metadata.duration,
                        height=plan.maximum_video.height,
                        fps=plan.maximum_video.fps,
                        require_audio=True,
                        require_sdr=True,
                    )
                checkpoint()
            if options.create_proxy:
                stage(
                    JobStage.CREATE_PROXY,
                    f"Скачивание или создание видео {options.reaper_proxy_height}p",
                )
                existing = self._recorded_valid_file(record, "proxy", "video", cancellation)
                if existing:
                    files["proxy"] = existing
                    on_progress(ProgressInfo(JobStage.CREATE_PROXY.value, f"Готовое видео {options.reaper_proxy_height}p найдено — пропущено", 100.0))
                else:
                    proxy_estimate = self.storage.estimate_download(plan.proxy_video.size, plan.proxy_audio.size)
                    require_space(f"Создание видео {options.reaper_proxy_height}p для REAPER", paths.materials, proxy_estimate)
                    require_space(f"Временные файлы видео {options.reaper_proxy_height}p", job_temp_path, proxy_estimate)
                    proxy_name = safe_file_name(
                        paths.base_name, self.naming.proxy, "mp4",
                        proxy_height=effective_proxy_height,
                    )
                    target = self._non_destructive_target(paths.materials / proxy_name)
                    max_is_exact_proxy = (
                        "maximum" in files
                        and source_height == effective_proxy_height
                        and is_reaper_compatible(plan.maximum_video, plan.maximum_audio)
                        and files["maximum"].suffix.casefold() == ".mp4"
                    )
                    if max_is_exact_proxy:
                        shutil.copy2(files["maximum"], target)
                    elif "maximum" in files:
                        self.ffmpeg.create_proxy(
                            files["maximum"], target, cancellation, transcode_video=True,
                            on_progress=on_progress,
                            stage=f"Создание видео {effective_proxy_height}p из существующего MAX",
                            maximum_height=effective_proxy_height,
                            environment=job_environment,
                            cwd=job_temp_path,
                        )
                    elif plan.proxy_requires_transcode:
                        proxy_temp = job_temp_path / "proxy_source"
                        proxy_temp.mkdir(exist_ok=True)
                        source = self.yt_dlp.download(
                                metadata.webpage_url,
                                plan.proxy_selector,
                                proxy_temp / "proxy_source.%(ext)s",
                                cancellation,
                                JobStage.CREATE_PROXY.value,
                                "mkv",
                                on_progress,
                                role="PROXY_SOURCE",
                                environment=job_environment,
                                cwd=job_temp_path,
                            )
                        self.ffmpeg.create_proxy(
                            source, target, cancellation, transcode_video=True,
                            on_progress=on_progress,
                            stage=f"Создание видео {options.reaper_proxy_height}p",
                            maximum_height=effective_proxy_height,
                            environment=job_environment,
                            cwd=job_temp_path,
                        )
                    else:
                        downloaded = self.yt_dlp.download(
                            metadata.webpage_url,
                            plan.proxy_selector,
                            paths.materials / (Path(proxy_name).stem + ".%(ext)s"),
                            cancellation,
                            JobStage.CREATE_PROXY.value,
                            "mp4",
                            on_progress,
                            role="PROXY_VIDEO",
                            environment=job_environment,
                            cwd=job_temp_path,
                        )
                        if downloaded.resolve() != target.resolve():
                            self.ffmpeg.create_proxy(
                                downloaded, target, cancellation, transcode_video=False,
                                on_progress=on_progress,
                                stage=f"Создание видео {options.reaper_proxy_height}p",
                                maximum_height=effective_proxy_height,
                                environment=job_environment,
                                cwd=job_temp_path,
                            )
                    files["proxy"] = target
                    proxy_probe = self.validator.validate_expected_video(
                        target,
                        cancellation,
                        duration=metadata.duration,
                        height=effective_proxy_height,
                        fps=plan.proxy_video.fps or plan.maximum_video.fps,
                        require_audio=True,
                        require_sdr=True,
                    )
                    video_stream = next(
                        item for item in proxy_probe.get("streams", [])
                        if item.get("codec_type") == "video"
                    )
                    proxy_state = {
                        "status": "VALID",
                        "path": str(target),
                        "size": target.stat().st_size,
                        "requested_height": options.reaper_proxy_height,
                        "effective_height": effective_proxy_height,
                        "width": int(video_stream.get("width") or 0),
                        "height": int(video_stream.get("height") or 0),
                        "fps": self._rate_value(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
                        "source": "existing_max_transcode" if "maximum" in files else "youtube_proxy",
                        "probe": proxy_probe,
                    }
                    record.setdefault("stages", {})["proxy"] = proxy_state
                    variants = record.setdefault("stages", {}).setdefault(
                        "proxy_variants", {"status": "INFO", "variants": {}}
                    ).setdefault("variants", {})
                    variants[str(effective_proxy_height)] = dict(proxy_state)
                    self._update_scan_index_file(
                        paths.root,
                        target,
                        "REAPER_PROXY",
                        {
                            "requested_height": options.reaper_proxy_height,
                            "effective_height": effective_proxy_height,
                            "width": proxy_state["width"],
                            "height": proxy_state["height"],
                            "fps": proxy_state["fps"],
                        },
                    )
                checkpoint()
            if options.download_audio:
                stage(JobStage.DOWNLOAD_AUDIO, "Скачиваю лучшую оригинальную аудиодорожку")
                existing = self._recorded_valid_file(record, "audio", "audio", cancellation)
                if existing:
                    files["audio"] = existing
                    on_progress(ProgressInfo(JobStage.DOWNLOAD_AUDIO.value, "Готовое аудио найдено — пропущено", 100.0))
                else:
                    blocked = self._blocked_existing_stage(record, "audio")
                    if blocked:
                        raise ValidationError(blocked)
                    require_space(
                        "Скачивание оригинального аудио", paths.materials,
                        self.storage.estimate_download(plan.best_audio.size),
                    )
                    audio_stem = safe_file_name(paths.base_name, self.naming.audio, "%(ext)s")
                    audio_stem = audio_stem[: -len(".%(ext)s")] + ".%(ext)s"
                    files["audio"] = self.yt_dlp.download(
                        metadata.webpage_url,
                        plan.best_audio.format_id,
                        paths.materials / audio_stem,
                        cancellation,
                        JobStage.DOWNLOAD_AUDIO.value,
                        None,
                        on_progress,
                        role="ORIGINAL_AUDIO",
                        environment=job_environment,
                        cwd=job_temp_path,
                    )
                    self.validator.validate_audio(files["audio"], cancellation)
                checkpoint()
            if options.create_instrumental:
                stage(JobStage.SEPARATE_STEMS, "Создаю Instrumental FLAC")
                existing = self._recorded_valid_file(record, "instrumental", "audio", cancellation)
                if existing:
                    files["instrumental"] = existing
                    on_progress(ProgressInfo(JobStage.SEPARATE_STEMS.value, "Готовый Instrumental FLAC найден — пропущено", 100.0))
                else:
                    if "audio" not in files:
                        raise ValidationError("Для UVR сначала нужна оригинальная аудиодорожка.")
                    blocked = self._blocked_existing_stage(record, "instrumental")
                    if blocked:
                        raise ValidationError(blocked)
                    inst_name = safe_file_name(paths.base_name, self.naming.instrumental, "flac")
                    source_probe = self.validator.validate_audio(files["audio"], cancellation)
                    source_duration = self.validator.duration(source_probe) or metadata.duration or 0
                    source_stream = next(
                        stream for stream in source_probe.get("streams", []) if stream.get("codec_type") == "audio"
                    )
                    sample_rate = int(source_stream.get("sample_rate") or 44100)
                    channels = int(source_stream.get("channels") or 2)
                    separator_estimate = self.storage.estimate_separator(source_duration, sample_rate, channels)
                    require_space("Подготовка временного аудио и Audio Separator", job_temp_path, separator_estimate)
                    require_space(
                        "Создание Instrumental FLAC", paths.materials,
                        max(512 * 1024**2, separator_estimate // 4),
                    )
                    if not self.direct_separator.available():
                        runtime_path = getattr(getattr(self.direct_separator, "runtime", None), "root", "")
                        raise AudioSeparatorRuntimeMissingError(runtime_path)
                    separator: StemSeparatorBackend = self.direct_separator
                    configure_job = getattr(separator, "configure_job", None)
                    if configure_job:
                        configure_job(job_id, job_temp_path)
                    files["instrumental"] = separator.separate(
                        files["audio"],
                        paths.materials / inst_name,
                        cancellation,
                        lambda message: on_progress(ProgressInfo(JobStage.SEPARATE_STEMS.value, message)),
                    )
                    output_probe = self.validator.validate_expected_audio(
                        files["instrumental"],
                        cancellation,
                        duration=source_duration,
                        require_flac=True,
                    )
                    output_duration = self.validator.duration(output_probe)
                    if source_duration and output_duration and abs(source_duration - output_duration) > max(2.0, source_duration * 0.02):
                        raise ValidationError("Длительность инструментала заметно отличается от исходного аудио.")
                    output_stream = next(stream for stream in output_probe.get("streams", []) if stream.get("codec_type") == "audio")
                    if int(output_stream.get("channels") or 0) != int(source_stream.get("channels") or 0):
                        raise ValidationError("Число каналов Instrumental не совпадает с исходным аудио.")
                    if int(output_stream.get("sample_rate") or 0) != int(source_stream.get("sample_rate") or 0):
                        raise ValidationError("Sample rate Instrumental не совпадает с исходным аудио.")
                checkpoint()
            if options.create_reaper_project:
                stage(JobStage.CREATE_REAPER, "Создаю RPP с двумя дорожками")
                if "proxy" not in files or "instrumental" not in files:
                    raise ValidationError(
                        f"Для проекта REAPER нужны существующие видео {options.reaper_proxy_height}p и Instrumental."
                    )
                rpp = paths.root / safe_file_name(paths.base_name, "{title}", "rpp")
                if rpp.exists() and not (
                    files.get("reaper")
                    and self.reaper.validate_project(files["reaper"], files["proxy"], files["instrumental"])
                ):
                    rpp = self._non_destructive_target(
                        paths.root / safe_file_name(
                            paths.base_name,
                            f"{{title}} [{effective_proxy_height}p]",
                            "rpp",
                        )
                    )
                duration = metadata.duration or 0.001
                if files.get("reaper") and self.reaper.validate_project(files["reaper"], files["proxy"], files["instrumental"]):
                    rpp = files["reaper"]
                    on_progress(ProgressInfo(JobStage.CREATE_REAPER.value, "Готовый проект REAPER найден — пропущено", 100.0))
                else:
                    files["reaper"] = self.reaper.generate_project(
                        rpp,
                        files["proxy"],
                        files["instrumental"],
                        duration,
                        self.initial_audio,
                        effective_proxy_height,
                    )
                if not self.reaper.validate_project(rpp, files["proxy"], files["instrumental"]):
                    raise ValidationError("Созданный проект REAPER не прошёл проверку.")
                record.setdefault("stages", {}).setdefault("reaper", {}).update({
                    "proxy_path": str(files["proxy"]),
                    "proxy_height": effective_proxy_height,
                })
                checkpoint()
            if options.create_vegas_project:
                stage(JobStage.CREATE_VEGAS, "Создаю проект VEGAS с MAX video и Instrumental")
                if "maximum" not in files or "instrumental" not in files:
                    raise ValidationError("Для проекта VEGAS нужны MAX video и Instrumental.")
                vegas_project = self.vegas.project_path(paths.root, paths.base_name)
                existing = files.get("vegas")
                if existing and self.vegas.validate_project(existing):
                    on_progress(ProgressInfo(JobStage.CREATE_VEGAS.value, "Готовый проект VEGAS найден — пропущено", 100.0))
                else:
                    result = self.vegas.create_project(
                        output=vegas_project,
                        max_video=files["maximum"],
                        instrumental=files["instrumental"],
                        duration=metadata.duration or 0.001,
                        temp_dir=job_temp_path / "vegas",
                        cancellation=cancellation,
                        auto_open=False,
                        job_id=job_id,
                    )
                    files["vegas"] = result.path
                    record.setdefault("stages", {})["vegas"] = {
                        "status": "VALID",
                        "path": str(result.path),
                        "size": result.path.stat().st_size,
                        "role": "VEGAS_PROJECT",
                        "source": "vegas_script",
                        "video_path": str(files["maximum"]),
                        "instrumental_path": str(files["instrumental"]),
                        "created_by_creator_assistant": True,
                        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                        "vegas_version": str(result.details.get("vegas_version") or ""),
                        "video_only_from_max": True,
                        "tracks": result.tracks,
                        "video_track_count": result.video_track_count,
                        "audio_track_count": result.audio_track_count,
                        "video_events": result.video_events,
                        "audio_events": result.audio_events,
                        "max_audio_events": result.max_audio_events,
                        "video_start_nanos": result.video_start_nanos,
                        "audio_start_nanos": result.audio_start_nanos,
                        "width": result.width,
                        "height": result.height,
                        "fps": result.fps,
                    }
                checkpoint()
            stage(JobStage.FINAL_VALIDATION, "Проверяю итоговые файлы")
            cancellation.raise_if_cancelled()
            final_stages = self.inspect_existing(metadata, options, paths.root, cancellation)
            previous_stages = record.get("stages", {})
            for key, details in list(final_stages.items()):
                previous = previous_stages.get(key, {}) if isinstance(previous_stages, dict) else {}
                if details.get("status") == "NOT_REQUIRED" and isinstance(previous, dict) and previous:
                    final_stages[key] = previous
                    continue
                if key == "proxy_variants" and isinstance(previous, dict):
                    old_variants = previous.get("variants", {}) if isinstance(previous.get("variants"), dict) else {}
                    new_variants = details.get("variants", {}) if isinstance(details.get("variants"), dict) else {}
                    final_stages[key] = {**previous, **details, "variants": {**old_variants, **new_variants}}
                    continue
                if details.get("status") == "VALID" and isinstance(previous, dict):
                    same_path = str(previous.get("path") or "").casefold() == str(details.get("path") or "").casefold()
                    if same_path:
                        final_stages[key] = {**previous, **details}
            required_stages = {key: True for key in self.required_roles(options, final_stages)}
            incomplete = [
                key for key, enabled in required_stages.items()
                if enabled and final_stages.get(key, {}).get("status") != "VALID"
            ]
            if incomplete:
                raise ValidationError(
                    "Финальная проверка не подтверждает готовность этапов: " + ", ".join(incomplete)
                )
            record["stages"] = final_stages
            record["files"] = {key: str(value) for key, value in files.items()}
            record["status"] = "completed"
            record["stage"] = JobStage.DONE.name
            self.job_store.save(metadata.video_id, record)
            self._write_manifest(
                paths.root,
                metadata,
                "completed",
                options.reaper_proxy_height,
                record.get("files", {}),
                record.get("stages", {}),
            )
            on_progress(ProgressInfo(JobStage.DONE.value, "Проект готов", 100.0))
            try:
                job_temp.mark_completed()
                job_temp.cleanup_current()
                record["temp_cleaned"] = True
                self.job_store.save(metadata.video_id, record)
            except OSError as exc:
                record["temp_cleanup_error"] = str(exc)
                self.job_store.save(metadata.video_id, record)
            return ProjectResult(paths.root, self._plan_lines(metadata, paths, plan, options), files, resumed)
        except DiskSpaceError as exc:
            save_waiting_for_disk(exc)
            raise
        except OSError as exc:
            classified = classify_resource_failure(exc)
            if isinstance(classified, DiskSpaceError):
                save_waiting_for_disk(classified)
                raise classified from exc
            raise
        except AudioSeparatorRuntimeMissingError:
            record["status"] = "runtime_install_required"
            record["stage"] = JobStage.SEPARATE_STEMS.name
            record["files"] = {key: str(value) for key, value in files.items()}
            record["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            self.job_store.save(metadata.video_id, record)
            self._write_manifest(paths.root, metadata, "runtime_install_required", options.reaper_proxy_height, record.get("files", {}), record.get("stages", {}))
            raise
        except ManualActionRequiredError:
            record["status"] = "manual_action_required"
            record["stage"] = JobStage.SEPARATE_STEMS.name
            record.setdefault("stages", {})["instrumental"] = {"status": "MANUAL_ACTION_REQUIRED"}
            record["files"] = {key: str(value) for key, value in files.items()}
            record["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            self.job_store.save(metadata.video_id, record)
            self._write_manifest(paths.root, metadata, "manual_action_required", options.reaper_proxy_height, record.get("files", {}), record.get("stages", {}))
            raise
        except YouTubeMediaForbiddenError as exc:
            record["status"] = "media_forbidden"
            record["stage"] = record.get("stage") or JobStage.DOWNLOAD_MAXIMUM.name
            record.setdefault("stages", {})["maximum"] = {
                "status": "PARTIAL",
                "path": str(exc.part_path or ""),
                "format_id": exc.format_id,
                "downloaded_bytes": exc.downloaded_bytes,
                "total_bytes": exc.total_bytes,
                "percent": exc.percent,
                "attempts": exc.attempts,
            }
            record["files"] = {key: str(value) for key, value in files.items()}
            record["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            self.job_store.save(metadata.video_id, record)
            self._write_manifest(paths.root, metadata, "media_forbidden", options.reaper_proxy_height, record.get("files", {}), record.get("stages", {}))
            raise
        except JobCancelledError:
            record["status"] = "cancelled"
            record.setdefault("stages", {})["instrumental"] = {"status": "CANCELLED"}
            record["files"] = {key: str(value) for key, value in files.items()}
            record["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            self.job_store.save(metadata.video_id, record)
            self._write_manifest(paths.root, metadata, "cancelled", options.reaper_proxy_height, record.get("files", {}), record.get("stages", {}))
            raise
        except Exception:
            record["status"] = "failed"
            record["files"] = {key: str(value) for key, value in files.items()}
            self.job_store.save(metadata.video_id, record)
            self._write_manifest(paths.root, metadata, "failed", options.reaper_proxy_height, record.get("files", {}), record.get("stages", {}))
            raise

    def _check_dependencies(
        self,
        options: ProjectOptions,
        existing_files: Optional[Dict[str, Path]] = None,
    ) -> None:
        existing = existing_files or {}
        missing = []
        needs_youtube_media = any((
            options.download_maximum and "maximum" not in existing,
            options.create_proxy and "proxy" not in existing,
            options.download_audio and "audio" not in existing,
            options.create_instrumental
            and "instrumental" not in existing
            and "audio" not in existing,
        ))
        if needs_youtube_media and not Path(self.yt_dlp.executable).is_file():
            missing.append("yt-dlp")
        if needs_youtube_media and not Path(self.ffmpeg.ffmpeg_path).is_file():
            missing.append("FFmpeg")
        if not Path(self.validator.ffprobe_path).is_file():
            missing.append("FFprobe")
        if options.create_instrumental and "instrumental" not in existing and not self.direct_separator.available():
            runtime_path = getattr(getattr(self.direct_separator, "runtime", None), "root", "")
            raise AudioSeparatorRuntimeMissingError(runtime_path)
        if options.create_vegas_project and "vegas" not in existing and not self.vegas.available:
            missing.append("VEGAS Pro")
        if missing:
            raise DependencyMissingError("Не найдены зависимости: " + ", ".join(missing))

    def _check_disk_space(self, destination: Path, plan: FormatPlan, options: ProjectOptions) -> None:
        # Heavy stages are checked individually immediately before execution.
        self.storage.require("Создание структуры проекта", destination, 64 * 1024**2)

    def _recorded_valid_file(
        self,
        record: Dict[str, Any],
        key: str,
        media_type: str,
        cancellation: CancellationToken,
    ) -> Optional[Path]:
        raw = record.get("files", {}).get(key) if isinstance(record.get("files"), dict) else None
        if not raw:
            return None
        path = Path(str(raw))
        try:
            if media_type == "video":
                self.validator.validate_video(path, cancellation)
            else:
                self.validator.validate_audio(path, cancellation)
            return path
        except JobCancelledError:
            raise
        except Exception:
            return None

    @staticmethod
    def _maximum_alternative_selectors(formats: List[VideoFormat], plan: FormatPlan) -> List[str]:
        selected = plan.maximum_video
        candidates = []
        for item in formats:
            if not item.has_video or item.format_id == selected.format_id:
                continue
            if item.height != selected.height:
                continue
            if selected.fps and item.fps and abs(item.fps - selected.fps) > 0.15:
                continue
            transfer = str(item.color_transfer or "").casefold()
            dynamic_range = str(item.dynamic_range or "SDR").casefold()
            if transfer in {"smpte2084", "arib-std-b67"} or dynamic_range not in {"", "sdr"}:
                continue
            selector = item.format_id if item.has_audio else f"{item.format_id}+{plan.maximum_audio.format_id}"
            candidates.append((item.size or 0, selector))
        candidates.sort(reverse=True)
        return [selector for _size, selector in candidates]

    def _plan_lines(
        self,
        metadata: VideoMetadata,
        paths: ProjectPaths,
        plan: FormatPlan,
        options: ProjectOptions,
    ) -> List[str]:
        lines = [
            f"Проект: {paths.root}",
            f"Максимум SDR: {plan.maximum_video.height or '?'}p, {plan.maximum_video.fps or '?'} FPS, контейнер {plan.maximum_container.upper()}",
            f"{options.reaper_proxy_height}p-план: {plan.proxy_video.height or '?'}p, {plan.proxy_video.fps or '?'} FPS; "
            + ("перекодирование только прокси" if plan.proxy_requires_transcode else "объединение без перекодирования"),
            f"Аудио: формат {plan.best_audio.format_id}, кодек {plan.best_audio.acodec}",
            "HDR/HLG/Dolby Vision исключены при выборе форматов.",
        ]
        if options.create_instrumental:
            lines.append("UVR: MDX-Net / UVR-MDX-NET Inst HQ 3 / Instrumental Only / FLAC.")
        if options.create_reaper_project:
            lines.append(f"REAPER: две дорожки с позиции 0 — VIDEO {options.reaper_proxy_height}P — ORIGINAL и INSTRUMENTAL.")
        if options.create_vegas_project:
            lines.append("VEGAS: защищённый отдельный процесс, только MAX video stream + Instrumental FLAC с 0.")
        return lines
