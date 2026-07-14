import logging
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from creator_assistant.domain.models import ProjectOptions, VideoMetadata
from creator_assistant.domain.youtube_auth import YtDlpAuthContext
from creator_assistant.services.legacy_media_inspector import (
    LegacyMediaProbe,
    LegacyProjectMediaInspector,
)
from creator_assistant.services.project_service import ProjectService
from creator_assistant.ui.media_role_resolution_dialog import MediaRoleResolutionDialog
from creator_assistant.ui.project_prep_tab import PlanStatus, ProjectPrepTab


def app():
    return QApplication.instance() or QApplication([])


def candidate(path: Path) -> dict:
    return {
        "path": str(path),
        "duration": 363.0,
        "audio_codec": "mp3",
        "sample_rate": 48000,
        "channels": 2,
        "size": path.stat().st_size,
        "reason": "Audio-only duration matches",
    }


def test_dialog_single_candidate_uses_qdialog_accepted_and_selected_path(tmp_path: Path):
    app()
    audio = tmp_path / "audio only.mp3"
    audio.write_bytes(b"audio")
    dialog = MediaRoleResolutionDialog(
        title="Video", duration=363, candidates=[candidate(audio)], project_path=tmp_path
    )

    assert "Найден один" in dialog.intro_label.text()
    assert "несколько" not in dialog.intro_label.text()
    assert dialog.use_button.isEnabled()
    dialog._use_selected()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.resolution_action == MediaRoleResolutionDialog.USE_EXISTING_FILE
    assert dialog.selected_path == audio
    dialog.deleteLater()


def test_dialog_multiple_candidates_uses_plural_and_rejects_cleanly(tmp_path: Path):
    app()
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    dialog = MediaRoleResolutionDialog(
        title="Video",
        duration=363,
        candidates=[candidate(first), candidate(second)],
        project_path=tmp_path,
    )

    assert "несколько" in dialog.intro_label.text()
    assert dialog.table.rowCount() == 2
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.resolution_action == MediaRoleResolutionDialog.CANCEL
    dialog.deleteLater()


def test_dialog_call_site_never_uses_instance_accepted_constant():
    source = Path("src/creator_assistant/ui/project_prep_tab.py").read_text(encoding="utf-8")
    assert "dialog.Accepted" not in source
    assert "QDialog.DialogCode.Accepted" in source


def test_audio_candidate_cardinality_has_distinct_states(tmp_path: Path):
    inspector = LegacyProjectMediaInspector(lambda _path, _token: {})
    first = LegacyMediaProbe(path=tmp_path / "first.mp3", size=10, audio_streams=1)
    second = LegacyMediaProbe(path=tmp_path / "second.mp3", size=9, audio_streams=1)

    missing = inspector._select("audio", [], lambda _item: None, "now")
    high = inspector._select("audio", [first], lambda _item: (112, 1.0, "high"), "now")
    medium = inspector._select("audio", [first], lambda _item: (85, 1.0, "medium"), "now")
    multiple = inspector._select(
        "audio", [first, second], lambda item: (112 if item is first else 100, 1.0, "candidate"), "now"
    )

    assert missing.status == "NOT_MATCHED"
    assert high.status == "VALID"
    assert medium.status == "CONFIRMATION_REQUIRED"
    assert len(medium.candidates) == 1
    assert multiple.status == "AMBIGUOUS"
    assert len(multiple.candidates) == 2


def test_vegas_only_dependencies_ignore_unrelated_ambiguous_audio():
    options = ProjectOptions(
        download_maximum=False,
        create_proxy=False,
        download_audio=False,
        create_instrumental=False,
        create_reaper_project=False,
        create_vegas_project=True,
    )
    states = {
        "maximum": {"status": "VALID"},
        "instrumental": {"status": "VALID"},
        "vegas": {"status": "VALID"},
        "audio": {"status": "AMBIGUOUS"},
        "proxy": {"status": "MISSING"},
        "reaper": {"status": "MISSING"},
    }

    assert ProjectService.required_roles(options, states) == {"maximum", "instrumental", "vegas"}
    assert ProjectService.plan_status(options, states) == "ALREADY_COMPLETE"


