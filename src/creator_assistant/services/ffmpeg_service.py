from __future__ import annotations

import os
import hashlib
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import ProgressInfo, ProxyFpsPolicy
from creator_assistant.domain.progress import format_bytes
from creator_assistant.infrastructure.process_runner import ProcessRunner


class FfmpegService:
    _proxy_lock = threading.Lock()
    _active_proxy_keys: set[str] = set()

    def __init__(self, runner: ProcessRunner, ffmpeg_path: str, nvenc_available: bool = False, ffprobe_path: str = "") -> None:
        self.runner = runner
        self.ffmpeg_path = ffmpeg_path
        self.nvenc_available = nvenc_available
        self.ffprobe_path = ffprobe_path

    def build_proxy_command(
        self,
        source: Path,
        target: Path,
        transcode_video: bool = True,
        maximum_height: int = 720,
        fps_policy: ProxyFpsPolicy = ProxyFpsPolicy.PRESERVE,
        source_fps: Optional[float] = None,
    ) -> List[str]:
        command = [self.ffmpeg_path, "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?"]
        if transcode_video:
            filters = [f"scale=-2:min({maximum_height}\\,ih)"]
            if fps_policy == ProxyFpsPolicy.EXACT_30:
                filters.append("fps=30")
            elif fps_policy == ProxyFpsPolicy.CAP_30 and source_fps and source_fps > 30.05:
                target_rate = (
                    "30000/1001"
                    if abs(source_fps - (60000 / 1001)) < 0.01
                    else "30"
                )
                filters.append(f"fps={target_rate}")
            command.extend(["-vf", ",".join(filters)])
            if self.nvenc_available:
                command.extend(["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "20", "-b:v", "0"])
            else:
                command.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "18"])
        else:
            command.extend(["-c:v", "copy"])
        if transcode_video:
            command.extend(["-c:a", "aac", "-b:a", "192k"])
        else:
            command.extend(["-c:a", "copy"])
        fps_mode = "passthrough" if fps_policy == ProxyFpsPolicy.PRESERVE else "cfr"
        command.extend(["-fps_mode", fps_mode, "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(target)])
        return command

    def create_proxy(
        self,
        source: Path,
        target: Path,
        cancellation: CancellationToken,
        transcode_video: bool = True,
        on_progress: Optional[Callable[[ProgressInfo], None]] = None,
        stage: str = "Создание видео-прокси",
        maximum_height: int = 720,
        fps_policy: ProxyFpsPolicy = ProxyFpsPolicy.PRESERVE,
        source_fps: Optional[float] = None,
        environment: Optional[Dict[str, str]] = None,
        cwd: Optional[Path] = None,
    ) -> Path:
        temporary = target.with_name(target.stem + ".tmp" + target.suffix)
        duration = self._duration(source)
        values = {}
        proxy_key = self.proxy_key(
            source,
            maximum_height,
            fps_policy,
            source_fps,
        )

        def parse(line: str) -> None:
            if "=" not in line:
                return
            key, value = line.strip().split("=", 1)
            values[key] = value
            if key not in ("progress", "out_time_us", "out_time_ms") or not on_progress:
                return
            microseconds = self._number(values.get("out_time_us"))
            if microseconds is None:
                microseconds = self._number(values.get("out_time_ms"))
            percent = None
            if duration and microseconds is not None:
                percent = self.progress_percent(microseconds, duration)
            if value == "end" or values.get("progress") == "end":
                percent = 100.0
            total_size = self._integer(values.get("total_size"))
            speed = values.get("speed", "")
            on_progress(
                ProgressInfo(
                    stage,
                    "Перекодирование через FFmpeg" if transcode_video else "Объединение потоков",
                    percent,
                    speed=speed,
                    downloaded=format_bytes(total_size),
                    downloaded_bytes=total_size,
                    substage_name="FFmpeg",
                )
            )

        with self._proxy_lock:
            if proxy_key in self._active_proxy_keys:
                raise RuntimeError(
                    "Создание этого proxy уже выполняется; повторный job не запущен."
                )
            self._active_proxy_keys.add(proxy_key)
        try:
            self.runner.run(
                self.build_proxy_command(
                    source,
                    temporary,
                    transcode_video,
                    maximum_height,
                    fps_policy,
                    source_fps,
                ),
                cancellation=cancellation,
                on_line=parse,
                environment=environment,
                cwd=cwd,
            )
            os.replace(str(temporary), str(target))
        finally:
            with self._proxy_lock:
                self._active_proxy_keys.discard(proxy_key)
        return target

    @staticmethod
    def proxy_key(
        source: Path,
        maximum_height: int,
        fps_policy: ProxyFpsPolicy,
        source_fps: Optional[float],
        *,
        codec_policy: str = "h264-reaper",
        pixel_format: str = "auto",
        profile_version: int = 1,
    ) -> str:
        try:
            stat = source.stat()
            fingerprint = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        except OSError:
            fingerprint = str(source.resolve())
        payload = (
            f"{fingerprint}|{maximum_height}|{codec_policy}|{fps_policy.value}|"
            f"{source_fps or 0:.6f}|{pixel_format}|{profile_version}"
        )
        return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()

    def remux_maximum_to_mp4(
        self,
        source: Path,
        target: Path,
        cancellation: CancellationToken,
        *,
        audio_codec: str = "",
        transcode_video: bool = False,
        on_progress: Optional[Callable[[ProgressInfo], None]] = None,
        stage: str = "Подготовка MAX MP4",
        environment: Optional[Dict[str, str]] = None,
        cwd: Optional[Path] = None,
    ) -> Path:
        temporary = target.with_name(target.stem + ".tmp" + target.suffix)
        duration = self._duration(source)
        values = {}
        audio_is_aac = audio_codec.casefold().startswith(("aac", "mp4a"))
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
        ]
        if transcode_video:
            if self.nvenc_available:
                command.extend(["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "18", "-b:v", "0"])
            else:
                command.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "18"])
        else:
            command.extend(["-c:v", "copy"])
        if audio_is_aac:
            command.extend(["-c:a", "copy"])
        else:
            command.extend(["-c:a", "aac", "-b:a", "192k"])
        command.extend(["-fps_mode", "passthrough", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(temporary)])

        def parse(line: str) -> None:
            if "=" not in line:
                return
            key, value = line.strip().split("=", 1)
            values[key] = value
            if key not in ("progress", "out_time_us", "out_time_ms") or not on_progress:
                return
            microseconds = self._number(values.get("out_time_us"))
            if microseconds is None:
                microseconds = self._number(values.get("out_time_ms"))
            percent = None
            if duration and microseconds is not None:
                percent = self.progress_percent(microseconds, duration)
            if value == "end" or values.get("progress") == "end":
                percent = 100.0
            total_size = self._integer(values.get("total_size"))
            on_progress(
                ProgressInfo(
                    stage,
                    "Remux MP4 без перекодирования видео" if not transcode_video else "Аварийное перекодирование видео для MP4",
                    percent,
                    speed=values.get("speed", ""),
                    downloaded=format_bytes(total_size),
                    downloaded_bytes=total_size,
                    substage_name="FFmpeg",
                )
            )

        self.runner.run(
            command,
            cancellation=cancellation,
            on_line=parse,
            environment=environment,
            cwd=cwd,
        )
        os.replace(str(temporary), str(target))
        return target

    def _duration(self, source: Path) -> Optional[float]:
        if not self.ffprobe_path:
            return None
        try:
            result = self.runner.run(
                [self.ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                timeout=30,
            )
            return float(result.output.strip().splitlines()[0])
        except (OSError, ValueError, IndexError, Exception):
            return None

    @staticmethod
    def _number(value: Optional[str]) -> Optional[float]:
        try:
            return float(value) if value not in (None, "N/A", "NA") else None
        except ValueError:
            return None

    @staticmethod
    def _integer(value: Optional[str]) -> Optional[int]:
        number = FfmpegService._number(value)
        return int(number) if number is not None else None

    @staticmethod
    def progress_percent(out_time_us: float, duration_seconds: float) -> Optional[float]:
        if duration_seconds <= 0:
            return None
        return max(0.0, min(100.0, out_time_us / 1_000_000.0 / duration_seconds * 100.0))

    def convert_for_uvr(self, source: Path, target: Path, cancellation: CancellationToken, environment=None, cwd=None) -> Path:
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "pcm_s24le",
            str(target),
        ]
        self.runner.run(command, cancellation=cancellation, environment=environment, cwd=cwd)
        return target
