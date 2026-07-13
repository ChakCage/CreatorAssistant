import json
from pathlib import Path

from creator_assistant.domain.shorts.models import SourceInfo
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.shorts.cache import ShortsCache
from creator_assistant.services.shorts.manifest import ShortsManifestStore
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore
from creator_assistant.services.shorts.source_service import ShortsSourceService, source_fingerprint


def sample_source(path: Path) -> SourceInfo:
    return SourceInfo(
        path=str(path), name=path.name, size=123, mtime=10.0, duration=65.25,
        width=1920, height=1080, fps=29.97, video_codec="h264", audio_codec="aac",
        audio_channels=2, sample_rate=48000,
    )


def test_source_fingerprint_is_stable_and_sensitive(tmp_path):
    source = sample_source(tmp_path / "ролик's.mp4")
    first = source_fingerprint(source)
    assert first == source_fingerprint(source)
    source.duration += 0.001
    assert first != source_fingerprint(source)


def test_project_manifest_is_atomic_utf8_and_resumable(tmp_path):
    source_file = tmp_path / "исходник.mp4"
    source_file.write_bytes(b"video")
    source = sample_source(source_file)
    source.fingerprint = source_fingerprint(source)
    store = ShortsProjectStore()
    paths = store.open_or_create(tmp_path / "Shorts", source)
    loaded = ShortsManifestStore(paths.manifest).load()
    assert loaded.source_path.endswith("исходник.mp4")
    assert paths.thumbnails.is_dir()
    assert not paths.manifest.with_suffix(".json.tmp").exists()
    assert store.open_or_create(tmp_path / "Shorts", source) == paths


def test_cache_invalidation_uses_stage_settings(tmp_path):
    source = sample_source(tmp_path / "source.mp4")
    source.fingerprint = "abc"
    manifest = ShortsManifestStore(tmp_path / "shorts_manifest.json").create("id", source)
    artifact = tmp_path / "proxy.mp4"
    artifact.write_bytes(b"ok")
    cache = ShortsCache(manifest)
    cache.mark_complete("proxy", {"height": 720})
    assert cache.stage_valid("proxy", artifact, {"height": 720})
    assert not cache.stage_valid("proxy", artifact, {"height": 480})


def test_ffprobe_parsing_handles_hdr_rotation_and_unicode(tmp_path):
    source_file = tmp_path / "майнкрафт.mp4"
    source_file.write_bytes(b"video")
    payload = {
        "format": {"duration": "42.5"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
             "avg_frame_rate": "60000/1001", "color_transfer": "smpte2084",
             "side_data_list": [{"rotation": -90}]},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "48000"},
        ],
    }

    class Runner:
        def run(self, command, **kwargs):
            text = json.dumps(payload)
            return ProcessResult(list(command), 0, text, stdout=text)

    info = ShortsSourceService(Runner(), "ffprobe.exe").probe(source_file)
    assert (info.width, info.height, round(info.fps, 3)) == (3840, 2160, 59.94)
    assert (info.dynamic_range, info.rotation) == ("HDR", -90)
    assert info.fingerprint
