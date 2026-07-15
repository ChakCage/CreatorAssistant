from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from creator_assistant.domain.errors import DiskSpaceError, ProcessExecutionError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.errors import InvalidClipError
from creator_assistant.domain.shorts.models import Candidate, SourceInfo
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.shorts.filter_graph_builder import ShortsFilterGraphBuilder


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(value: str) -> str:
    cleaned = INVALID_FILENAME.sub("_", value).strip().rstrip(".")
    return cleaned[:180] or "Short"


def unique_output_path(folder: Path, source_title: str, index: int, custom_title: str = "") -> Path:
    stem = safe_filename(custom_title or f"{source_title} [Short {index:02d}]")
    candidate = folder / f"{stem}.mp4"
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}).mp4"
        number += 1
    return candidate


class ShortsRenderService:
    def __init__(self, runner: ProcessRunner, ffmpeg_path: str, ffprobe_path: str, prefer_nvenc: bool, reserve_bytes: int = 5 * 1024**3) -> None:
        self.runner, self.ffmpeg_path, self.ffprobe_path = runner, ffmpeg_path, ffprobe_path
        self.prefer_nvenc, self.reserve_bytes = prefer_nvenc, reserve_bytes
        self.builder = ShortsFilterGraphBuilder()
        self.last_encoder = ""
        self.current_speed = ""
        self.last_speed = ""
        self.last_elapsed = 0.0

    def build_command(self, source: SourceInfo, candidate: Candidate, subtitle: Path, target: Path, nvenc: bool) -> list[str]:
        branding = candidate.branding_settings or {}
        banner = Path(str(branding.get("channel_banner_path") or ""))
        has_banner = bool(branding.get("show_channel_card", False)) and banner.is_file()
        graph = self.builder.build(
            candidate,
            source,
            subtitle.name if subtitle.is_file() else "",
            input_clipped=True,
            has_channel_banner=has_banner,
        )
        video = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-b:v", "0"] if nvenc else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19"]
        command = [
            self.ffmpeg_path, "-hide_banner", "-y", "-ss", f"{candidate.start:.3f}", "-i", source.path,
        ]
        if has_banner:
            command.extend(["-loop", "1", "-i", str(banner)])
        command.extend([
            "-t", f"{candidate.duration:.3f}", "-filter_complex_threads", "0", "-filter_complex", graph,
            "-map", "[v]", "-map", "[a]", *video, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
            "-fps_mode", "vfr", "-shortest",
            "-progress", "pipe:1", "-nostats", str(target),
        ])
        return command

    def render(self, source: SourceInfo, candidate: Candidate, subtitle: Path, target: Path, cancellation: CancellationToken, on_progress: Optional[Callable[[float], None]] = None) -> Path:
        if candidate.end <= candidate.start or candidate.start < 0 or candidate.end > source.duration + 0.05:
            raise InvalidClipError("Границы Short выходят за пределы исходника.")
        target.parent.mkdir(parents=True, exist_ok=True)
        required = max(200 * 1024**2, int(candidate.duration * 12_000_000 / 8 * 2))
        free = shutil.disk_usage(target.parent).free
        if free < required + self.reserve_bytes:
            raise DiskSpaceError("Рендер Short", target.parent, required, free, self.reserve_bytes)
        temporary = target.with_name(target.stem + ".rendering" + target.suffix)
        temporary.unlink(missing_ok=True)
        started = time.monotonic()
        self.current_speed = ""

        def run(nvenc: bool) -> None:
            values = {}

            def parse(line: str) -> None:
                if "=" not in line:
                    return
                key, value = line.split("=", 1)
                values[key] = value
                if key == "speed":
                    self.current_speed = value.strip()
                if not on_progress:
                    return
                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        microseconds = float(value)
                        on_progress(max(0.0, min(100.0, microseconds / 1_000_000 / candidate.duration * 100)))
                    except ValueError:
                        pass
                elif key == "progress" and value == "end":
                    on_progress(100.0)

            command = self.build_command(source, candidate, subtitle, temporary, nvenc)
            logging.getLogger("creator_assistant").info(
                "Short render command=%s source_fps=%.3f duration=%.3f encoder=%s",
                subprocess.list2cmdline(command), source.fps, candidate.duration,
                "h264_nvenc" if nvenc else "libx264",
            )
            self.runner.run(command, cancellation=cancellation, on_line=parse, cwd=subtitle.parent)

        try:
            run(self.prefer_nvenc)
            self.last_encoder = "h264_nvenc" if self.prefer_nvenc else "libx264"
        except ProcessExecutionError:
            if not self.prefer_nvenc or cancellation.is_cancelled:
                temporary.unlink(missing_ok=True)
                raise
            temporary.unlink(missing_ok=True)
            run(False)
            self.last_encoder = "libx264"
        self.validate(temporary, candidate.duration, cancellation)
        os.replace(str(temporary), str(target))
        self.last_elapsed = time.monotonic() - started
        self.last_speed = self.current_speed or (f"{candidate.duration / self.last_elapsed:.2f}x" if self.last_elapsed else "")
        logging.getLogger("creator_assistant").info(
            "Short render complete elapsed=%.2fs fps=%.3f speed=%s encoder=%s",
            self.last_elapsed, source.fps, self.last_speed, self.last_encoder,
        )
        return target

    def validate(self, path: Path, expected_duration: float, cancellation: CancellationToken) -> dict:
        result = self.runner.run([
            self.ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
        ], cancellation=cancellation, timeout=60)
        data = json.loads(result.stdout or result.output)
        video = next((item for item in data.get("streams", []) if item.get("codec_type") == "video"), None)
        audio = next((item for item in data.get("streams", []) if item.get("codec_type") == "audio"), None)
        duration = float((data.get("format") or {}).get("duration") or 0)
        if not video or (int(video.get("width", 0)), int(video.get("height", 0))) != (1080, 1920):
            raise InvalidClipError("FFprobe не подтвердил вертикальный кадр 1080×1920.")
        if video.get("codec_name") != "h264" or not audio or audio.get("codec_name") != "aac":
            raise InvalidClipError("FFprobe не подтвердил H.264/AAC в итоговом MP4.")
        if abs(duration - expected_duration) > max(1.0, expected_duration * 0.03):
            raise InvalidClipError("Длительность итогового Short не совпадает с выбранными границами.")
        return data
