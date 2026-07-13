import shutil
from pathlib import Path

import pytest

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import Candidate, SubtitleCue
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.shorts.render_service import ShortsRenderService
from creator_assistant.services.shorts.source_service import ShortsSourceService
from creator_assistant.services.shorts.subtitle_service import SubtitleService


def test_real_ffmpeg_vertical_render_with_burned_cyrillic(tmp_path):
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/FFprobe not available")
    runner = ProcessRunner()
    token = CancellationToken()
    source_path = tmp_path / "исходник автора's.mp4"
    runner.run([
        ffmpeg, "-hide_banner", "-y", "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=25:d=1.2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.2", "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source_path),
    ], cancellation=token)
    source = ShortsSourceService(runner, ffprobe).probe(source_path)
    candidate = Candidate("short_001", 0, 1.0, 90, "Привет, Shorts!", layout_settings={"mode": "center_crop", "crop_center": 50})
    subtitle = tmp_path / "short_001.ass"
    SubtitleService().write([SubtitleCue(0, 1, "Привет, Shorts!")], tmp_path / "short_001.srt", subtitle, {"style": "gaming", "position": "center", "size": 72})
    output = tmp_path / "готовый Short.mp4"
    service = ShortsRenderService(runner, ffmpeg, ffprobe, False, 0)
    service.render(source, candidate, subtitle, output, token)
    probe = service.validate(output, 1.0, token)
    video = next(item for item in probe["streams"] if item.get("codec_type") == "video")
    assert (video["width"], video["height"], video["codec_name"]) == (1080, 1920, "h264")
    assert output.stat().st_size > 10_000
