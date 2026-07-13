from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


CURRENT_SCHEMA_VERSION = 2
MANIFEST_NAME = ".creator-assistant.json"


class ManifestStatus(str, Enum):
    VALID = "VALID"
    LEGACY = "LEGACY"
    INCOMPLETE = "INCOMPLETE"
    CORRUPTED = "CORRUPTED"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    MISSING = "MISSING"


@dataclass(frozen=True)
class DiscoveredFile:
    path: str
    classification: str = "UNKNOWN"
    size: int = 0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectManifest:
    schema_version: int = CURRENT_SCHEMA_VERSION
    video_id: str = ""
    source_url: str = ""
    title: str = ""
    project_path: str = ""
    materials_path: str = ""
    author_preset: str = ""
    preset_id: str = ""
    channel_id: str = ""
    uploader_id: str = ""
    channel_name: str = ""
    job_id: str = ""
    state: str = "DISCOVERED"
    stages: Dict[str, Any] = field(default_factory=dict)
    files: Dict[str, Any] = field(default_factory=dict)
    discovered_files: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    reaper_proxy_height: int = 720

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManifestLoadResult:
    status: ManifestStatus
    path: Path
    manifest: Optional[ProjectManifest] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    message: str = ""


class ManifestValidator:
    @staticmethod
    def parse(data: Any, path: Path) -> ManifestLoadResult:
        if not isinstance(data, dict):
            return ManifestLoadResult(ManifestStatus.CORRUPTED, path, message="Корень manifest должен быть JSON-объектом.")
        version = data.get("schema_version", 0)
        if not isinstance(version, int):
            return ManifestLoadResult(ManifestStatus.INCOMPLETE, path, raw=data, message="schema_version имеет неверный тип.")
        if version > CURRENT_SCHEMA_VERSION:
            return ManifestLoadResult(ManifestStatus.UNSUPPORTED_VERSION, path, raw=data, message=f"Версия {version} не поддерживается.")
        if version < CURRENT_SCHEMA_VERSION:
            return ManifestLoadResult(ManifestStatus.LEGACY, path, raw=data, message=f"Требуется миграция schema_version={version}.")
        manifest = ProjectManifest(
            schema_version=version,
            video_id=str(data.get("video_id") or ""),
            source_url=str(data.get("source_url") or data.get("url") or ""),
            title=str(data.get("title") or ""),
            project_path=str(data.get("project_path") or ""),
            materials_path=str(data.get("materials_path") or ""),
            author_preset=str(data.get("author_preset") or ""),
            preset_id=str(data.get("preset_id") or ""),
            channel_id=str(data.get("channel_id") or ""),
            uploader_id=str(data.get("uploader_id") or ""),
            channel_name=str(data.get("channel_name") or data.get("channel") or data.get("uploader") or ""),
            job_id=str(data.get("job_id") or ""),
            state=str(data.get("state") or data.get("status") or "DISCOVERED"),
            stages=data.get("stages") if isinstance(data.get("stages"), dict) else {},
            files=data.get("files") if isinstance(data.get("files"), dict) else {},
            discovered_files=data.get("discovered_files") if isinstance(data.get("discovered_files"), list) else [],
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            reaper_proxy_height=(
                int(data.get("reaper_proxy_height", 720))
                if str(data.get("reaper_proxy_height", 720)).isdigit()
                and int(data.get("reaper_proxy_height", 720)) in {480, 720, 1080}
                else 720
            ),
        )
        missing = [name for name in ("video_id", "project_path", "materials_path") if not getattr(manifest, name)]
        if missing:
            return ManifestLoadResult(
                ManifestStatus.INCOMPLETE, path, manifest=manifest, raw=data,
                message="Отсутствуют обязательные поля: " + ", ".join(missing),
            )
        return ManifestLoadResult(ManifestStatus.VALID, path, manifest=manifest, raw=data)

    @staticmethod
    def validate_for_write(manifest: ProjectManifest) -> None:
        result = ManifestValidator.parse(manifest.to_dict(), Path(manifest.project_path) / MANIFEST_NAME)
        if result.status != ManifestStatus.VALID:
            raise ValueError(result.message or f"Manifest status is {result.status.value}")


class ManifestLoader:
    def load(self, path: Path) -> ManifestLoadResult:
        if not path.is_file():
            return ManifestLoadResult(ManifestStatus.MISSING, path, message="Manifest отсутствует.")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return ManifestLoadResult(ManifestStatus.CORRUPTED, path, message=str(exc))
        return ManifestValidator.parse(raw, path)


