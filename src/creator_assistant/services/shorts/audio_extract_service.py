from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessRunner


class TranscriptionAudioService:
    def __init__(self, runner: ProcessRunner, ffmpeg_path: str) -> None:
        self.runner, self.ffmpeg_path = runner, ffmpeg_path

    def extract(self, source: Path, target: Path, cancellation: CancellationToken, on_line=None) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.stem + ".tmp" + target.suffix)
        self.runner.run([
            self.ffmpeg_path, "-hide_banner", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-progress", "pipe:1", "-nostats", str(temporary),
        ], cancellation=cancellation, on_line=on_line)
        temporary.replace(target)
        return target
