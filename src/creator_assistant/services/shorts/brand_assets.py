from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QImageReader

from creator_assistant.infrastructure.settings_store import shared_brand_assets_root


class BrandAssetType(str, Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    ANIMATED_IMAGE = "ANIMATED_IMAGE"


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
ANIMATED_EXTENSIONS = {".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | ANIMATED_EXTENSIONS | VIDEO_EXTENSIONS


@dataclass(frozen=True)
class BrandAsset:
    asset_id: str
    asset_type: str
    stored_filename: str
    original_name: str
    content_hash: str
    size: int
    width: int = 0
    height: int = 0
    duration: float = 0.0
    codec: str = ""
    container: str = ""
    has_alpha: bool = False
    has_audio: bool = False
    preview_filename: str = ""
    created_at: str = ""


class BrandAssetError(ValueError):
    pass


class BrandAssetLibrary:
    SCHEMA_VERSION = 1

    def __init__(
        self,
        root: Path | None = None,
        *,
        ffprobe_path: str = "",
        ffmpeg_path: str = "",
        probe: Callable[[Path], dict[str, Any]] | None = None,
    ) -> None:
        self.root = Path(root or shared_brand_assets_root())
        self.originals_root = self.root / "originals"
        self.previews_root = self.root / "previews"
        self.profiles_root = self.root / "profiles"
        self.manifest_path = self.root / "manifest.json"
        self.migration_report_path = self.root / "migration-report.json"
        self.ffprobe_path = str(ffprobe_path or shutil.which("ffprobe") or "")
        self.ffmpeg_path = str(ffmpeg_path or shutil.which("ffmpeg") or "")
        self._probe = probe
        self._lock = RLock()

    def ensure_root(self) -> Path:
        for path in (self.root, self.originals_root, self.previews_root, self.profiles_root):
            path.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            self._write_manifest({"schema_version": self.SCHEMA_VERSION, "assets": []})
        return self.root

    def assets(self) -> list[BrandAsset]:
        data = self._read_manifest()
        result: list[BrandAsset] = []
        for raw in data.get("assets", []):
            try:
                result.append(BrandAsset(**{key: value for key, value in raw.items() if key in BrandAsset.__dataclass_fields__}))
            except (TypeError, ValueError):
                continue
        return result

    def get(self, asset_id: str) -> BrandAsset | None:
        key = str(asset_id or "").casefold()
        return next((item for item in self.assets() if item.asset_id.casefold() == key), None)

    def path_for(self, asset: BrandAsset | str | None) -> Path | None:
        if isinstance(asset, str):
            asset = self.get(asset)
        if not asset:
            return None
        path = self.originals_root / asset.stored_filename
        return path if path.is_file() else None

    def preview_path(self, asset: BrandAsset | str | None) -> Path | None:
        if isinstance(asset, str):
            asset = self.get(asset)
        if not asset or not asset.preview_filename:
            return None
        path = self.previews_root / asset.preview_filename
        return path if path.is_file() else None

    def import_asset(self, source: Path) -> BrandAsset:
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(source)
        extension = source.suffix.casefold()
        if extension not in SUPPORTED_EXTENSIONS:
            raise BrandAssetError(f"Неподдерживаемый формат бренд-материала: {extension or 'без расширения'}")
        digest = _sha256(source)
        with self._lock:
            self.ensure_root()
            existing = next((item for item in self.assets() if item.content_hash == digest), None)
            if existing and self.path_for(existing):
                return existing
            metadata = self._inspect(source, extension)
            kind = BrandAssetType(metadata.pop("asset_type"))
            asset_id = f"brand_{kind.value.casefold()}_{digest[:16]}"
            filename = f"{asset_id}{extension}"
            target = self.originals_root / filename
            if not target.exists():
                temporary = target.with_suffix(target.suffix + ".tmp")
                shutil.copy2(source, temporary)
                os.replace(str(temporary), str(target))
            preview = self._create_preview(target, asset_id, kind)
            asset = BrandAsset(
                asset_id=asset_id,
                asset_type=kind.value,
                stored_filename=filename,
                original_name=source.name,
                content_hash=digest,
                size=source.stat().st_size,
                preview_filename=preview.name if preview else "",
                created_at=datetime.now(timezone.utc).isoformat(),
                **metadata,
            )
            data = self._read_manifest()
            values = [item for item in data.get("assets", []) if item.get("asset_id") != asset.asset_id]
            values.append(asdict(asset))
            data.update({"schema_version": self.SCHEMA_VERSION, "assets": values})
            self._write_manifest(data)
            return asset

    def verify(self, asset_id: str) -> tuple[bool, str]:
        asset = self.get(asset_id)
        path = self.path_for(asset)
        if not asset or not path:
            return False, "Файл бренд-материала отсутствует"
        if _sha256(path) != asset.content_hash:
            return False, "Hash бренд-материала не совпадает с manifest"
        try:
            inspected = self._inspect(path, path.suffix.casefold())
        except (BrandAssetError, OSError, ValueError) as exc:
            return False, str(exc)
        if inspected.get("asset_type") != asset.asset_type:
            return False, "Тип фактического файла не совпадает с manifest"
        return True, "Материал проверен"

    def _inspect(self, source: Path, extension: str) -> dict[str, Any]:
        if extension in IMAGE_EXTENSIONS | ANIMATED_EXTENSIONS:
            reader = QImageReader(str(source))
            if not reader.canRead():
                raise BrandAssetError("Файл не является поддерживаемым изображением")
            size = reader.size()
            animated = extension == ".gif" or bool(reader.supportsAnimation())
            image = reader.read()
            if image.isNull():
                raise BrandAssetError("Изображение повреждено или не декодируется")
            return {
                "asset_type": BrandAssetType.ANIMATED_IMAGE.value if animated else BrandAssetType.IMAGE.value,
                "width": int(size.width() or image.width()),
                "height": int(size.height() or image.height()),
                "duration": 0.0,
                "codec": extension.lstrip("."),
                "container": extension.lstrip("."),
                "has_alpha": bool(image.hasAlphaChannel()),
                "has_audio": False,
            }
        data = self._probe(source) if self._probe else self._run_ffprobe(source)
        streams = list(data.get("streams") or [])
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        if not video:
            raise BrandAssetError("FFprobe не обнаружил видеопоток")
        fmt = data.get("format") or {}
        format_name = str(fmt.get("format_name") or "")
        allowed = {
            ".mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
            ".mov": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
            ".webm": {"matroska", "webm"},
        }[extension]
        if not any(value in allowed for value in format_name.split(",")):
            raise BrandAssetError(f"Фактический контейнер {format_name or 'неизвестен'} не соответствует {extension}")
        pixel_format = str(video.get("pix_fmt") or "").casefold()
        has_alpha = any(marker in pixel_format for marker in ("rgba", "bgra", "argb", "abgr", "yuva", "gbrap"))
        return {
            "asset_type": BrandAssetType.VIDEO.value,
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "duration": float(fmt.get("duration") or video.get("duration") or 0),
            "codec": str(video.get("codec_name") or ""),
            "container": format_name,
            "has_alpha": has_alpha,
            "has_audio": any(item.get("codec_type") == "audio" for item in streams),
        }

    def _run_ffprobe(self, source: Path) -> dict[str, Any]:
        if not self.ffprobe_path:
            raise BrandAssetError("FFprobe не найден; видео нельзя безопасно проверить")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [self.ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=flags,
        )
        if completed.returncode:
            raise BrandAssetError(f"FFprobe отклонил материал: {completed.stderr.strip() or completed.returncode}")
        try:
            return json.loads(completed.stdout)
        except ValueError as exc:
            raise BrandAssetError("FFprobe вернул некорректные метаданные") from exc

    def _create_preview(self, source: Path, asset_id: str, kind: BrandAssetType) -> Path | None:
        target = self.previews_root / f"{asset_id}.png"
        if kind in {BrandAssetType.IMAGE, BrandAssetType.ANIMATED_IMAGE}:
            reader = QImageReader(str(source))
            image = reader.read()
            preview = image.scaled(640, 360, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if not image.isNull() and preview.save(str(target), "PNG"):
                return target
            return None
        if not self.ffmpeg_path:
            return None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [self.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-ss", "0", "-i", str(source), "-frames:v", "1", "-vf", "scale=640:-2", str(target)],
            capture_output=True, timeout=60, creationflags=flags,
        )
        return target if completed.returncode == 0 and target.is_file() else None

    def _read_manifest(self) -> dict[str, Any]:
        self.ensure_root()
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            raw = {}
        return raw if isinstance(raw, dict) else {}

    def _write_manifest(self, value: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(self.manifest_path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
