import json
from dataclasses import asdict
from pathlib import Path

from creator_assistant.domain.automation.models import AutomationJob, AutomationJobSource, AutomationShort
from creator_assistant.domain.shorts.models import (
    Candidate, Transcript, TranscriptSegment, TranscriptWord,
)
from creator_assistant.infrastructure.automation_job_store import AutomationJobStore
from creator_assistant.services.automation.selection import AutomaticCandidateSelector
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.project_template import (
    ProjectShortsTemplate, composition_snapshot_hash, render_identity, normalized_scale_percent,
)
from creator_assistant.services.shorts.render_settings import VerticalRenderSettingsResolver
from creator_assistant.services.shorts.subtitle_service import SubtitleService


def settings():
    return {
        "shorts_subtitle_defaults": {
            "style": "clean", "layout_mode": "center_crop", "foreground_scale": 100,
            "crop_center": 50, "size": 58, "vertical_offset": 0,
        },
        "shorts_subtitle_presets": {"clean": {"style": "clean", "size": 58}},
        "shorts_branding_defaults": {"show_title": False, "show_channel_card": False},
        "shorts_channel_profile_links": {},
    }


def template():
    return ProjectShortsTemplate(
        subtitle={
            "style": "clean", "font_family": "Segoe UI", "size": 66,
            "outline": 4, "shadow": 2, "position": "lower", "alignment": "center",
            "horizontal_offset": 9, "vertical_offset": -23, "maximum": 30,
            "lines": 2, "banner_gap": 15, "line_anchor_mode": "first_line_fixed",
        },
        layout={"mode": "blur_background", "foreground_scale": 150, "crop_center": 62},
        branding={
            "show_title": True, "title_size": 82, "title_alignment": "center",
            "title_y": 180, "show_channel_card": True, "banner_scale": 115,
            "banner_offset_x": 11, "banner_offset_y": -19, "banner_opacity": 90,
        },
    )


def asset_store(tmp_path: Path):
    root = tmp_path / "assets"
    for identifier, author in (("beppo_ru", "Beppo"), ("myles_ru", "MylesMC")):
        folder = root / identifier
        folder.mkdir(parents=True)
        (folder / "banner.png").write_bytes(identifier.encode())
        (folder / "profile.json").write_text(json.dumps({
            "id": identifier, "display_name": author, "source_author": author,
            "aliases": [author], "subscribe_banner": "banner.png", "enabled": True,
        }), encoding="utf-8")
    return ChannelAssetStore(root)


def candidate(index: int):
    return Candidate(
        f"short_{index:03d}", index * 50, index * 50 + 40, 95 - index,
        f"candidate {index}.", candidate_rank=index,
        subtitle_settings={"style": "gaming", "size": 42, "cues": [{"start": 0, "end": 1, "text": str(index)}]},
        layout_settings={"mode": "center_crop", "foreground_scale": 80},
        branding_settings={"translated_video_title": "Переведённое название", "title_size": 40},
    )


def test_four_candidates_inherit_one_project_template_and_only_explicit_override_differs(tmp_path):
    resolver = VerticalRenderSettingsResolver(settings(), asset_store(tmp_path))
    values = [resolver.resolve(item, source_author="Beppo", project_template=template()) for item in map(candidate, range(1, 5))]
    assert {value.layout["mode"] for value in values} == {"blur_background"}
    assert {value.layout["foreground_scale"] for value in values} == {150}
    assert {value.subtitle["size"] for value in values} == {66}
    assert {value.branding["title_size"] for value in values} == {82}
    assert {value.branding["banner_scale"] for value in values} == {115}
    assert {value.branding["final_title_text"] for value in values} == {"Переведённое название"}

    overridden = candidate(2)
    overridden.settings_override = True
    overridden.layout_settings = {"mode": "solid_color", "foreground_scale": 125}
    result = resolver.resolve(overridden, source_author="Beppo", project_template=template())
    assert result.layout["mode"] == "solid_color"
    assert result.layout["foreground_scale"] == 125