def test_manual_resolution_metadata_is_preserved_in_manifest_file_entry():
    result = ProjectService._manifest_files_from_record(
        {"audio": r"E:\project\Materials\audio.mp3"},
        {"audio": {
            "status": "VALID",
            "source": "manual_legacy_resolution",
            "confirmed_by_user": True,
            "duration": 363.0,
            "codec": "mp3",
        }},
    )

    assert result["audio"]["confirmed_by_user"] is True
    assert result["audio"]["duration"] == 363.0
    assert result["audio"]["codec"] == "mp3"


class RuntimeStub:
    root = Path("missing")
    marker_path = Path("missing")

    def is_ready(self):
        return False


class SettingsStoreStub:
    def save(self, _settings):
        pass


class ContainerStub:
    def __init__(self, root: Path):
        self.settings = {
            "youtube_root": str(root),
            "author_paths": [str(root)],
            "selected_author_path": str(root),
            "youtube_access": {},
            "open_folder_after_completion": False,
        }
        self.settings_store = SettingsStoreStub()
        self.audio_separator_runtime = RuntimeStub()
        self.logger = logging.getLogger("preflight-test")
        self.youtube_auth = YtDlpAuthContext()
        self.paths = {"yt_dlp": "yt-dlp.exe"}
        self.projects = ProjectService.__new__(ProjectService)


def test_output_checkboxes_allow_only_vegas(tmp_path: Path):
    app()
    tab = ProjectPrepTab(ContainerStub(tmp_path))
    for checkbox in (
        tab.max_check, tab.proxy_check, tab.audio_check,
        tab.instrumental_check, tab.reaper_check,
    ):
        checkbox.setChecked(False)
    tab.vegas_check.setChecked(True)

    assert tab.vegas_check.isEnabled()
    assert tab.vegas_check.isChecked()
    assert all(
        checkbox.isEnabled()
        for checkbox in (
            tab.max_check, tab.proxy_check, tab.audio_check,
            tab.instrumental_check, tab.reaper_check,
        )
    )
    tab.close()


def test_complete_vegas_only_plan_does_not_start_workflow(tmp_path: Path):
    app()
    container = ContainerStub(tmp_path)
    tab = ProjectPrepTab(container)
    project = tmp_path / "Project"
    project.mkdir()
    vegas = project / "Project.veg"
    vegas.write_bytes(b"veg")
    tab.metadata = VideoMetadata("id", "Project", 363, "https://youtu.be/id")
    tab.selected_project_path = project
    tab.current_project_path = project
    tab.current_veg_path = vegas
    tab.project_selection = "resume"
    tab._resume_states = {
        "maximum": {"status": "VALID", "path": str(project / "max.mp4")},
        "instrumental": {"status": "VALID", "path": str(project / "inst.flac")},
        "vegas": {"status": "VALID", "path": str(vegas)},
        "audio": {"status": "AMBIGUOUS"},
        "proxy": {"status": "MISSING"},
        "reaper": {"status": "MISSING"},
    }
    for checkbox in (
        tab.max_check, tab.proxy_check, tab.audio_check,
        tab.instrumental_check, tab.reaper_check,
    ):
        checkbox.setChecked(False)
    tab.vegas_check.setChecked(True)
    tab._refresh_preflight_plan()

    assert tab.plan_status == PlanStatus.ALREADY_COMPLETE
    assert tab.create_button.text() == "Проект уже готов"
    assert not tab.create_button.isEnabled()
    assert tab.overall_progress_bar.value() == 100
    assert tab.open_vegas_button.isEnabled()
    tab.create_project()
    assert tab.active_thread is None
    assert "повторный workflow не запускался" in tab.progress_label.text()
    tab.close()


def test_existing_project_starts_preflight_automatically(monkeypatch, tmp_path: Path):
    app()
    tab = ProjectPrepTab(ContainerStub(tmp_path))
    tab.metadata = VideoMetadata("id", "Project", 363, "https://youtu.be/id")
    project = tmp_path / "Project"
    project.mkdir()
    started = []
    monkeypatch.setattr(tab, "_start_project_migration", started.append)

    tab._detect_existing_project(
        "id", candidates=[{"path": str(project), "preset_name": "Beppo", "confirmed": True}]
    )

    assert started == [project]
    assert "Проверка существующего проекта" in tab.preflight_label.text()
    tab.close()
