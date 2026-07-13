import logging
import os
import time
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QSignalSpy

from creator_assistant.app import ServiceContainer
from creator_assistant.domain.models import ProjectOptions, VideoFormat, VideoMetadata, ProgressInfo
from creator_assistant.domain.stages import JobStage, ORDERED_STAGES
from creator_assistant.domain.youtube_auth import YtDlpAuthContext
from creator_assistant.infrastructure.manifest_store import ManifestStatus, ManifestValidator
from creator_assistant.infrastructure.settings_store import DEFAULT_SETTINGS
from creator_assistant.ui.project_prep_tab import ProjectPrepTab, UiJobState
from creator_assistant.ui.settings_dialog import SettingsDialog
from creator_assistant.services.settings_service import SettingsService


class RuntimeStub:
    marker_path = Path("missing")

    def is_ready(self):
        return False


class StoreStub:
    def __init__(self):
        self.saved = None

    def save(self, settings):
        self.saved = deepcopy(settings)


class ContainerStub:
    def __init__(self, root: Path):
        initial_settings = deepcopy(DEFAULT_SETTINGS)
        initial_settings.update({
            "youtube_root": str(root),
            "author_paths": [str(root)],
            "selected_author_path": str(root),
            "reaper_proxy_height": 720,
            "temp_root": str(root / "temp"),
        })
        self.settings_store = StoreStub()
        self.audio_separator_runtime = RuntimeStub()
        self.logger = logging.getLogger("proxy-quality-live-ui")
        self.youtube_auth = YtDlpAuthContext()
        self.paths = {"yt_dlp": "yt-dlp.exe"}
        self.save_calls = 0
        self.diagnostics_calls = 0
        self.rebuild_calls = 0
        self.settings_service = SettingsService(self.settings_store, initial_settings, self.logger)

    @property
    def settings(self):
        return self.settings_service.settings

    def save_settings(self, settings, started_at=None):
        self.save_calls += 1

        return ServiceContainer.save_settings(self, settings, started_at=started_at)

    def auto_detect_dependencies(self, rebuild=True):
        self.diagnostics_calls += 1
        raise AssertionError("proxy-only save must not run dependency diagnostics")

    def rebuild(self):
        self.rebuild_calls += 1
        raise AssertionError("proxy-only save must not rebuild services")


def app():
    return QApplication.instance() or QApplication([])


def metadata():
    gib = 1024**3
    return VideoMetadata(
        "proxy-live", "Proxy live", 10, "https://youtu.be/abcdefghijk",
        formats=[
            VideoFormat("1080", "mp4", height=1080, fps=30, vcodec="avc1", filesize=int(1.2 * gib)),
            VideoFormat("720", "mp4", height=720, fps=30, vcodec="avc1", filesize=int(0.8 * gib)),
            VideoFormat("480", "mp4", height=480, fps=30, vcodec="avc1", filesize=int(0.4 * gib)),
            VideoFormat("audio", "m4a", acodec="mp4a.40.2", filesize=int(0.1 * gib)),
        ],
    )


def proxy_stage_text(tab):
    return tab.stage_list.item(ORDERED_STAGES.index(JobStage.CREATE_PROXY)).text()


def test_saved_quality_updates_open_tab_and_estimate_without_worker(tmp_path: Path):
    application = app()
    container = ContainerStub(tmp_path)
    tab = ProjectPrepTab(container)
    tab.metadata = metadata()
    emitted = QSignalSpy(container.settings_service.settings_changed)
    thread_count = len(tab._threads)

    updated = deepcopy(container.settings)
    updated["reaper_proxy_height"] = 480
    container.save_settings(updated, started_at=time.monotonic())
    assert "480p" in tab.proxy_check.text()
    assert "480p" in tab.info_captions["proxy_size"].text()
    assert "480p" in proxy_stage_text(tab)
    assert tab.info_labels["proxy_size"].text() == "≈ 0.5 ГБ"

    updated = deepcopy(container.settings)
    updated["reaper_proxy_height"] = 1080
    container.save_settings(updated, started_at=time.monotonic())
    assert "1080p" in tab.proxy_check.text()
    assert "1080p" in tab.info_captions["proxy_size"].text()
    assert "1080p" in proxy_stage_text(tab)
    assert tab.info_labels["proxy_size"].text() == "≈ 1.3 ГБ"
    assert len(tab._threads) == thread_count == 0
    assert emitted.count() == 2
    assert [emitted.at(index)[0]["reaper_proxy_height"] for index in range(emitted.count())] == [480, 1080]
    assert container.settings_service.last_timing["ui_latency_ms"] < 200
    assert container.diagnostics_calls == 0
    assert container.rebuild_calls == 0
    assert tab.options().reaper_proxy_height == 1080
    tab.close()
    application.processEvents()


