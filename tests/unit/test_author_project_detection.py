from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from creator_assistant.domain.author_presets import AuthorMatchKind, AuthorPreset
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import VideoMetadata
from creator_assistant.domain.youtube_auth import YtDlpAuthContext
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.infrastructure.manifest_store import MANIFEST_NAME, ManifestLoader, ManifestMigrator, ManifestWriter
from creator_assistant.infrastructure.project_index import ProjectIndex, ProjectRoot
from creator_assistant.infrastructure.settings_store import SettingsStore
from creator_assistant.services.author_preset_resolver import AuthorPresetResolver
from creator_assistant.services.project_service import ProjectService
from creator_assistant.ui.project_prep_tab import ProjectPrepTab


def preset(name: str, root: Path, *, channel_ids=(), handles=(), aliases=()) -> AuthorPreset:
    return AuthorPreset(
        f"preset-{name.casefold()}", name, str(root),
        tuple(channel_ids), tuple(handles), tuple(aliases),
    )


def metadata(**changes) -> VideoMetadata:
    values = {
        "video_id": "7e09O9pzSG8",
        "title": "I Mined The End Dimension - Hardcore",
        "duration": 120,
        "webpage_url": "https://youtu.be/7e09O9pzSG8",
        "formats": [],
        "channel_id": "UC-BEPPO",
        "channel": "Beppo",
        "uploader_id": "@Beppo",
        "channel_handle": "@Beppo",
    }
    values.update(changes)
    return VideoMetadata(**values)


def service(tmp_path: Path, index: ProjectIndex | None = None) -> ProjectService:
    item = ProjectService.__new__(ProjectService)
    item.job_store = JobStore(tmp_path / "jobs")
    item.manifest_loader = ManifestLoader()
    item.manifest_writer = ManifestWriter(item.manifest_loader)
    item.manifest_migrator = ManifestMigrator()
    item.project_index = index
    item.project_roots = []
    item.validator = None
    return item


def test_exact_channel_id_selects_correct_preset(tmp_path: Path):
    beppo = preset("Beppo", tmp_path / "Beppo", channel_ids=("UC-BEPPO",))
    myles = preset("MylesMC", tmp_path / "Myles", channel_ids=("UC-MYLES",))
    result = AuthorPresetResolver().resolve(metadata(channel="Renamed channel"), [myles, beppo])
    assert result.kind == AuthorMatchKind.EXACT_MATCH
    assert result.preset == beppo
    assert result.matched_by == "channel_id"


def test_channel_display_name_change_does_not_break_id_mapping(tmp_path: Path):
    beppo = preset("Old visible name", tmp_path / "Beppo", channel_ids=("UC-BEPPO",))
    result = AuthorPresetResolver().resolve(metadata(channel="Completely New Name"), [beppo])
    assert result.preset == beppo


def test_handle_is_fallback_identity(tmp_path: Path):
    beppo = preset("Creator", tmp_path / "Beppo", handles=("beppo",))
    result = AuthorPresetResolver().resolve(metadata(channel_id="", channel="Unrelated", channel_handle="https://youtube.com/@Beppo"), [beppo])
    assert result.kind == AuthorMatchKind.EXACT_MATCH
    assert result.matched_by == "handle"


def test_handle_exactly_matching_display_name_needs_no_fuzzy_guess(tmp_path: Path):
    myles = preset("MylesMC", tmp_path / "Myles")
    item = metadata(
        channel_id="UC-MYLES", channel="Myles", uploader_id="@MylesMC",
        channel_handle="@MylesMC", uploader="Myles",
    )
    result = AuthorPresetResolver().resolve(item, [myles])
    assert result.kind == AuthorMatchKind.EXACT_MATCH
    assert result.preset == myles
    assert result.matched_by == "handle"


def test_alias_matches_only_when_explicitly_configured(tmp_path: Path):
    plain = preset("Robert", tmp_path / "plain")
    aliased = preset("Robert", tmp_path / "alias", aliases=("Beppo",))
    item = metadata(channel_id="", channel_handle="", uploader_id="", uploader="", channel="Beppo")
    assert AuthorPresetResolver().resolve(item, [plain]).kind == AuthorMatchKind.NO_MATCH
    assert AuthorPresetResolver().resolve(item, [aliased]).kind == AuthorMatchKind.ALIAS_MATCH


def test_unknown_channel_is_never_randomly_selected(tmp_path: Path):
    items = [preset("MylesMC", tmp_path / "Myles"), preset("Beppo", tmp_path / "Beppo")]
    result = AuthorPresetResolver().resolve(metadata(channel_id="UC-UNKNOWN", channel="Someone Else", uploader_id="", channel_handle=""), items)
    assert result.kind == AuthorMatchKind.NO_MATCH
    assert result.preset is None


