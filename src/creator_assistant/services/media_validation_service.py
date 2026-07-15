from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from creator_assistant.domain.errors import ValidationError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessRunner


class MediaValidationService:
    def __init__(self, runner: ProcessRunner, ffprobe_path: str) -> None:
        self.runner = runner
        self.ffprobe_path = ffprobe_path

    def probe(self, path: Path, cancellation: Optional[CancellationToken] = None) -> Dict[str, Any]:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValidationError(f"Файл отсутствует или пуст: {path}")
        result = self.runner.run(
            [
                self.ffprobe_path,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            cancellation=cancellation,
            timeout=60,
        )
        try:
            return json.loads(result.stdout or result.output)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"FFprobe не смог проверить файл: {path}") from exc

    def validate_video(self, path: Path, cancellation: CancellationToken) -> Dict[str, Any]:
        data = self.probe(path, cancellation)
        if not any(stream.get("codec_type") == "video" for stream in data.get("streams", [])):
            raise ValidationError(f"В файле нет видеопотока: {path.name}")
        return data

    def validate_audio(self, path: Path, cancellation: CancellationToken) -> Dict[str, Any]:
        data = self.probe(path, cancellation)
        if not any(stream.get("codec_type") == "audio" for stream in data.get("streams", [])):
            raise ValidationError(f"В файле нет аудиопотока: {path.name}")
        return data

    def validate_expected_video(
        self,
        path: Path,
        cancellation: Optional[CancellationToken],
        *,
        duration: Optional[float] = None,
        height: Optional[int] = None,
        fps: Optional[float] = None,
        require_audio: bool = True,
        maximum_height: Optional[int] = None,
        require_sdr: bool = True,
    ) -> Dict[str, Any]:
        data = self.probe(path, cancellation)
        streams = data.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if not video:
            raise ValidationError(f"В файле нет видеопотока: {path.name}")
        if require_audio and not any(stream.get("codec_type") == "audio" for stream in streams):
            raise ValidationError(f"В файле нет аудиопотока: {path.name}")
        actual_height = self._integer(video.get("height"))
        if height and actual_height != height:
            raise ValidationError(f"Неверное разрешение {actual_height or '?'}p, ожидалось {height}p: {path.name}")
        if maximum_height and actual_height and actual_height > maximum_height:
            raise ValidationError(f"Разрешение {actual_height}p выше допустимых {maximum_height}p: {path.name}")
        actual_fps = self._rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        if fps and actual_fps and abs(actual_fps - fps) > 0.15:
            raise ValidationError(f"Неверный FPS {actual_fps:.3f}, ожидалось {fps:.3f}: {path.name}")
        transfer = str(video.get("color_transfer") or "").casefold()
        if require_sdr and transfer in {"smpte2084", "arib-std-b67"}:
            raise ValidationError(f"Найден HDR/HLG вместо SDR: {path.name}")
        self._validate_duration(data, duration, path)
        return data

    def validate_maximum_mp4(
        self,
        path: Path,
        cancellation: Optional[CancellationToken],
        *,
        duration: Optional[float] = None,
        height: Optional[int] = None,
        fps: Optional[float] = None,
        require_sdr: bool = True,
    ) -> Dict[str, Any]:
        data = self.validate_expected_video(
            path,
            cancellation,
            duration=duration,
            height=height,
            fps=fps,
            require_audio=True,
            require_sdr=require_sdr,
        )
        format_name = str((data.get("format") or {}).get("format_name") or "").casefold()
        if "mp4" not in format_name:
            raise ValidationError(f"MAX РІРёРґРµРѕ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ РЅР°СЃС‚РѕСЏС‰РёРј MP4-РєРѕРЅС‚РµР№РЅРµСЂРѕРј: {path.name}")
        return data

    def validate_expected_audio(
        self,
        path: Path,
        cancellation: Optional[CancellationToken],
        *,
        duration: Optional[float] = None,
        require_flac: bool = False,
    ) -> Dict[str, Any]:
        data = self.probe(path, cancellation)
        streams = data.get("streams", [])
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            raise ValidationError(f"В файле нет аудиопотока: {path.name}")
        if require_flac:
            codecs = {str(stream.get("codec_name") or "").casefold() for stream in streams if stream.get("codec_type") == "audio"}
            if path.suffix.casefold() != ".flac" or "flac" not in codecs:
                raise ValidationError(f"Инструментал не является корректным FLAC: {path.name}")
        self._validate_duration(data, duration, path)
        return data

    def _validate_duration(self, data: Dict[str, Any], expected: Optional[float], path: Path) -> None:
        actual = self.duration(data)
        if expected and actual and abs(actual - expected) > max(3.0, expected * 0.01):
            raise ValidationError(f"Длительность {actual:.2f} с не совпадает с ожидаемой {expected:.2f} с: {path.name}")

    @staticmethod
    def _integer(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _rate(value: Any) -> Optional[float]:
        try:
            text = str(value)
            if "/" in text:
                numerator, denominator = text.split("/", 1)
                return float(numerator) / float(denominator) if float(denominator) else None
            return float(text)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @staticmethod
    def duration(data: Dict[str, Any]) -> Optional[float]:
        try:
            return float(data.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            return None
