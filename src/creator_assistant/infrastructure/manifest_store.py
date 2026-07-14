from __future__ import annotations

import datetime as dt
import ctypes
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


CURRENT_SCHEMA_VERSION = 2
METADATA_DIR_NAME = ".creator-assistant"
MANIFEST_NAME = ".creator-assistant/manifest.json"
LEGACY_MANIFEST_NAME = ".creator-assistant.json"
LEGACY_STATE_NAME = ".creator-assistant"

MATERIALS_DIRECTORY_NAMES = {"материалы", "materials", "source", "sources"}
EXCLUDED_DIRECTORY_NAMES = {
    "media", "backups", "backup", "reaper media", "recorded media", "peak files",
    "peaks", "peak", "waveforms", "waveform", "audio peaks", "autosave", "auto-save",
    "vegas auto save", "vegas backups", "rendered files", "renders", "render", "exports",
    "export", "output", "final", "готово", "shorts", "cache", ".cache", "temp", "tmp",
    "logs", "_test_output", "proxy cache", "waveform cache", "adobe", "capcut", "premiere",
    "davinci", "resolve", "recovery", "history", "versions", METADATA_DIR_NAME,
}
SIDECAR_SUFFIXES = {
    ".sfk", ".sfap0", ".sfap1", ".sfvp0", ".sfvp1", ".sfl", ".sfdecprop",
    ".reapeaks", ".pkf", ".peak", ".lck", ".lock", ".tmp", ".temp", ".part",
    ".ytdl", ".bak", ".autosave", ".undo", ".crdownload",
}
SIDECAR_ENDINGS = (".rpp-bak", ".veg.bak", ".veg~")


def project_metadata_dir(project_path: Path) -> Path:
    return project_path / METADATA_DIR_NAME


def project_manifest_path(project_path: Path) -> Path:
    return project_metadata_dir(project_path) / "manifest.json"


def is_ignored_sidecar(path: Path) -> bool:
    name = path.name.casefold()
    return path.suffix.casefold() in SIDECAR_SUFFIXES or name.endswith(SIDECAR_ENDINGS)


def is_excluded_directory(path: Path) -> bool:
    return path.name.casefold() in EXCLUDED_DIRECTORY_NAMES


