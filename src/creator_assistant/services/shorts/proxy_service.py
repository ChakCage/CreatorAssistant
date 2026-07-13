from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessRunner


class AnalysisProxyService:
    def __init__(self, runner: ProcessRunner, ffmpeg_path: str, nvenc: bool = False) -> None:
        self.runner, self.ffmpeg_path, self.nvenc = runner, ffmpeg_path, nvenc

    def create(self, source: Path, target: Path, cancellation: CancellationToken, on_line=None) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.stem + ".tmp" + target.suffix)
        codec = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "24", "-b:v", "0"] if self.nvenc else ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
        command = [self.ffmpeg_path, "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:min(720\\,ih)", *codec, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-fps_mode", "passthrough", "-progress", "pipe:1", "-nostats", str(temporary)]
        self.runner.run(command, cancellation=cancellation, on_line=on_line)
        temporary.replace(target)
        return target
