from pathlib import Path

from PySide6.QtGui import QColor, QImage

from creator_assistant.domain.shorts.models import Candidate, SourceInfo
from creator_assistant.services.shorts.brand_assets import BrandAssetLibrary
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.filter_graph_builder import ShortsFilterGraphBuilder


def _png(path: Path) -> Path:
    image = QImage(320, 100, QImage.Format_ARGB32)
    image.fill(QColor(255, 0, 0, 128))
    assert image.save(str(path), "PNG")
    return path


def _video_probe(_path: Path) -> dict:
    return {
        "format": {"format_name": "matroska,webm", "duration": "3.5"},
        "streams": [
            {"codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuva420p", "width": 640, "height": 240},
            {"codec_type": "audio", "codec_name": "opus"},
        ],
    }


def test_library_copies_deduplicates_and_verifies_image(tmp_path: Path):
    source = _png(tmp_path / "banner.png")
    library = BrandAssetLibrary(tmp_path / "shared")

    first = library.import_asset(source)
    second = library.import_asset(source)

    assert first.asset_id == second.asset_id
    assert first.asset_type == "IMAGE"
    assert first.has_alpha is True
    assert library.path_for(first) != source
    assert library.path_for(first).is_file()
    assert library.verify(first.asset_id) == (True, "Материал проверен")
    assert len(library.assets()) == 1


def test_two_editions_share_profile_and_managed_asset(tmp_path: Path):
    library = BrandAssetLibrary(tmp_path / "CreatorAssistant" / "Shared" / "BrandAssets")
    developer = ChannelAssetStore(library.profiles_root, library=library)
    commercial = ChannelAssetStore(library.profiles_root, library=library)
    profile = developer.import_brand_asset("beppo_ru", _png(tmp_path / "beppo.png"), {
        "display_name": "Беппо на русском", "source_author": "Beppo", "aliases": ["Beppo", "Беппо"],
    })

    # Both editions resolve the same shared profile root/library in production.
    assert commercial.get(profile.id).brand_asset_id == profile.brand_asset_id
    assert commercial.banner_path(profile.id) == developer.banner_path(profile.id)
    assert commercial.library.path_for(profile.brand_asset_id).is_file()


def test_video_metadata_container_and_audio_are_verified(tmp_path: Path):
    source = tmp_path / "overlay.webm"
    source.write_bytes(b"fake webm for injected ffprobe")
    library = BrandAssetLibrary(tmp_path / "shared", probe=_video_probe)

    asset = library.import_asset(source)

    assert asset.asset_type == "VIDEO"
    assert asset.codec == "vp9"
    assert asset.duration == 3.5
    assert asset.has_alpha is True
    assert asset.has_audio is True
    assert library.verify(asset.asset_id)[0] is True


def test_video_overlay_graph_has_timing_geometry_and_explicit_audio_mix():
    source = SourceInfo("x.mp4", "x.mp4", 1, 1, 60, 1920, 1080, 59.94, "h264", "aac", 2, 48000, "SDR")
    candidate = Candidate("short_001", 0, 60, 80, "reason")
    candidate.branding_settings = {
        "show_channel_card": True,
        "brand_asset_type": "VIDEO",
        "brand_asset_has_audio": True,
        "banner_audio_enabled": True,
        "banner_audio_volume": 25,
        "banner_display_duration": 5,
        "banner_loop": False,
        "banner_freeze_last_frame": True,
        "banner_scale": 150,
        "banner_opacity": 70,
    }

    graph = ShortsFilterGraphBuilder().build(candidate, source, has_channel_banner=True, input_clipped=True)

    assert "tpad=stop_mode=clone" in graph
    assert "trim=duration=5.000" in graph
    assert "colorchannelmixer=aa=0.700" in graph
    assert "volume=0.250" in graph
    assert "amix=inputs=2" in graph
