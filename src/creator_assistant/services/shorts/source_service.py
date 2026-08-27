from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from creator_assistant.domain.shorts.errors import InvalidSourceError
from creator_assistant.domain.shorts.models import SourceInfo
from creator_assistant.infrastructure.process_runner import ProcessRunner


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi"}
EXCLUDED_HINTS = ("[max ", "[480p]", "[720p]", "[1080p]", "preview", "proxy")
FILE_HASH_CHUNK_SIZE = 8 * 1024 * 1024


def parse_fraction(value: str) -> float:
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator) if float(denominator) else 0.0
    except (AttributeError, ValueError, ZeroDivisionError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def source_fingerprint(source: SourceInfo) -> str:
    stable = {
        "path": str(Path(source.path).resolve()).casefold(),
        "size": source.size,
        "mtime_ns": int(source.mtime * 1_000_000_000),
        "duration_ms": round(source.duration * 1000),
        "width": source.width,
        "height": source.height,
        "fps_milli": round(source.fps * 1000),
        "video_codec": source.video_codec,
        "audio_codec": source.audio_codec,
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode("utf-8")).hexdigest()


def source_content_fingerprint(path: Path) -> str:
    """Return a rename- and copy-stable identity for FREE source accounting."""
    digest = hashlib.sha256()
    with path.resolve().open("rb") as stream:
        while chunk := stream.read(FILE_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def quota_source_fingerprint(source: SourceInfo) -> str:
    """Resolve the content identity used only by the server quota gate."""
    return source.content_fingerprint or source_content_fingerprint(Path(source.path))


class ShortsSourceService:
    def __init__(self, runner: ProcessRunner, ffprobe_path: str) -> None:
        self.runner = runner
        self.ffprobe_path = ffprobe_path

    def probe(self, path: Path) -> SourceInfo:
        path = path.resolve()
        if not path.is_file():
            raise InvalidSourceError("Выбранный видеофайл не существует.")
        if not self.ffprobe_path:
            raise InvalidSourceError("FFprobe не найден. Укажите путь в настройках.")
        result = self.runner.run([
            self.ffprobe_path, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ], timeout=60)
        try:
            data: Dict[str, Any] = json.loads(result.stdout or result.output)
        except (TypeError, ValueError) as exc:
            raise InvalidSourceError("FFprobe вернул некорректные сведения об исходнике.") from exc
        streams: Iterable[Dict[str, Any]] = data.get("streams") or []
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if not video:
            raise InvalidSourceError("В выбранном файле нет видеопотока.")
        if not audio:
            raise InvalidSourceError("В выбранном файле нет аудиопотока.")
        try:
            duration = float((data.get("format") or {}).get("duration") or video.get("duration"))
        except (TypeError, ValueError) as exc:
            raise InvalidSourceError("Не удалось определить длительность видео.") from exc
        if duration <= 0:
            raise InvalidSourceError("Длительность видео должна быть больше нуля.")
        tags = video.get("tags") or {}
        side_data = video.get("side_data_list") or []
        rotation = tags.get("rotate", 0)
        for entry in side_data:
            if "rotation" in entry:
                rotation = entry["rotation"]
        transfer = str(video.get("color_transfer") or "").casefold()
        dynamic_range = "HDR" if transfer in {"smpte2084", "arib-std-b67"} else "SDR"
        stat = path.stat()
        info = SourceInfo(
            path=str(path), name=path.name, size=stat.st_size, mtime=stat.st_mtime,
            duration=duration, width=int(video.get("width") or 0), height=int(video.get("height") or 0),
            fps=parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"),
            video_codec=str(video.get("codec_name") or ""), audio_codec=str(audio.get("codec_name") or ""),
            audio_channels=int(audio.get("channels") or 0), sample_rate=int(audio.get("sample_rate") or 0),
            dynamic_range=dynamic_range, rotation=int(float(rotation or 0)),
        )
        info.fingerprint = source_fingerprint(info)
        info.content_fingerprint = source_content_fingerprint(path)
        return info

    @staticmethod
    def project_video_candidates(folder: Path) -> List[Path]:
        if not folder.is_dir():
            return []
        files = [item for item in folder.iterdir() if item.is_file() and item.suffix.casefold() in VIDEO_EXTENSIONS]
        preferred = [item for item in files if not any(hint in item.name.casefold() for hint in EXCLUDED_HINTS)]
        return sorted(preferred or files, key=lambda item: item.stat().st_size, reverse=True)