def test_settings_save_and_cancel_have_distinct_live_behavior(tmp_path: Path):
    application = app()
    container = ContainerStub(tmp_path)
    tab = ProjectPrepTab(container)
    emitted = QSignalSpy(container.settings_service.settings_changed)

    dialog = SettingsDialog(container)
    dialog.proxy_height.setCurrentIndex(dialog.proxy_height.findData(480))
    dialog._save()
    assert container.save_calls == 1
    assert emitted.count() == 1
    assert container.settings["reaper_proxy_height"] == 480
    assert "480p" in proxy_stage_text(tab)

    cancelled = SettingsDialog(container)
    cancelled.proxy_height.setCurrentIndex(cancelled.proxy_height.findData(1080))
    cancelled.reject()
    assert container.save_calls == 1
    assert emitted.count() == 1
    assert container.settings["reaper_proxy_height"] == 480
    assert "480p" in proxy_stage_text(tab)
    tab.close()
    application.processEvents()


def test_running_job_keeps_snapshot_until_terminal_state(monkeypatch, tmp_path: Path):
    application = app()
    container = ContainerStub(tmp_path)
    tab = ProjectPrepTab(container)
    tab.metadata = metadata()
    tab.active_job_proxy_height = 480
    tab._set_job_state(UiJobState.RUNNING)
    updated = deepcopy(container.settings)
    updated["reaper_proxy_height"] = 1080
    container.save_settings(updated, started_at=time.monotonic())
    assert "1080p" in tab.progress_label.text()
    assert "следующему проекту" in tab.progress_label.text()
    assert "480p" in proxy_stage_text(tab)
    options = ProjectOptions(reaper_proxy_height=480)
    tab._reset_progress(options)
    tab._progress(ProgressInfo(JobStage.CREATE_PROXY.value, "кодирование", 38))
    assert "480p" in proxy_stage_text(tab)
    assert "480p" in tab.progress_label.text()
    assert "720p" not in tab.log.toPlainText()

    tab._set_job_state(UiJobState.COMPLETED)
    assert "1080p" in proxy_stage_text(tab)
    tab.close()
    application.processEvents()


def test_each_proxy_height_emits_exactly_once_and_never_starts_heavy_work(tmp_path: Path):
    application = app()
    container = ContainerStub(tmp_path)
    tab = ProjectPrepTab(container)
    spy = QSignalSpy(container.settings_service.settings_changed)

    for expected_count, height in enumerate((480, 720, 1080), start=1):
        updated = deepcopy(container.settings)
        updated["reaper_proxy_height"] = height
        started = time.monotonic()
        container.save_settings(updated, started_at=started)
        assert spy.count() == expected_count
        assert spy.at(expected_count - 1)[0]["reaper_proxy_height"] == height
        assert container.settings_service.last_timing["ui_latency_ms"] < 200
        assert container.settings_store.saved["reaper_proxy_height"] == height

    assert container.diagnostics_calls == 0
    assert container.rebuild_calls == 0
    assert len(tab._threads) == 0
    tab.close()
    application.processEvents()


def test_schema_v2_manifest_without_proxy_height_defaults_to_720(tmp_path: Path):
    result = ManifestValidator.parse(
        {
            "schema_version": 2,
            "video_id": "legacy",
            "project_path": str(tmp_path),
            "materials_path": str(tmp_path / "Материалы"),
        },
        tmp_path / ".creator-assistant.json",
    )
    assert result.status == ManifestStatus.VALID
    assert result.manifest.reaper_proxy_height == 720
