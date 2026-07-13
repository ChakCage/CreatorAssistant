from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import SubtitleCue, Transcript
from creator_assistant.services.shorts.transcription.base import TranscriptionBackend


def srt_timestamp(seconds: float, separator: str = ",") -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


class TranscriptionService:
    def __init__(self, backend: TranscriptionBackend) -> None:
        self.backend = backend

    def transcribe(self, audio: Path, analysis_dir: Path, cancellation: CancellationToken, on_line=None) -> Transcript:
        raw_dir = analysis_dir / ".whisper"
        transcript = self.backend.transcribe(audio, raw_dir, cancellation, on_line)
        self.write_files(transcript, analysis_dir)
        return transcript

    @staticmethod
    def write_files(transcript: Transcript, analysis_dir: Path) -> None:
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "transcript.json").write_text(json.dumps(asdict(transcript), ensure_ascii=False, indent=2), encoding="utf-8")
        (analysis_dir / "transcript.txt").write_text(transcript.text + "\n", encoding="utf-8")
        srt = []
        vtt = ["WEBVTT", ""]
        for number, segment in enumerate(transcript.segments, 1):
            srt.extend([str(number), f"{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}", segment.text, ""])
            vtt.extend([f"{srt_timestamp(segment.start, '.')} --> {srt_timestamp(segment.end, '.')}", segment.text, ""])
        (analysis_dir / "transcript.srt").write_text("\n".join(srt), encoding="utf-8")
        (analysis_dir / "transcript.vtt").write_text("\n".join(vtt), encoding="utf-8")

    @staticmethod
    def load(path: Path) -> Transcript:
        data = json.loads(path.read_text(encoding="utf-8"))
        from creator_assistant.domain.shorts.models import TranscriptSegment, TranscriptWord
        segments = []
        for item in data.get("segments", []):
            words = [TranscriptWord(**word) for word in item.pop("words", [])]
            segments.append(TranscriptSegment(**item, words=words))
        return Transcript(**{**data, "segments": segments})
