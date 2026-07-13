import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from creator_assistant.domain.errors import JobCancelledError, ValidationError
from creator_assistant.domain.job import CancellationToken, JobState, JobStatus
from creator_assistant.domain.stages import JobStage
from creator_assistant.infrastructure.dependency_detector import (
    SOURCE_PATH,
    SOURCE_SAVED,
    DependencyDetector,
)
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.infrastructure.process_runner import ProcessRunner, ProcessResult
from creator_assistant.services.media_validation_service import MediaValidationService


def test_job_state_transitions_and_restart():
    state = JobState()
    state.start()
    state.complete_stage(JobStage.VALIDATE_URL)
    state.complete_stage(JobStage.FETCH_METADATA)
    state.cancel()
    assert state.status == JobStatus.CANCELLED
    state.start()
    assert state.status == JobStatus.RUNNING


def test_job_rejects_transition_while_pending():
    with pytest.raises(ValidationError):
        JobState().advance(JobStage.FETCH_METADATA)


def test_cancellation_token():
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelledError):
        token.raise_if_cancelled()


def test_process_runner_cancels_child():
    token = CancellationToken()
    runner = ProcessRunner()
    caught = []

    def run():
        try:
            runner.run([sys.executable, "-c", "import time; time.sleep(20)"], cancellation=token)
        except Exception as exc:
            caught.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.3)
    token.cancel()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert isinstance(caught[0], JobCancelledError)


def test_job_store_only_resumes_owned_project(tmp_path: Path):
    project = tmp_path / "Проект"
    (project / "Материалы").mkdir(parents=True)
    store = JobStore(tmp_path / "state")
    store.save("abc", {"created_by": "CreatorAssistant", "status": "failed", "project_path": str(project)})
    assert store.resumable_path("abc") == project
    store.save("foreign", {"status": "failed", "project_path": str(project)})
    assert store.resumable_path("foreign") is None


def test_missing_dependency_is_reported():
    assert DependencyDetector.executable("", ("creator-assistant-command-that-does-not-exist",)) == ""


class DependencyRunner:
    def run(self, command, **kwargs):
        name = Path(command[0]).name.casefold()
        if "yt-dlp" in name:
            output = "2026.07.04"
        elif "ffprobe" in name:
            output = "ffprobe version 8.0"
        elif "ffmpeg" in name:
            output = "ffmpeg version 8.0"
        else:
            output = ""
        return ProcessResult(list(command), 0, output)


def test_valid_saved_yt_dlp_is_preserved_and_has_source(tmp_path: Path):
    executable = tmp_path / "yt-dlp.exe"
    executable.write_bytes(b"exe")
    settings = {"yt_dlp_path": str(executable), "dependency_sources": {}}
    detector = DependencyDetector(DependencyRunner())
    resolution = detector.discover(settings)["yt_dlp"]
    assert resolution.path == str(executable.resolve())
    assert resolution.version == "2026.07.04"
    assert resolution.source == SOURCE_SAVED


def test_invalid_saved_path_triggers_path_search(monkeypatch, tmp_path: Path):
    executable = tmp_path / "yt-dlp.exe"
    executable.write_bytes(b"exe")
    fake_python = tmp_path / "runtime" / "python.exe"
    fake_python.parent.mkdir()
    monkeypatch.setattr("creator_assistant.infrastructure.dependency_detector.sys.executable", str(fake_python))
    monkeypatch.setattr("creator_assistant.infrastructure.dependency_detector.local_data_root", lambda: tmp_path / "local")
    monkeypatch.setattr("creator_assistant.infrastructure.dependency_detector.Path.home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr("creator_assistant.infrastructure.dependency_detector.shutil.which", lambda name: str(executable) if "yt-dlp" in name else None)
    settings = {"youtube_root": str(tmp_path / "youtube"), "yt_dlp_path": str(tmp_path / "missing.exe"), "dependency_sources": {}}
    detector = DependencyDetector(DependencyRunner())
    resolutions = detector.discover(settings)
    assert resolutions["yt_dlp"].source == SOURCE_PATH
    assert detector.apply_to_settings(settings, resolutions)
    assert settings["yt_dlp_path"] == str(executable.resolve())


def test_ffprobe_is_discovered_next_to_ffmpeg(tmp_path: Path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"exe")
    ffprobe.write_bytes(b"exe")
    detector = DependencyDetector(DependencyRunner())
    resolutions = detector.discover({"ffmpeg_path": str(ffmpeg), "dependency_sources": {}})
    assert resolutions["ffmpeg"].path == str(ffmpeg.resolve())
    assert resolutions["ffprobe"].path == str(ffprobe.resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows-only subprocess flags")
def test_process_runner_uses_hidden_windows_flags():
    startupinfo, flags = ProcessRunner._hidden_process_options()
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startupinfo.wShowWindow == subprocess.SW_HIDE


def test_process_runner_decodes_utf8_without_damaging_cyrillic():
    runner = ProcessRunner()
    value = r"E:\YouTube\MylesMC\Делаю\Название\Материалы\video.mkv"
    assert runner._decode_bytes(value.encode("utf-8"), "stdout") == value


def test_process_runner_uses_safe_system_encoding_fallback(monkeypatch):
    runner = ProcessRunner()
    value = "Казак — аудио.flac"
    monkeypatch.setattr("creator_assistant.infrastructure.process_runner.locale.getpreferredencoding", lambda _do_setlocale=False: "cp1251")
    assert runner._decode_bytes(value.encode("cp1251"), "stderr") == value


class FakeRunner:
    def run(self, command, **kwargs):
        return ProcessResult(list(command), 0, json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "12.0"}}))


def test_ready_file_validation(tmp_path: Path):
    media = tmp_path / "готово.mp4"
    media.write_bytes(b"data")
    service = MediaValidationService(FakeRunner(), "ffprobe")
    assert service.validate_video(media, CancellationToken())["format"]["duration"] == "12.0"
