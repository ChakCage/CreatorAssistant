import json
from pathlib import Path

from creator_assistant.domain.shorts.models import SourceInfo
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.shorts.cache import ShortsCache
from creator_assistant.services.shorts.manifest import ShortsManifestStore
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore
from creator_assistant.services.shorts.source_service import (
    ShortsSourceService,
    quota_source_fingerprint,
    source_content_fingerprint,
    source_fingerprint,
)


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


def test_quota_source_identity_survives_rename_and_identical_copy(tmp_path):
    original = tmp_path / "original.mp4"
    renamed = tmp_path / "renamed.mp4"
    copied = tmp_path / "other" / "copied.mp4"
    original.write_bytes(b"synthetic-video-content")
    renamed.write_bytes(original.read_bytes())
    copied.parent.mkdir()
    copied.write_bytes(original.read_bytes())

    identities = {source_content_fingerprint(path) for path in (original, renamed, copied)}

    assert len(identities) == 1
    source = sample_source(renamed)
    source.content_fingerprint = identities.pop()
    assert quota_source_fingerprint(source) == source_content_fingerprint(original)


def test_quota_source_identity_changes_when_content_changes(tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"content-a")
    second.write_bytes(b"content-b")
    assert source_content_fingerprint(first) != source_content_fingerprint(second)


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


def test_suggested_shorts_root_uses_creator_project_or_standalone_name(tmp_path):
    project = tmp_path / "Creator Project"
    marker = project / ".creator-assistant" / "manifest.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    nested = project / "Материалы" / "movie.mp4"
    nested.parent.mkdir()
    nested.write_bytes(b"video")
    assert ShortsProjectStore.suggested_root(nested) == project / "Shorts"

    standalone = tmp_path / "Exports" / "Minecraft.mp4"
    standalone.parent.mkdir()
    standalone.write_bytes(b"video")
    assert ShortsProjectStore.suggested_root(standalone) == standalone.parent / "Minecraft Shorts"


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
