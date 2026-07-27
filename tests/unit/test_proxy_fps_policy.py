import json
from pathlib import Path

import pytest

from creator_assistant.domain.errors import ValidationError
from creator_assistant.domain.models import ProxyFpsPolicy, VideoFormat
from creator_assistant.services.ffmpeg_service import FfmpegService
from creator_assistant.services.format_selector import build_format_plan, select_proxy_video
from creator_assistant.services.media_validation_service import MediaValidationService


def test_cap_30_transcode_builds_cfr_filter_for_60_fps():
    command = FfmpegService(object(), "ffmpeg.exe").build_proxy_command(
        Path("input.mp4"),
        Path("proxy.mp4"),
        maximum_height=480,
        fps_policy=ProxyFpsPolicy.CAP_30,
        source_fps=60.0,
    )
    assert command[command.index("-vf") + 1] == r"scale=-2:min(480\,ih),fps=30"
    assert command[command.index("-fps_mode") + 1] == "cfr"


def test_cap_30_keeps_ntsc_cadence_family():
    command = FfmpegService(object(), "ffmpeg.exe").build_proxy_command(
        Path("input.mp4"),
        Path("proxy.mp4"),
        fps_policy=ProxyFpsPolicy.CAP_30,
        source_fps=60000 / 1001,
    )
    assert "fps=30000/1001" in command[command.index("-vf") + 1]


def test_preserve_accepts_60_and_uses_passthrough():
    command = FfmpegService(object(), "ffmpeg.exe").build_proxy_command(
        Path("input.mp4"),
        Path("proxy.mp4"),
        fps_policy=ProxyFpsPolicy.PRESERVE,
        source_fps=60.0,
    )
    assert "fps=" not in command[command.index("-vf") + 1]
    assert command[command.index("-fps_mode") + 1] == "passthrough"


def test_proxy_selector_prefers_30_fps_direct_stream_for_cap_policy():
    formats = [
        VideoFormat("60", "mp4", height=480, fps=60, vcodec="h264"),
        VideoFormat("30", "mp4", height=480, fps=30, vcodec="h264"),
    ]
    selected = select_proxy_video(formats, 480, ProxyFpsPolicy.CAP_30)
    assert selected.format_id == "30"
    plan = build_format_plan(
        formats + [VideoFormat("audio", "m4a", acodec="aac")],
        480,
        ProxyFpsPolicy.CAP_30,
    )
    assert plan.proxy_requires_transcode is False


class _ProbeRunner:
    def __init__(self, avg: str, real: str):
        self.avg = avg
        self.real = real

    def run(self, _command, **_kwargs):
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 854,
                    "height": 480,
                    "avg_frame_rate": self.avg,
                    "r_frame_rate": self.real,
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "10", "format_name": "mp4"},
        }
        return type("Result", (), {"stdout": json.dumps(payload), "output": ""})()


@pytest.mark.parametrize("rate", ["30000/1001", "30/1"])
def test_cap_validation_accepts_30_compatible_rationals(tmp_path: Path, rate: str):
    path = tmp_path / "proxy.mp4"
    path.write_bytes(b"proxy")
    service = MediaValidationService(_ProbeRunner(rate, rate), "ffprobe.exe")
    assert service.validate_expected_video(
        path, None, height=480, fps_policy=ProxyFpsPolicy.CAP_30
    )


def test_cap_validation_rejects_5994_with_actionable_message(tmp_path: Path):
    path = tmp_path / "proxy.mp4"
    path.write_bytes(b"proxy")
    service = MediaValidationService(
        _ProbeRunner("60000/1001", "60000/1001"), "ffprobe.exe"
    )
    with pytest.raises(ValidationError, match="не более 30 FPS"):
        service.validate_expected_video(
            path, None, height=480, fps_policy=ProxyFpsPolicy.CAP_30
        )
    assert service.validate_expected_video(
        path, None, height=480, fps_policy=ProxyFpsPolicy.PRESERVE
    )


def test_ntsc_integer_pairs_use_tolerance():
    assert MediaValidationService.fps_compatible(30000 / 1001, 30.0)
    assert MediaValidationService.fps_compatible(60000 / 1001, 60.0)


def test_proxy_key_is_stable_and_includes_profile(tmp_path: Path):
    source = tmp_path / "исходник.mp4"
    source.write_bytes(b"video")
    first = FfmpegService.proxy_key(source, 480, ProxyFpsPolicy.CAP_30, 60.0)
    repeated = FfmpegService.proxy_key(source, 480, ProxyFpsPolicy.CAP_30, 60.0)
    preserve = FfmpegService.proxy_key(source, 480, ProxyFpsPolicy.PRESERVE, 60.0)
    assert first == repeated
    assert first != preserve
