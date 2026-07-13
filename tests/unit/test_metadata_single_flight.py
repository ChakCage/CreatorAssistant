import logging
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from creator_assistant.domain.models import VideoFormat, VideoMetadata
from creator_assistant.domain.youtube_auth import YtDlpAuthContext
from creator_assistant.ui.project_prep_tab import ProjectPrepTab
from creator_assistant.services.metadata_request_controller import MetadataRequestController, RetryPolicy


class RuntimeStub:
    root = Path("missing")
    marker_path = Path("missing-marker")

    def is_ready(self):
        return False


class SettingsStoreStub:
    def save(self, _settings):
        pass


class MetadataStub:
    def __init__(self, gate=None):
        self.calls = 0
        self.gate = gate
        self.auth = YtDlpAuthContext()
        self.yt_dlp_path = "yt-dlp.exe"

    def fetch(self, url, cancellation):
        self.calls += 1
        if self.gate:
            self.gate.wait(2)
        video_id = url.split("v=")[-1]
        return VideoMetadata(
            video_id,
            "Single flight",
            12,
            url,
            formats=[
                VideoFormat("maximum", "webm", height=1440, fps=60, vcodec="vp9", filesize=100),
                VideoFormat("proxy", "mp4", height=720, fps=60, vcodec="avc1", filesize=50),
                VideoFormat("audio", "webm", acodec="opus", abr=160, filesize=10),
                VideoFormat("aac", "m4a", acodec="mp4a.40.2", abr=128, filesize=8),
            ],
        )


class ContainerStub:
    def __init__(self, root: Path, metadata):
        self.settings = {
            "youtube_root": str(root), "author_paths": [], "selected_author_path": "",
            "youtube_access": {"mode": "none", "browser": "chrome", "browser_profile": "", "cookies_file": "", "always_use": False},
        }
        self.settings_store = SettingsStoreStub()
        self.audio_separator_runtime = RuntimeStub()
        self.metadata = metadata
        self.logger = logging.getLogger("single-flight-test")
        self.metadata_controller = MetadataRequestController(
            metadata,
            RetryPolicy(retry_delay_min=0, retry_delay_max=0, manual_delay_min=0, manual_delay_max=0),
            self.logger,
        )
        self.youtube_auth = metadata.auth
        self.paths = {"yt_dlp": "yt-dlp.exe"}


def _app():
    return QApplication.instance() or QApplication([])


def _wait(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("UI condition timed out")


def test_insert_does_not_fetch_and_one_click_starts_one_subprocess(tmp_path: Path):
    app = _app()
    metadata = MetadataStub()
    tab = ProjectPrepTab(ContainerStub(tmp_path, metadata))
    tab.url_edit.setText("https://www.youtube.com/watch?v=abcdefghijk")
    app.processEvents()
    assert metadata.calls == 0
    assert tab.fetch_button.isEnabled()
    assert not tab.create_button.isEnabled()
    tab.fetch_button.click()
    _wait(app, lambda: not tab.metadata_request_in_progress)
    assert metadata.calls == 1
    assert tab.create_button.isEnabled()
    tab.fetch_button.click()
    app.processEvents()
    assert metadata.calls == 1  # five-minute session cache
    tab.close()
    app.processEvents()


def test_double_action_is_single_flight_and_stale_response_is_ignored(tmp_path: Path):
    app = _app()
    gate = threading.Event()
    metadata = MetadataStub(gate)
    tab = ProjectPrepTab(ContainerStub(tmp_path, metadata))
    tab.url_edit.setText("https://www.youtube.com/watch?v=abcdefghijk")
    tab.request_metadata("button")
    _wait(app, lambda: metadata.calls == 1)
    tab.request_metadata("enter")
    assert metadata.calls == 1
    tab.url_edit.setText("https://www.youtube.com/watch?v=lmnopqrstuv")
    gate.set()
    _wait(app, lambda: not tab.metadata_request_in_progress)
    assert metadata.calls == 1
    assert tab.metadata is None
    assert not tab.create_button.isEnabled()
    tab.close()
    app.processEvents()


def test_ten_metadata_cycles_leave_no_threads_or_qthread_warning(tmp_path: Path):
    app = _app()
    metadata = MetadataStub()
    tab = ProjectPrepTab(ContainerStub(tmp_path, metadata))
    messages = []
    previous = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    request_ids = []
    original_start = tab._start_metadata_worker

    def tracked_start(function, request_id, generation_id):
        request_ids.append(request_id)
        return original_start(function, request_id, generation_id)

    tab._start_metadata_worker = tracked_start
    try:
        for _cycle in range(10):
            tab.reset_for_new_project("https://www.youtube.com/watch?v=abcdefghijk")
            tab.request_metadata("button")
            _wait(
                app,
                lambda: not tab.metadata_request_in_progress
                and tab.metadata_thread is None
                and not tab._threads,
            )
            assert tab.metadata is not None
        assert len(request_ids) == 10
        assert len(set(request_ids)) == 10
        assert not any("QThread: Destroyed while thread is still running" in value for value in messages)
    finally:
        qInstallMessageHandler(previous)
        tab.close()
        app.processEvents()
