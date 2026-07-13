from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import AudioFeatures
from creator_assistant.infrastructure.process_runner import ProcessRunner


SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")
MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?[0-9.]+) dB")


class AudioActivityService:
    def __init__(self, runner: ProcessRunner, ffmpeg_path: str) -> None:
        self.runner, self.ffmpeg_path = runner, ffmpeg_path

    def analyse(self, source: Path, duration: float, output: Path, cancellation: CancellationToken, on_line=None) -> AudioFeatures:
        open_silence = None
        pauses: list[list[float]] = []
        mean = -99.0

        def parse(line: str) -> None:
            nonlocal open_silence, mean
            start = SILENCE_START.search(line)
            end = SILENCE_END.search(line)
            volume = MEAN_VOLUME.search(line)
            if start:
                open_silence = float(start.group(1))
            if end:
                silence_end = float(end.group(1))
                pauses.append([open_silence if open_silence is not None else 0.0, silence_end])
                open_silence = None
            if volume:
                mean = float(volume.group(1))
            if on_line:
                on_line(line)

        self.runner.run([
            self.ffmpeg_path, "-hide_banner", "-i", str(source), "-vn", "-af",
            "silencedetect=n=-35dB:d=0.7,volumedetect", "-f", "null", "-",
        ], cancellation=cancellation, on_line=parse)
        if open_silence is not None:
            pauses.append([open_silence, duration])
        speech = []
        cursor = 0.0
        for start, end in sorted(pauses):
            if start > cursor:
                speech.append([cursor, start])
            cursor = max(cursor, end)
        if cursor < duration:
            speech.append([cursor, duration])
        features = AudioFeatures(speech_intervals=speech, pauses=pauses, mean_loudness=mean)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(asdict(features), ensure_ascii=False, indent=2), encoding="utf-8")
        return features

    @staticmethod
    def load(path: Path) -> AudioFeatures:
        return AudioFeatures(**json.loads(path.read_text(encoding="utf-8")))