class ManifestWriter:
    def __init__(self, loader: Optional[ManifestLoader] = None) -> None:
        self.loader = loader or ManifestLoader()

    def write(self, path: Path, manifest: ProjectManifest, *, backup_existing: bool = True) -> ManifestLoadResult:
        ManifestValidator.validate_for_write(manifest)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        backup = path.with_name(path.name + ".bak")
        backup_temporary = backup.with_name(backup.name + ".tmp")
        try:
            if path.exists() and backup_existing and not backup.exists():
                # Read the complete original before touching any destination file.
                original = path.read_bytes()
                with backup_temporary.open("wb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(str(backup_temporary), str(backup))
            payload = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(path))
            saved = self.loader.load(path)
            if saved.status != ManifestStatus.VALID:
                raise ValueError("Повторная проверка записанного manifest не пройдена: " + saved.message)
            return saved
        except Exception:
            for candidate in (temporary, backup_temporary):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            raise


class LegacyProjectScanner:
    MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus"}
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

    def __init__(self, probe: Optional[Callable[[Path], Dict[str, Any]]] = None) -> None:
        self.probe = probe

    def scan(self, project_path: Path) -> List[DiscoveredFile]:
        results: List[DiscoveredFile] = []
        try:
            candidates = [item for item in project_path.rglob("*") if item.is_file()]
        except OSError:
            candidates = []
        ignored = {MANIFEST_NAME, MANIFEST_NAME + ".bak", MANIFEST_NAME + ".tmp"}
        for path in candidates:
            if path.name in ignored:
                continue
            classification = "UNKNOWN"
            details: Dict[str, Any] = {}
            suffix = path.suffix.casefold()
            if suffix in self.IMAGE_EXTENSIONS:
                classification = "IMAGE"
            elif suffix == ".rpp":
                classification = "REAPER_PROJECT"
            elif suffix in self.MEDIA_EXTENSIONS:
                classification = "MEDIA_UNKNOWN"
                if self.probe:
                    try:
                        probe = self.probe(path)
                        streams = probe.get("streams", []) if isinstance(probe, dict) else []
                        has_video = any(item.get("codec_type") == "video" for item in streams if isinstance(item, dict))
                        has_audio = any(item.get("codec_type") == "audio" for item in streams if isinstance(item, dict))
                        classification = "VIDEO" if has_video else ("AUDIO" if has_audio else "MEDIA_UNKNOWN")
                        details = {"has_video": has_video, "has_audio": has_audio}
                    except Exception as exc:
                        details = {"probe_error": str(exc)}
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            results.append(DiscoveredFile(str(path), classification, size, details))
        return results


class ManifestMigrator:
    def migrate(
        self,
        existing: ManifestLoadResult,
        *,
        video_id: str,
        source_url: str,
        title: str,
        project_path: Path,
        author_preset: str,
        job_id: str,
        discovered_files: List[DiscoveredFile],
        reaper_proxy_height: int = 720,
        preset_id: str = "",
        channel_id: str = "",
        uploader_id: str = "",
        channel_name: str = "",
    ) -> ProjectManifest:
        raw = existing.raw if isinstance(existing.raw, dict) else {}
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        files: Dict[str, Any] = raw.get("files") if isinstance(raw.get("files"), dict) else {}
        if not files:
            files = {
                f"discovered_{index:03d}": {
                    "path": item.path,
                    "classification": item.classification,
                    "size": item.size,
                }
                for index, item in enumerate(discovered_files, 1)
            }
        return ProjectManifest(
            schema_version=CURRENT_SCHEMA_VERSION,
            video_id=video_id or str(raw.get("video_id") or ""),
            source_url=source_url or str(raw.get("source_url") or raw.get("url") or ""),
            title=title or str(raw.get("title") or ""),
            project_path=str(project_path),
            materials_path=str(project_path / "Материалы"),
            author_preset=author_preset,
            preset_id=preset_id or str(raw.get("preset_id") or ""),
            channel_id=channel_id or str(raw.get("channel_id") or ""),
            uploader_id=uploader_id or str(raw.get("uploader_id") or ""),
            channel_name=channel_name or str(raw.get("channel_name") or raw.get("channel") or raw.get("uploader") or ""),
            job_id=job_id or str(raw.get("job_id") or ""),
            state="DISCOVERED",
            stages=raw.get("stages") if isinstance(raw.get("stages"), dict) else {},
            files=files,
            discovered_files=[asdict(item) for item in discovered_files],
            created_at=str(raw.get("created_at") or now),
            updated_at=now,
            reaper_proxy_height=reaper_proxy_height if reaper_proxy_height in {480, 720, 1080} else 720,
        )
