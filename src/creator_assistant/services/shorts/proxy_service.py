import os
import time
import uuid
from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessRunner


class AnalysisProxyService:
    def __init__(self, runner: ProcessRunner, ffmpeg_path: str, nvenc: bool = False) -> None:
        self.runner, self.ffmpeg_path, self.nvenc = runner, ffmpeg_path, nvenc

    def create(self, source: Path, target: Path, cancellation: CancellationToken, on_line=None) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp{target.suffix}")
        codec = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "24", "-b:v", "0"] if self.nvenc else ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
        command = [self.ffmpeg_path, "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:min(720\\,ih)", *codec, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-fps_mode", "passthrough", "-progress", "pipe:1", "-nostats", str(temporary)]
        try:
            self.runner.run(command, cancellation=cancellation, on_line=on_line)
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise RuntimeError("FFmpeg не создал валидный analysis proxy.")
            self._replace_with_retry(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return target

    @staticmethod
    def _replace_with_retry(temporary: Path, target: Path) -> None:
        last_error: OSError | None = None
        for attempt in range(8):
            try:
                os.replace(str(temporary), str(target))
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.12 * (attempt + 1))
        message = (
            f"Не удалось заменить analysis proxy: файл занят другим процессом ({target}). "
            "Закройте проигрыватель/preview и повторите анализ."
        )
        raise PermissionError(message) from last_error
