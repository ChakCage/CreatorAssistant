import json
import logging
import os
import threading
import time
from pathlib import Path
import shiboken6

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import ProgressInfo, ProjectResult, VideoMetadata
from creator_assistant.domain.youtube_auth import YtDlpAuthContext
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.services.project_service import ProjectService
from creator_assistant.ui.project_prep_tab import ProjectPrepTab, UiJobState
from creator_assistant.infrastructure.manifest_store import ManifestLoader, ManifestMigrator, ManifestStatus, ManifestWriter


def metadata(video_id="5nTuu0FzAUg", title="100 Players Simulate Minecraft's Magical Purge"):
    return VideoMetadata(video_id, title, 12, f"https://youtu.be/{video_id}", formats=[])


def registry(tmp_path: Path) -> ProjectService:
    service = ProjectService.__new__(ProjectService)
    service.job_store = JobStore(tmp_path / "jobs")
    service.manifest_loader = ManifestLoader()
    service.manifest_writer = ManifestWriter(service.manifest_loader)
    service.manifest_migrator = ManifestMigrator()
    return service


def test_planned_path_never_auto_selects_numbered_copy(tmp_path: Path):
    service = registry(tmp_path)
    item = metadata()
    exact = tmp_path / item.title
    exact.mkdir()
    (tmp_path / f"{item.title} (2)").mkdir()
    assert service.planned_path(tmp_path, item) == exact
    assert service.copy_path(tmp_path, item) == tmp_path / f"{item.title} (3)"


def test_discovery_prefers_video_id_record_over_title_and_numbered_folders(tmp_path: Path):
    service = registry(tmp_path)
    item = metadata()
    correct = tmp_path / "renamed project"
    correct.mkdir()
    exact = tmp_path / item.title
    exact.mkdir()
    numbered = tmp_path / f"{item.title} (3)"
    numbered.mkdir()
    service.job_store.save(item.video_id, {
        "created_by": "CreatorAssistant", "status": "cancelled", "project_path": str(correct)
    })
    found = service.find_existing_projects(tmp_path, item)
    assert found[0]["path"] == correct
    assert found[0]["source"] == "job_store"
    assert exact in [candidate["path"] for candidate in found]
    assert numbered not in [candidate["path"] for candidate in found]