def test_channel_banner_image_changes_but_template_geometry_stays_shared(tmp_path):
    resolver = VerticalRenderSettingsResolver(settings(), asset_store(tmp_path))
    item = candidate(1)
    beppo = resolver.resolve(item, source_author="Beppo", project_template=template())
    myles = resolver.resolve(item, source_author="MylesMC", project_template=template())
    assert beppo.profile_id == "beppo_ru" and myles.profile_id == "myles_ru"
    assert beppo.branding["channel_banner_path"] != myles.branding["channel_banner_path"]
    for key in ("banner_scale", "banner_offset_x", "banner_offset_y", "banner_opacity"):
        assert beppo.branding[key] == myles.branding[key]


def test_four_qualified_candidates_are_selected_not_only_highest():
    candidates = [Candidate(f"short_{index:03d}", index * 100, index * 100 + 40, 95 - index, "text.") for index in range(1, 5)]
    selected, _ = AutomaticCandidateSelector().select(candidates, {
        "minimum_score": 70, "maximum_per_source": 10, "minimum_duration": 25,
        "maximum_duration": 75, "minimum_temporal_distance": 30,
    })
    assert [item.id for item in selected] == ["short_001", "short_002", "short_003", "short_004"]


def test_partial_overlap_in_real_project_is_not_mistaken_for_duplicate():
    candidates = [
        Candidate("short_001", 142.100, 181.700, 91.1, "one."),
        Candidate("short_003", 28.820, 83.480, 79.0, "two."),
        Candidate("short_002", 282.500, 329.300, 77.3, "three."),
        Candidate("short_004", 58.700, 103.700, 76.9, "four."),
    ]
    selected, _ = AutomaticCandidateSelector().select(candidates, {
        "minimum_score": 70, "maximum_per_source": 10, "minimum_duration": 25,
        "maximum_duration": 75, "minimum_temporal_distance": 30,
    })
    assert {item.id for item in selected} == {"short_001", "short_002", "short_003", "short_004"}


def test_snapshot_and_render_keys_are_stable_and_unique():
    snapshot = {"template": template().to_dict()}
    digest = composition_snapshot_hash(snapshot)
    keys = [render_identity("fingerprint", f"short_{index:03d}", index * 50, index * 50 + 40, digest) for index in range(1, 5)]
    assert digest == composition_snapshot_hash(snapshot)
    assert len(set(keys)) == 4


def test_template_summary_scale_is_not_multiplied_twice():
    assert normalized_scale_percent(150) == 150
    assert normalized_scale_percent(1.5) == 150


def test_timed_subtitles_exclude_context_after_and_split_long_speech():
    words = [TranscriptWord(index * 0.9, index * 0.9 + 0.7, f"слово{index}") for index in range(28)]
    inside = TranscriptSegment(1, 0, 24.8, " ".join(word.word for word in words), words=words)
    after = TranscriptSegment(2, 25.0, 28.0, "контекст после кандидата")
    transcript = Transcript("ru", 30, inside.text + " " + after.text, [inside, after])
    clip = Candidate("short_001", 0, 20, 90, inside.text)
    cues = SubtitleService().generate(transcript, clip, maximum=28, lines=2)
    assert len(cues) > 3
    assert all(0 <= cue.start < cue.end <= 20 for cue in cues)
    assert all(cue.end - cue.start <= 8.01 for cue in cues)
    assert "контекст после" not in " ".join(cue.text for cue in cues)
    assert SubtitleService.validate(cues, clip.duration, {"maximum": 28, "lines": 2}) == []


def test_cancelled_job_record_can_be_deleted_without_touching_artifacts(tmp_path):
    store = AutomationJobStore(tmp_path / "jobs")
    artifact = tmp_path / "render.mp4"
    artifact.write_bytes(b"video")
    job = AutomationJob("cancelled", [AutomationJobSource("source.mp4")], status="CANCELLED")
    job.shorts = [AutomationShort("one", "source", "short_001", 1, 0, 40, 90)]
    store.save(job)
    assert store.delete(job.job_id)
    assert store.load(job.job_id) is None
    assert artifact.is_file()
