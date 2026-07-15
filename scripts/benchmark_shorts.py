"""Reproducible 59.94 FPS benchmark for the Shorts render pipeline."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import Candidate, SourceInfo, SubtitleCue
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.shorts.render_service import ShortsRenderService
from creator_assistant.services.shorts.subtitle_service import SubtitleService


LONG_RUSSIAN_TEXT = (
    "Это очень длинная русская строка субтитров для проверки безопасной ширины, "
    "двух строк и корректного разбиения сверхдлинногорусскогословабезпробелов"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_path = output / "source_1280x720_5994.mp4"
    runner = ProcessRunner()
    token = CancellationToken()
    if not source_path.is_file():
        runner.run([
            args.ffmpeg, "-hide_banner", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=60000/1001:duration=60",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=60",
            "-c:v", "mpeg4", "-q:v", "5", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(source_path),
        ], cancellation=token, timeout=600)
    source = SourceInfo(
        str(source_path), source_path.name, source_path.stat().st_size, source_path.stat().st_mtime,
        60.0, 1280, 720, 60000 / 1001, "mpeg4", "aac", 1, 48000, "SDR", 0, "benchmark-5994",
    )
    service = ShortsRenderService(runner, args.ffmpeg, args.ffprobe, True, 0)
    subtitle_service = SubtitleService()
    report = {"source": str(source_path), "fps": source.fps, "duration": source.duration, "renders": []}
    for mode in ("center_crop", "blur_background"):
        for style in ("clean", "large", "gaming"):
            candidate = Candidate(
                f"{mode}_{style}", 0, 60, 90, LONG_RUSSIAN_TEXT,
                layout_settings={"mode": mode, "crop_center": 65, "foreground_scale": 92},
                subtitle_settings={"style": style, "position": "lower", "lines": 2},
            )
            ass = output / f"{candidate.id}.ass"
            subtitle_service.write(
                [SubtitleCue(0, 60, LONG_RUSSIAN_TEXT)], output / f"{candidate.id}.srt", ass,
                candidate.subtitle_settings,
            )
            target = output / f"{candidate.id}.mp4"
            started = time.monotonic()
            service.render(source, candidate, ass, target, token)
            elapsed = time.monotonic() - started
            report["renders"].append({
                "mode": mode, "style": style, "seconds": round(elapsed, 3),
                "speed": service.last_speed, "encoder": service.last_encoder,
                "output": str(target),
            })
            print(json.dumps(report["renders"][-1], ensure_ascii=False), flush=True)
    report_path = output / "benchmark_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
