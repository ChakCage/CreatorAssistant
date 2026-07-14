from pathlib import Path
from typing import Optional

import pytest

from creator_assistant.domain.models import VideoFormat, VideoMetadata
from creator_assistant.infrastructure.manifest_store import (
    DiscoveredFile,
    ManifestLoadResult,
    ManifestMigrator,
    ManifestStatus,
)
from creator_assistant.services.format_selector import build_format_plan
from creator_assistant.services.legacy_media_inspector import LegacyMediaProbe, LegacyProjectMediaInspector
from creator_assistant.services.project_service import ProjectService


def metadata(source_height: int = 1080) -> VideoMetadata:
    return VideoMetadata(
        "profile", "Profile", 60, "https://youtu.be/profile",
        formats=[
            VideoFormat("max", "mp4", width=source_height * 16 // 9, height=source_height, fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("720", "mp4", width=1280, height=min(720, source_height), fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("480", "mp4", width=854, height=min(480, source_height), fps=60, vcodec="h264", acodec="aac"),
            VideoFormat("audio", "m4a", acodec="aac"),
        ],
    )


def probe(path: str, height: int, width: Optional[int] = None) -> LegacyMediaProbe:
    return LegacyMediaProbe(
        path=Path(path), size=100, duration=60, width=width or height * 16 // 9,
        height=height, fps=60, video_codec="h264", audio_codec="aac",
        video_streams=1, audio_streams=1, dynamic_range="SDR",
    )


@pytest.mark.parametrize(
    ("requested", "found"),
    ((480, 720), (720, 480), (1080, 720)),
)
def test_different_actual_height_never_satisfies_requested_profile(requested: int, found: int):
    item = metadata(1080)
    plan = build_format_plan(item.formats, requested)
    inspector = LegacyProjectMediaInspector(lambda _path, _token: {})

    assert inspector._score_proxy(probe(f"video [{found}p].mp4", found), item, plan, requested) is None


def test_requested_1080_uses_720_when_source_is_720_without_upscale():
    item = metadata(720)
    plan = build_format_plan(item.formats, 1080)
    effective = ProjectService.effective_proxy_height(plan, 1080)
    inspector = LegacyProjectMediaInspector(lambda _path, _token: {})

    assert effective == 720
    assert inspector._score_proxy(probe("small_video.mp4", 720), item, plan, effective) is not None


def test_exact_profile_is_selected_from_multiple_variants():
    item = metadata()
    plan = build_format_plan(item.formats, 480)
    inspector = LegacyProjectMediaInspector(lambda _path, _token: {})
    match = inspector._select(
        "REAPER_PROXY",
        [probe("odd-name.mp4", 480), probe("video [720p].mp4", 720)],
        lambda value: inspector._score_proxy(value, item, plan, 480),
        "now",
    )

    assert match.status == "VALID"
    assert Path(match.path).name == "odd-name.mp4"


def test_filename_cannot_override_actual_height_and_vertical_is_rejected():
    item = metadata()
    plan = build_format_plan(item.formats, 480)
    inspector = LegacyProjectMediaInspector(lambda _path, _token: {})

    assert inspector._score_proxy(probe("fake [480p].mp4", 720), item, plan, 480) is None
    assert inspector._score_proxy(probe("vertical [720p].mp4", 720, width=405), item, plan, 720) is None


def test_manifest_migration_keeps_legacy_proxy_for_validation(tmp_path: Path):
    proxy = tmp_path / "legacy proxy.mp4"
    proxy.write_bytes(b"proxy")
    raw = {
        "schema_version": 2,
        "video_id": "profile",
        "project_path": str(tmp_path),
        "materials_path": str(tmp_path / "Материалы"),
        "files": {"proxy": {"path": str(proxy), "source": "legacy_manifest"}},
    }
    existing = ManifestLoadResult(ManifestStatus.VALID, tmp_path / "manifest.json", raw=raw)
    manifest = ManifestMigrator().migrate(
        existing,
        video_id="profile",
        source_url="https://youtu.be/profile",
        title="Profile",
        project_path=tmp_path,
        author_preset="Author",
        job_id="job",
        discovered_files=[DiscoveredFile(str(proxy), "VIDEO", proxy.stat().st_size)],
    )

    assert manifest.reaper_proxies["legacy"]["status"] == "NEEDS_VALIDATION"
    assert manifest.reaper_proxies["legacy"]["path"] == str(proxy)


def test_manifest_merge_preserves_720_when_480_is_added():
    existing = {"720": {"status": "VALID", "path": "720.mp4", "height": 720}}
    stages = {
        "proxy": {"status": "VALID", "path": "480.mp4", "height": 480, "effective_height": 480},
        "proxy_variants": {"variants": {"480": {"status": "VALID", "path": "480.mp4", "height": 480}}},
    }

    merged = ProjectService._merge_proxy_variants(existing, stages)

    assert set(merged) == {"480", "720"}
    assert merged["720"]["path"] == "720.mp4"
    assert merged["480"]["path"] == "480.mp4"
