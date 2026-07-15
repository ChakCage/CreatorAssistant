import pytest

from creator_assistant.domain.models import VideoFormat
from creator_assistant.services.format_selector import (
    build_format_plan,
    choose_container,
    is_hdr,
    select_maximum_sdr,
    select_proxy_video,
)


def fmt(identifier, height=None, fps=None, vcodec="none", acodec="none", dynamic="SDR", ext="webm", tbr=0):
    return VideoFormat(identifier, ext, height=height, fps=fps, vcodec=vcodec, acodec=acodec, dynamic_range=dynamic, tbr=tbr)


def formats():
    return [
        fmt("hdr", 2160, 60, "vp9.2", dynamic="HDR"),
        fmt("4k", 2160, 60, "vp9", tbr=18000),
        fmt("1080", 1080, 60, "avc1", tbr=9000),
        fmt("720h", 720, 60, "avc1", tbr=5000),
        fmt("720v", 720, 60, "vp9", tbr=7000),
        fmt("audio_opus", acodec="opus", tbr=160),
        fmt("audio_aac", acodec="mp4a.40.2", ext="m4a", tbr=128),
    ]


def test_hdr_markers_are_excluded():
    assert is_hdr(formats()[0])
    assert not is_hdr(formats()[1])


def test_maximum_sdr_wins_over_hdr():
    selected = select_maximum_sdr(formats())
    assert selected.format_id == "4k"
    assert selected.height == 2160
    assert selected.fps == 60


def test_proxy_prefers_h264_at_720p():
    assert select_proxy_video(formats()).format_id == "720h"


def test_proxy_does_not_upscale():
    selected = select_proxy_video([fmt("480", 480, 30, "avc1"), fmt("1080", 1080, 30, "avc1"), fmt("a", acodec="aac")])
    assert selected.height == 480
    assert selected.fps == 30


@pytest.mark.parametrize(("height", "expected"), [(480, "480"), (720, "720h"), (1080, "1080")])
def test_proxy_height_is_configurable(height, expected):
    available = formats() + [fmt("480", 480, 60, "avc1")]
    assert select_proxy_video(available, height).format_id == expected


def test_container_selection():
    assert choose_container(fmt("v", 1080, 30, "avc1"), fmt("a", acodec="mp4a.40.2")) == "mp4"
    assert choose_container(fmt("v", 1080, 30, "vp9"), fmt("a", acodec="opus")) == "mp4"
    assert choose_container(fmt("v", 1080, 30, "av01"), fmt("a", acodec="opus")) == "mp4"


def test_plan_keeps_original_fps_and_detects_no_transcode():
    plan = build_format_plan(formats())
    assert plan.maximum_video.fps == 60
    assert plan.proxy_video.fps == 60
    assert plan.proxy_requires_transcode is False
