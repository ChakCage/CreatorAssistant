import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from creator_assistant.domain.automation.models import (
    AutomationJob, AutomationJobSource, AutomationProfile, AutomationShort,
)
from creator_assistant.domain.shorts.models import Candidate
from creator_assistant.services.automation.selection import AutomaticCandidateSelector, unique_automation_shorts
from creator_assistant.services.automation.shorts_pipeline import ExistingShortsAutomationPipeline
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.render_settings import VerticalRenderSettingsResolver
from creator_assistant.ui.autopilot_tab import AutopilotResultsDialog


def _settings():
    return {
        "shorts_subtitle_defaults": {
            "style": "gaming", "font_family": "Segoe UI", "size": 66,
            "position": "lower", "alignment": "left", "horizontal_offset": 37,
            "vertical_offset": -23, "maximum": 29, "lines": 2,
            "outline": 6, "shadow": 4, "background": True,
            "safe_margin": 144, "auto_above_banner": True, "banner_gap": 41,
            "layout_mode": "blur_background", "crop_center": 63,
            "foreground_scale": 147, "background_color": "black",
        },
        "shorts_subtitle_presets": {
            "gaming": {"style": "gaming", "size": 66, "vertical_offset": -23},
        },
        "shorts_branding_defaults": {
            "preset": "promotion", "show_title": True, "title_style": "gaming",
            "title_font_family": "Segoe UI", "title_alignment": "right",
            "title_offset_x": 24, "title_y": 190, "title_size": 82,
            "show_channel_card": True, "channel_profile_id": "beppo_ru",
            "banner_scale": 110, "banner_offset_x": 9, "banner_offset_y": -12,
            "banner_opacity": 88,
        },
        "shorts_channel_profile_links": {},
    }


def _assets(tmp_path: Path) -> ChannelAssetStore:
    root = tmp_path / "assets"
    profile = root / "beppo_ru"
    profile.mkdir(parents=True)
    (profile / "banner.png").write_bytes(b"png")
    (profile / "profile.json").write_text(json.dumps({
        "id": "beppo_ru", "display_name": "Beppo На Русском", "source_author": "Beppo",
        "aliases": ["Beppo", "Беппо"], "subscribe_banner": "banner.png", "enabled": True,
        "default_banner_scale": 123, "default_banner_offset_x": 17,
        "default_banner_offset_y": -31, "default_banner_opacity": 91,
    }, ensure_ascii=False), encoding="utf-8")
    return ChannelAssetStore(root)


def _candidate(identifier="short_001", start=0.0, end=40.0, score=95.0, rank=1):
    return Candidate(
        identifier, start, end, score, "transcript", final_score=score, candidate_rank=rank,
        # These are deliberately stale visual values copied into an old manifest.
        subtitle_settings={"style": "clean", "size": 42, "cues": [{"start": 0, "end": 1, "text": "cue"}]},
        layout_settings={"mode": "center_crop", "crop_center": 1},
        branding_settings={
            "title_size": 40, "translated_video_title": "Русское название",
            "short_hook_title": "Hook", "final_title_text": "Hook",
        },
    )


def test_saved_vertical_editor_settings_override_stale_candidate_visuals(tmp_path):
    resolved = VerticalRenderSettingsResolver(_settings(), _assets(tmp_path)).resolve(
        _candidate(), source_author="Beppo", selected_profile_id="beppo_ru",
    )
    assert resolved.subtitle["style"] == "gaming"
    assert resolved.subtitle["size"] == 66
    assert resolved.subtitle["vertical_offset"] == -23
    assert resolved.subtitle["alignment"] == "left"
    assert resolved.subtitle["cues"][0]["text"] == "cue"
    assert resolved.layout == {
        "mode": "blur_background", "crop_center": 63, "foreground_scale": 147,
        "background_color": "black",
    }
    assert resolved.branding["title_style"] == "gaming"
    assert resolved.branding["title_size"] == 82
    assert resolved.branding["final_title_text"] == "Hook"
    assert resolved.branding["banner_scale"] == 123
    assert resolved.branding["banner_offset_x"] == 17
    assert resolved.branding["banner_offset_y"] == -31
    assert resolved.profile_id == "beppo_ru"


def test_autopilot_composition_uses_same_shared_saved_settings(tmp_path):
    settings = _settings()
    assets = _assets(tmp_path)
    container = SimpleNamespace(settings=settings)
    pipeline = ExistingShortsAutomationPipeline(container)
    pipeline.assets = assets
    candidate = _candidate()
    source_path = tmp_path / "Beppo" / "video.mp4"
    source = AutomationJobSource(str(source_path), source_id="source-a", source_author="Beppo")
    short = AutomationShort(
        "source-a-short_001", "source-a", candidate.id, 1,
        candidate.start, candidate.end, candidate.final_score, candidate_data=asdict(candidate),
    )
    job = AutomationJob("job", [source], profile=AutomationProfile(channel_profile_id="beppo_ru"), shorts=[short])
    expected = VerticalRenderSettingsResolver(settings, assets).resolve(
        candidate, source_author="Beppo", aliases=["Beppo", *[p.name for p in source_path.parents][:4]],
        selected_profile_id="beppo_ru",
    )
    pipeline.prepare_composition(job)
    actual = job.shorts[0]
    assert actual.subtitle_settings == expected.subtitle
    assert actual.layout_settings == expected.layout
    assert actual.branding_settings["title_size"] == expected.branding["title_size"]
    assert actual.branding_settings["banner_scale"] == expected.branding["banner_scale"]
    assert actual.branding_settings["final_title_text"] == "Русское название"
    assert actual.branding_settings["title_mode"] == "TRANSLATED_SOURCE_TITLE"


def test_five_unique_selected_shorts_create_five_unique_queue_entries():
    candidates = [_candidate(f"short_{index:03d}", index * 100, index * 100 + 40, 100 - index, index) for index in range(1, 6)]
    candidates.extend([
        _candidate("short_001", 0, 40, 20, 99),
        _candidate("internal_alt", 200, 240, 20, 98),
    ])
    selected, _ = AutomaticCandidateSelector().select(candidates, {
        "minimum_score": 0, "maximum_per_source": 10, "minimum_temporal_distance": 0,
        "minimum_duration": 1, "maximum_duration": 100,
    })
    assert [item.id for item in selected] == [f"short_{index:03d}" for index in range(1, 6)]

    shorts = [
        AutomationShort(f"source-{item.id}", "source", item.id, item.candidate_rank,
                        item.start, item.end, item.final_score, candidate_data=asdict(item))
        for item in selected
    ]
    # A resumed/approved job may still contain an old duplicate; queue defence removes it.
    shorts.append(AutomationShort("duplicate", "source", "short_001", 90, 500, 540, 1))
    unique = unique_automation_shorts(shorts)
    queue = ExistingShortsAutomationPipeline._render_queue_details(unique)
    assert len(unique) == len(queue) == 5
    assert len({item.short_id for item in unique}) == 5
    assert len({item.candidate_id for item in unique}) == 5
    assert len({item["output_filename"] for item in queue}) == 5
    assert [item["candidate_id"] for item in queue] == [f"short_{index:03d}" for index in range(1, 6)]
    assert all(item["start"] < item["end"] for item in queue)
    job = AutomationJob("job", [AutomationJobSource("source.mp4", source_id="source")], shorts=unique)
    rows = AutopilotResultsDialog.rows(job)
    assert len(rows) == 5
    assert len({row[1] for row in rows}) == 5
    assert len({row[2] for row in rows}) == 5
