from pathlib import Path

from creator_assistant.services.shorts.global_template_library import GlobalShortsTemplateLibrary
from creator_assistant.services.shorts.project_template import ProjectShortsTemplate


def composition() -> ProjectShortsTemplate:
    return ProjectShortsTemplate(
        layout={"mode": "blur_background", "foreground_scale": 150, "crop_center": 61},
        subtitle={"style": "clean", "size": 66, "vertical_offset": -8},
        branding={
            "show_channel_card": True,
            "banner_scale": 115,
            "banner_offset_x": 7,
            "banner_offset_y": -12,
            "banner_opacity": 90,
            "channel_profile_id": "beppo_ru",
            "channel_banner_path": r"C:\private\beppo.png",
            "source_author": "Beppo",
        },
    )


def test_global_library_crud_assignments_and_atomic_reload(tmp_path: Path):
    path = tmp_path / "global_templates.json"
    library = GlobalShortsTemplateLibrary(path)
    created = library.create("Minecraft — Blur 150% + баннер", composition())
    copied = library.duplicate(created.template_id)
    renamed = library.rename(copied.template_id, "TikTok clean")
    library.assign("default", created.template_id)
    library.assign("youtube", created.template_id)
    library.assign("tiktok", renamed.template_id)

    restored = GlobalShortsTemplateLibrary(path)
    assert restored.selected("default").name == "Minecraft — Blur 150% + баннер"
    assert restored.selected("tiktok").name == "TikTok clean"
    assert restored.delete(renamed.template_id)
    assert restored.assignment("tiktok") == ""


def test_global_template_never_persists_banner_identity_or_asset_path(tmp_path: Path):
    library = GlobalShortsTemplateLibrary(tmp_path / "library.json")
    created = library.create("Safe", composition())
    branding = created.project_template().branding
    assert branding["show_channel_card"] is True
    assert branding["banner_scale"] == 115
    assert "channel_profile_id" not in branding
    assert "channel_banner_path" not in branding
    assert "source_author" not in branding
    assert "private" not in (tmp_path / "library.json").read_text(encoding="utf-8")


def test_editing_project_copy_does_not_change_global_template(tmp_path: Path):
    library = GlobalShortsTemplateLibrary(tmp_path / "library.json")
    created = library.create("Base", composition())
    project = created.project_template().to_dict()
    project["layout"]["foreground_scale"] = 200
    assert library.get(created.template_id).project_template().layout["foreground_scale"] == 150