def set_hidden_directory(path: Path) -> bool:
    if os.name != "nt" or not path.is_dir():
        return False
    try:
        get_attributes = ctypes.windll.kernel32.GetFileAttributesW
        set_attributes = ctypes.windll.kernel32.SetFileAttributesW
        attributes = get_attributes(str(path))
        if attributes == 0xFFFFFFFF:
            return False
        return bool(set_attributes(str(path), attributes | 0x2))
    except (AttributeError, OSError):
        return False


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
    reaper_proxies: Dict[str, Any] = field(default_factory=dict)
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
            reaper_proxies=(
                data.get("reaper_proxies")
                if isinstance(data.get("reaper_proxies"), dict)
                else {}
            ),
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
        requested = path
        project_path = path.parent.parent if path.name == "manifest.json" and path.parent.name == METADATA_DIR_NAME else path.parent
        candidates = (
            project_manifest_path(project_path),
            project_path / LEGACY_MANIFEST_NAME,
            project_path / LEGACY_STATE_NAME,
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), requested)
        if not path.is_file():
            return ManifestLoadResult(ManifestStatus.MISSING, requested, message="Manifest отсутствует.")
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
        project_path = Path(manifest.project_path)
        path = project_manifest_path(project_path)
        self._prepare_metadata_directory(project_path)
        temporary = path.with_name(path.name + ".tmp")
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = self._unique_backup(backup_dir / "manifest.json.bak")
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
            self._finish_legacy_manifest_migration(project_path)
            set_hidden_directory(path.parent)
            return saved
        except Exception:
            for candidate in (temporary, backup_temporary):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    @staticmethod
    def _unique_backup(path: Path) -> Path:
        if not path.exists():
            return path
        index = 2
        while True:
            candidate = path.with_name(f"{path.stem}.{index}{path.suffix}")
            if not candidate.exists():
                return candidate
            index += 1

    def _prepare_metadata_directory(self, project_path: Path) -> None:
        metadata_dir = project_metadata_dir(project_path)
        if metadata_dir.is_file():
            temporary = project_path / ".creator-assistant.legacy"
            if temporary.exists():
                raise OSError("Не удалось безопасно мигрировать legacy .creator-assistant: временный путь уже существует.")
            os.replace(str(metadata_dir), str(temporary))
            try:
                metadata_dir.mkdir(parents=False, exist_ok=False)
                backups = metadata_dir / "backups"
                backups.mkdir()
                backup = backups / "legacy-state"
                shutil.copy2(temporary, backup)
                if backup.read_bytes() != temporary.read_bytes():
                    raise OSError("Проверка backup legacy .creator-assistant не пройдена.")
                state = metadata_dir / "state.json"
                try:
                    parsed = json.loads(temporary.read_text(encoding="utf-8"))
                    state.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
                    json.loads(state.read_text(encoding="utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    # Нечитаемый legacy state всё равно полностью сохранён в backups.
                    pass
                temporary.unlink()
            except Exception:
                if metadata_dir.is_dir():
                    shutil.rmtree(metadata_dir, ignore_errors=True)
                if temporary.exists():
                    os.replace(str(temporary), str(project_path / LEGACY_STATE_NAME))
                raise
        else:
            metadata_dir.mkdir(parents=True, exist_ok=True)
        for name in ("logs", "temp", "backups"):
            (metadata_dir / name).mkdir(exist_ok=True)
        set_hidden_directory(metadata_dir)

    def _finish_legacy_manifest_migration(self, project_path: Path) -> None:
        legacy = project_path / LEGACY_MANIFEST_NAME
        if not legacy.is_file():
            return
        try:
            json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        backup = self._unique_backup(project_metadata_dir(project_path) / "backups" / LEGACY_MANIFEST_NAME)
        shutil.copy2(legacy, backup)
        if backup.read_bytes() != legacy.read_bytes():
            raise OSError("Проверка backup legacy manifest не пройдена.")
        legacy.unlink()


class LegacyProjectScanner:
    MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus"}
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

    def __init__(self, probe: Optional[Callable[[Path], Dict[str, Any]]] = None) -> None:
        self.probe = probe
        self.stats: Dict[str, int] = {}

    def scan(self, project_path: Path) -> List[DiscoveredFile]:
        results: List[DiscoveredFile] = []
        candidates, excluded_dirs, ignored_sidecars = self._allowed_files(project_path)
        probe_calls = 0
        ignored = {LEGACY_MANIFEST_NAME, "manifest.json", "manifest.json.bak", "manifest.json.tmp"}
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
                if self.probe and probe_calls < 20:
                    try:
                        probe_calls += 1
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
        self.stats = {
            "allowed_files": len(results),
            "excluded_directories": excluded_dirs,
            "ignored_sidecar_files": ignored_sidecars,
            "ffprobe_calls": probe_calls,
        }
        return results

    @staticmethod
    def _allowed_files(project_path: Path, *, include_material_subdirs: bool = False) -> tuple[List[Path], int, int]:
        files: List[Path] = []
        excluded_dirs = 0
        ignored_sidecars = 0
        material_dirs: List[Path] = []
        try:
            root_children = list(project_path.iterdir())
        except OSError:
            root_children = []
        for child in root_children:
            if child.is_file():
                if child.name.casefold() in {LEGACY_MANIFEST_NAME.casefold(), LEGACY_STATE_NAME.casefold()}:
                    continue
                if is_ignored_sidecar(child):
                    ignored_sidecars += 1
                else:
                    files.append(child)
            elif child.is_dir():
                if is_excluded_directory(child):
                    excluded_dirs += 1
                elif child.name.casefold() in MATERIALS_DIRECTORY_NAMES:
                    material_dirs.append(child)
                else:
                    excluded_dirs += 1
        for material_dir in material_dirs:
            try:
                children = list(material_dir.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_file():
                    if is_ignored_sidecar(child):
                        ignored_sidecars += 1
                    else:
                        files.append(child)
                elif child.is_dir():
                    if is_excluded_directory(child):
                        excluded_dirs += 1
                        continue
                    if not include_material_subdirs:
                        excluded_dirs += 1
                        continue
                    try:
                        nested = list(child.iterdir())
                    except OSError:
                        continue
                    for item in nested:
                        if not item.is_file():
                            continue
                        if is_ignored_sidecar(item):
                            ignored_sidecars += 1
                        else:
                            files.append(item)
        return files, excluded_dirs, ignored_sidecars


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
        reaper_proxies: Dict[str, Any] = (
            dict(raw.get("reaper_proxies"))
            if isinstance(raw.get("reaper_proxies"), dict)
            else {}
        )
        legacy_proxy = files.get("proxy") or files.get("reaper_proxy")
        if not reaper_proxies and isinstance(legacy_proxy, dict) and legacy_proxy.get("path"):
            actual_height = legacy_proxy.get("effective_height") or legacy_proxy.get("height")
            profile_key = str(actual_height) if str(actual_height) in {"480", "720", "1080"} else "legacy"
            reaper_proxies[profile_key] = {
                **legacy_proxy,
                "status": "VALID" if profile_key != "legacy" else "NEEDS_VALIDATION",
                "source": legacy_proxy.get("source") or "legacy_manifest",
            }
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
            reaper_proxies=reaper_proxies,
            discovered_files=[asdict(item) for item in discovered_files],
            created_at=str(raw.get("created_at") or now),
            updated_at=now,
            reaper_proxy_height=reaper_proxy_height if reaper_proxy_height in {480, 720, 1080} else 720,
        )