def test_duplicate_channel_id_requires_user_selection(tmp_path: Path):
    items = [
        preset("Beppo A", tmp_path / "a", channel_ids=("UC-BEPPO",)),
        preset("Beppo B", tmp_path / "b", channel_ids=("UC-BEPPO",)),
    ]
    result = AuthorPresetResolver().resolve(metadata(), items)
    assert result.kind == AuthorMatchKind.MULTIPLE_MATCHES
    assert len(result.matches) == 2


def test_legacy_paths_migrate_without_losing_name_or_path(tmp_path: Path):
    root = tmp_path / "Beppo" / "Делаю"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"author_paths": [str(root)]}, ensure_ascii=False), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded["author_paths"] == [str(root)]
    assert loaded["author_presets"][0]["display_name"] == "Beppo"
    assert loaded["author_presets"][0]["root_path"] == str(root)


def test_search_uses_every_configured_root_and_finds_other_preset(tmp_path: Path):
    myles = tmp_path / "Myles" / "Делаю"
    beppo = tmp_path / "Beppo" / "Делаю"
    myles.mkdir(parents=True)
    project = beppo / "renamed"
    project.mkdir(parents=True)
    manifest_path = project / MANIFEST_NAME
    manifest_path.parent.mkdir()
    manifest_path.write_text(json.dumps({
        "schema_version": 2, "video_id": "7e09O9pzSG8", "title": "renamed",
        "project_path": str(project), "materials_path": str(project / "Материалы"),
    }, ensure_ascii=False), encoding="utf-8")
    registry = service(tmp_path)
    found = registry.find_existing_projects_across([
        ProjectRoot(myles, "myles", "MylesMC"), ProjectRoot(beppo, "beppo", "Beppo")
    ], metadata())
    assert found[0]["path"] == project
    assert found[0]["preset_name"] == "Beppo"


def test_discovery_does_not_create_destination_before_decision(tmp_path: Path):
    destination = tmp_path / "Myles" / "Делаю"
    destination.mkdir(parents=True)
    registry = service(tmp_path)
    planned = registry.planned_path(destination, metadata())
    registry.find_existing_projects_across([ProjectRoot(destination)], metadata())
    assert not planned.exists()


def test_binding_existing_project_never_invokes_downloader(tmp_path: Path):
    project = tmp_path / "Beppo" / "Делаю" / "existing"
    project.mkdir(parents=True)
    user_file = project / "valid-user-file.mp4"
    user_file.write_bytes(b"already-valid")
    registry = service(tmp_path)
    registry.migrate_existing(
        metadata(), project, job_id="migration", author_preset="Beppo",
        cancellation=CancellationToken(), on_progress=lambda _info: None,
    )
    assert user_file.read_bytes() == b"already-valid"
    assert registry.job_store.project_path("7e09O9pzSG8") == project


def test_numbered_copy_is_allocated_only_by_explicit_copy_call(tmp_path: Path):
    destination = tmp_path / "Делаю"
    destination.mkdir()
    registry = service(tmp_path)
    exact = registry.planned_path(destination, metadata())
    exact.mkdir()
    assert registry.planned_path(destination, metadata()) == exact
    copy = registry.copy_path(destination, metadata())
    assert copy.name.endswith("(2)")
    assert not copy.exists()


def test_legacy_folder_without_manifest_is_only_a_fallback_offer(tmp_path: Path):
    destination = tmp_path / "Делаю"
    destination.mkdir()
    registry = service(tmp_path)
    legacy = registry.planned_path(destination, metadata())
    legacy.mkdir()
    found = registry.find_existing_projects(destination, metadata())
    assert found[0]["path"] == legacy
    assert found[0]["source"] == "title"
    assert found[0]["confirmed"] is False


def test_title_is_not_treated_as_confirmed_identity(tmp_path: Path):
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
        (root / metadata().title).mkdir()
    registry = service(tmp_path)
    found = registry.find_existing_projects_across([ProjectRoot(root) for root in roots], metadata())
    assert len(found) == 2
    assert all(not item["confirmed"] for item in found)


def test_unavailable_root_is_reported_and_does_not_break_scan(tmp_path: Path):
    available = tmp_path / "available"
    available.mkdir()
    index = ProjectIndex(tmp_path / "index.json")
    result = index.rescan([ProjectRoot(tmp_path / "missing"), ProjectRoot(available)])
    assert result["scanned_roots"] == [str(available)]
    assert "Недоступна" in result["warnings"][0]


