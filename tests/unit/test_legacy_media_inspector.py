from pathlib import Path

from creator_assistant.domain.models import VideoFormat, VideoMetadata
from creator_assistant.services.format_selector import build_format_plan
from creator_assistant.services.legacy_media_inspector import LegacyProjectMediaInspector


def metadata() -> VideoMetadata:
    return VideoMetadata(
        "legacy-id",
        "Legacy Title",
        120.0,
        "https://youtu.be/legacy-id",
        formats=[
            VideoFormat("max", "mp4", height=1440, width=2560, fps=60, vcodec="vp9"),
            VideoFormat("proxy", "mp4", height=720, width=1280, fps=60, vcodec="avc1"),
            VideoFormat("audio", "m4a", acodec="mp4a.40.2", abr=128),
        ],
    )


def probe_factory(calls: dict[str, int]):
    def probe(path: Path, _cancellation=None):
        calls[str(path)] = calls.get(str(path), 0) + 1
        name = path.name.casefold()
        if "short" in name:
            return video_probe(720, 1280, 60)
        if "inst" in name:
            return audio_probe("flac")
        if path.suffix.casefold() in {".m4a", ".mp3", ".wav", ".opus"}:
            return audio_probe("aac")
        if "max" in name or "source" in name:
            return video_probe(2560, 1440, 60)
        if "proxy" in name:
            return video_probe(1280, 720, 60)
        return audio_probe("aac")

    return probe


def video_probe(width: int, height: int, fps: int):
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": width,
                "height": height,
                "avg_frame_rate": f"{fps}/1",
                "color_transfer": "bt709",
            },
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
        ],
        "format": {"duration": "120.0", "format_name": "mov,mp4,m4a", "bit_rate": "4000000"},
    }


def audio_probe(codec: str):
    return {
        "streams": [{"codec_type": "audio", "codec_name": codec, "sample_rate": "48000", "channels": 2}],
        "format": {"duration": "120.0", "format_name": codec, "bit_rate": "320000"},
    }


def test_detects_non_template_legacy_media_by_content(tmp_path: Path):
    project = tmp_path / "Legacy Project"
    materials = project / "Anything"
    materials.mkdir(parents=True)
    paths = {
        "maximum": materials / "episode final max render.mp4",
        "proxy": materials / "edit proxy.mp4",
        "audio": materials / "voice source.m4a",
        "instrumental": materials / "music inst.flac",
    }
    for path in paths.values():
        path.write_bytes(b"media")
    calls: dict[str, int] = {}

    result = LegacyProjectMediaInspector(probe_factory(calls)).inspect(
        project,
        metadata(),
        build_format_plan(metadata().formats),
        proxy_height=720,
    )

    assert result["maximum"].status == "VALID"
    assert result["maximum"].path == str(paths["maximum"])
    assert result["proxy"].status == "VALID"
    assert result["audio"].status == "VALID"
    assert result["instrumental"].status == "VALID"


def test_ambiguous_maximum_is_not_auto_accepted(tmp_path: Path):
    project = tmp_path / "Legacy Project"
    project.mkdir()
    first = project / "source max a.mp4"
    second = project / "source max b.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    result = LegacyProjectMediaInspector(probe_factory({})).inspect(
        project,
        metadata(),
        build_format_plan(metadata().formats),
        proxy_height=720,
    )

    assert result["maximum"].status == "AMBIGUOUS"
    assert result["maximum"].confidence == "AMBIGUOUS"
    assert len(result["maximum"].candidates) == 2


def test_short_vertical_file_is_not_used_as_proxy_or_maximum(tmp_path: Path):
    project = tmp_path / "Legacy Project"
    project.mkdir()
    short = project / "short proxy.mp4"
    short.write_bytes(b"short")

    result = LegacyProjectMediaInspector(probe_factory({})).inspect(
        project,
        metadata(),
        build_format_plan(metadata().formats),
        proxy_height=720,
    )

    assert result["maximum"].status == "NOT_MATCHED"
    assert result["proxy"].status == "NOT_MATCHED"


def test_probe_cache_is_used_per_inspector(tmp_path: Path):
    project = tmp_path / "Legacy Project"
    project.mkdir()
    source = project / "source max.mp4"
    source.write_bytes(b"media")
    calls: dict[str, int] = {}
    inspector = LegacyProjectMediaInspector(probe_factory(calls))

    for _ in range(2):
        inspector.inspect(project, metadata(), build_format_plan(metadata().formats), proxy_height=720)

    assert calls[str(source)] == 1
