from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import Scene
from creator_assistant.infrastructure.process_runner import ProcessRunner


PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


class SceneDetectionService:
    def __init__(self, runner: ProcessRunner, ffmpeg_path: str, threshold: float = 0.35) -> None:
        self.runner, self.ffmpeg_path, self.threshold = runner, ffmpeg_path, threshold

    def detect(self, source: Path, duration: float, output: Path, cancellation: CancellationToken, on_line: Optional[Callable[[str], None]] = None) -> list[Scene]:
        cuts: list[float] = []

        def parse(line: str) -> None:
            match = PTS_RE.search(line)
            if match:
                value = float(match.group(1))
                if 0.1 < value < duration - 0.1:
                    cuts.append(value)
            if on_line:
                on_line(line)

        self.runner.run([
            self.ffmpeg_path, "-hide_banner", "-i", str(source), "-vf",
            f"select='gt(scene,{self.threshold})',showinfo", "-an", "-f", "null", "-",
        ], cancellation=cancellation, on_line=parse)
        boundaries = [0.0] + sorted(set(round(value, 3) for value in cuts)) + [duration]
        scenes = [Scene(start, end, 0.0) for start, end in zip(boundaries, boundaries[1:]) if end - start >= 0.05]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps([asdict(scene) for scene in scenes], ensure_ascii=False, indent=2), encoding="utf-8")
        return scenes

    def generate_thumbnails(self, source: Path, scenes: list[Scene], folder: Path, cancellation: CancellationToken, limit: int = 120) -> list[Scene]:
        folder.mkdir(parents=True, exist_ok=True)
        for index, scene in enumerate(scenes[:limit], 1):
            target = folder / f"scene_{index:04d}.jpg"
            if not target.is_file() or target.stat().st_size == 0:
                at = min(scene.end - 0.01, scene.start + min(0.5, max(0.05, (scene.end - scene.start) / 2)))
                self.runner.run([
                    self.ffmpeg_path, "-hide_banner", "-y", "-ss", f"{max(0, at):.3f}", "-i", str(source),
                    "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "3", str(target),
                ], cancellation=cancellation)
            scene.thumbnail = str(target)
        return scenes

    @staticmethod
    def save(path: Path, scenes: list[Scene]) -> None:
        path.write_text(json.dumps([asdict(scene) for scene in scenes], ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def load(path: Path) -> list[Scene]:
        return [Scene(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