def test_index_updates_after_manifest_checkpoint(tmp_path: Path):
    root = tmp_path / "Beppo" / "Делаю"
    project = root / "project"
    project.mkdir(parents=True)
    index = ProjectIndex(tmp_path / "index.json")
    registry = service(tmp_path, index)
    registry.project_roots = [preset("Beppo", root, channel_ids=("UC-BEPPO",)).to_dict()]
    registry._write_manifest(project, metadata(), "running")
    assert index.lookup("7e09O9pzSG8")[0]["project_path"] == str(project)


def test_stale_index_entry_is_marked_without_crash(tmp_path: Path):
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({"projects": [{"video_id": "7e09O9pzSG8", "project_path": str(tmp_path / "gone")}]}, ensure_ascii=False), encoding="utf-8")
    item = ProjectIndex(index_path).lookup("7e09O9pzSG8")[0]
    assert item["stale"] is True


class RuntimeStub:
    marker_path = Path("missing")

    def is_ready(self):
        return False


class StoreStub:
    def __init__(self):
        self.saved = None

    def save(self, settings):
        self.saved = deepcopy(settings)


class UiContainer:
    def __init__(self, root: Path):
        myles = root / "MylesMC" / "Делаю"
        beppo = root / "Beppo" / "Делаю"
        myles.mkdir(parents=True)
        beppo.mkdir(parents=True)
        self.settings = {
            "youtube_root": str(root), "author_paths": [], "selected_author_path": str(myles),
            "author_presets": [
                preset("MylesMC", myles, channel_ids=("UC-MYLES",)).to_dict(),
                preset("Beppo", beppo, channel_ids=("UC-BEPPO",)).to_dict(),
            ],
            "suggest_remember_author": True, "youtube_access": {}, "open_folder_after_completion": False,
        }
        self.settings_store = StoreStub()
        self.audio_separator_runtime = RuntimeStub()
        self.projects = service(root / "registry")
        self.projects.project_roots = self.settings["author_presets"]
        self.logger = logging.getLogger("author-project-detection-test")
        self.youtube_auth = YtDlpAuthContext()
        self.paths = {"yt_dlp": "yt-dlp.exe"}


def test_manual_override_is_retained_for_current_video(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    container = UiContainer(tmp_path)
    tab = ProjectPrepTab(container)
    tab.metadata = metadata()
    myles_index = tab.author_combo.findData(str(tmp_path / "MylesMC" / "Делаю"))
    tab.author_combo.setCurrentIndex(myles_index)
    tab._author_manual_override_video_id = tab.metadata.video_id
    tab._resolve_author_for_metadata(tab.metadata)
    assert tab._current_preset_name() == "MylesMC"
    tab.close()
    app.processEvents()


def test_explicit_remember_adds_channel_mapping(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    container = UiContainer(tmp_path)
    tab = ProjectPrepTab(container)
    unknown = metadata(channel_id="UC-NEW", channel="New Creator", uploader_id="@new", channel_handle="@new")
    target = next(item for item in tab.author_presets if item.display_name == "MylesMC")
    tab._bind_channel_to_preset(unknown, target)
    saved = next(item for item in container.settings_store.saved["author_presets"] if item["preset_id"] == target.preset_id)
    assert "UC-NEW" in saved["youtube_channel_ids"]
    assert "@new" in saved["youtube_handles"]
    tab.close()
    app.processEvents()


def test_declining_remember_does_not_mutate_presets(monkeypatch, tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    container = UiContainer(tmp_path)
    tab = ProjectPrepTab(container)
    before = deepcopy(container.settings["author_presets"])

    class RejectingBox:
        AcceptRole, ActionRole, RejectRole = 0, 1, 2
        Information = 1

        def __init__(self, *_args):
            self.clicked = None

        def setWindowTitle(self, *_args): pass
        def setText(self, *_args): pass
        def setDefaultButton(self, *_args): pass
        def addButton(self, _text, role):
            button = object()
            if role == self.RejectRole:
                self.clicked = button
            return button
        def exec(self): return 0
        def clickedButton(self): return self.clicked

    monkeypatch.setattr("creator_assistant.ui.project_prep_tab.QMessageBox", RejectingBox)
    assert tab._unknown_author_choice(metadata(channel_id="UC-NEW", channel="Unknown"), None) is None
    assert container.settings["author_presets"] == before
    assert container.settings_store.saved is None
    tab.close()
    app.processEvents()