def test_manifest_discovery_and_explicit_legacy_binding(tmp_path: Path):
    service = registry(tmp_path)
    item = metadata()
    project = tmp_path / "legacy name"
    project.mkdir()
    (project / service.MANIFEST_NAME).write_text(json.dumps({"video_id": item.video_id}), encoding="utf-8")
    assert service.find_existing_projects(tmp_path, item)[0]["path"] == project

    legacy = tmp_path / item.title
    legacy.mkdir()
    other = metadata("other-id", item.title)
    candidates = service.find_existing_projects(tmp_path, other)
    assert candidates[0]["confirmed"] is False
    stale = tmp_path / f"{item.title} (2)"
    service.job_store.save("other-id", {
        "created_by": "CreatorAssistant", "status": "cancelled", "project_path": str(stale)
    })
    service.bind_existing(other, legacy)
    manifest = json.loads((legacy / service.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["video_id"] == "other-id"
    assert service.job_store.project_path("other-id") == legacy
    assert service.job_store.load("other-id")["project_paths"] == [str(stale), str(legacy)]


class RuntimeStub:
    root = Path("missing")
    marker_path = Path("missing-marker")

    def is_ready(self):
        return False


class SettingsStoreStub:
    def save(self, _settings):
        pass


class ContainerStub:
    def __init__(self, root: Path):
        self.settings = {
            "youtube_root": str(root), "author_paths": [str(root)],
            "selected_author_path": str(root), "youtube_access": {},
            "open_folder_after_completion": False,
        }
        self.settings_store = SettingsStoreStub()
        self.audio_separator_runtime = RuntimeStub()
        self.logger = logging.getLogger("project-identity-test")
        self.youtube_auth = YtDlpAuthContext()
        self.paths = {"yt_dlp": "yt-dlp.exe"}


def wait(app, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("UI condition timed out")


def test_reset_preserves_preferences_and_ignores_late_worker_result(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    tab = ProjectPrepTab(ContainerStub(tmp_path))
    tab.max_check.setChecked(False)
    selected_author = tab.author_combo.currentData()
    tab.metadata = metadata()
    tab.url_edit.setText(tab.metadata.webpage_url)
    tab.active_job_id = "old-job"
    tab._job_generation = 7
    tab.token = CancellationToken()
    tab._set_job_state(UiJobState.RUNNING)
    gate = threading.Event()
    delivered = []

    def work(_progress):
        gate.wait(1)
        return "late"

    tab._start_worker(work, delivered.append, lambda *_args: None)
    old_worker = tab.active_worker
    tab.reset_for_new_project()
    gate.set()
    wait(app, lambda: not tab._threads)
    assert delivered == []
    app.processEvents()
    assert not shiboken6.isValid(old_worker)
    assert tab.job_state == UiJobState.IDLE
    assert tab.metadata is None
    assert tab.active_job_id is None
    assert tab.author_combo.currentData() == selected_author
    assert not tab.max_check.isChecked()
    assert not tab.create_button.isEnabled()
    tab._job_generation += 1
    tab.active_job_id = "new-job"
    tab._set_job_state(UiJobState.PREPARING)
    tab._start_worker(lambda _progress: "project-b", delivered.append, lambda *_args: None)
    wait(app, lambda: not tab._threads)
    assert delivered == ["project-b"]
    tab.close()


def test_ui_legacy_migration_and_continue_finishes_five_times_with_zero_threads(monkeypatch, tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    container = ContainerStub(tmp_path)
    service = registry(tmp_path)
    project_jobs = []

    def execute(_destination, _metadata, _options, _token, progress, resume_path, _new_path):
        project_jobs.append(str(resume_path))
        progress(ProgressInfo("Готово", "Тестовый resume завершён", 100.0))
        return ProjectResult(resume_path, [], resumed=True)

    service.execute = execute
    container.projects = service
    project = tmp_path / "100 Players Simulate Minecraft's Magical Purge"
    project.mkdir()
    user_file = project / "custom-user-file.bin"
    user_file.write_bytes(b"preserve-me")
    tab = ProjectPrepTab(container)
    tab.metadata = metadata()
    tab.url_edit.blockSignals(True)
    tab.url_edit.setText(tab.metadata.webpage_url)
    tab.url_edit.blockSignals(False)
    messages = []
    previous = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    monkeypatch.setattr("creator_assistant.ui.project_prep_tab.QMessageBox.information", lambda *_args: 0)
    try:
        for _cycle in range(5):
            tab._start_project_migration(project)
            wait(app, lambda: tab.active_thread is None and not tab._threads)
            assert tab.project_selection == "resume"
            assert tab.create_button.isEnabled()
            assert ManifestLoader().load(project / service.MANIFEST_NAME).status == ManifestStatus.VALID
            assert user_file.read_bytes() == b"preserve-me"
            assert len(tab._threads) == 0
            tab.create_project()
            wait(app, lambda: tab.active_thread is None and not tab._threads)
            assert tab.job_state == UiJobState.COMPLETED
            assert len(tab._threads) == 0
        assert len(project_jobs) == 5
        assert not any("QThread: Destroyed while thread is still running" in value for value in messages)
    finally:
        qInstallMessageHandler(previous)
        tab.close()


def test_critical_ui_slot_exception_is_caught_without_closing_app(monkeypatch, tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    tab = ProjectPrepTab(ContainerStub(tmp_path))
    shown = []
    monkeypatch.setattr(
        "creator_assistant.ui.project_prep_tab.ErrorDialog.exec",
        lambda self: shown.append(True) or 0,
    )

    def fail():
        raise RuntimeError("simulated slot failure")

    tab._safe_ui_action("test_failure", fail)()
    app.processEvents()
    assert shown == [True]
    assert tab.job_state == UiJobState.FAILED
    assert QApplication.instance() is app
    tab.close()


def test_completed_project_has_separate_exact_folder_and_rpp_actions(monkeypatch, tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    container = ContainerStub(tmp_path)
    project = tmp_path / "exact project"
    project.mkdir()
    rpp = project / "exact.rpp"
    rpp.write_text("<REAPER_PROJECT", encoding="utf-8")
    opened = []
    monkeypatch.setattr("creator_assistant.ui.project_prep_tab.os.startfile", lambda path: opened.append(("folder", path)))
    monkeypatch.setattr("creator_assistant.ui.project_prep_tab.QMessageBox.information", lambda *_args: 0)
    tab = ProjectPrepTab(container)
    tab._project_ready(ProjectResult(project, [], {"reaper": rpp}))
    assert tab.open_folder_button.isEnabled()
    assert tab.open_rpp_button.isEnabled()
    assert not tab.create_button.isEnabled()
    tab.open_current_project_folder()
    tab.open_current_rpp()
    assert opened == [("folder", str(project)), ("folder", str(rpp))]
    tab.reset_for_new_project()
    assert tab.current_project_path is None
    assert tab.current_rpp_path is None
    assert not tab.open_folder_button.isEnabled()
    assert not tab.open_rpp_button.isEnabled()
    tab.close()
    app.processEvents()
