from pathlib import Path
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from creator_assistant.domain.errors import JobCancelledError, ManualActionRequiredError, ProcessExecutionError, ValidationError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.services.stem_separation.uvr_manual_fallback import UvrManualFallbackBackend
from creator_assistant.ui.workers import FunctionWorker
from creator_assistant.ui.manual_uvr_dialog import ManualUvrDialog
from PySide6.QtWidgets import QApplication, QLabel, QPushButton


def test_manual_fallback_never_launches_gui_or_waits(tmp_path: Path):
    launcher = tmp_path / "UVR_Launcher.exe"
    launcher.write_bytes(b"exe")
    materials = tmp_path / "MylesMC" / "Материалы"
    materials.mkdir(parents=True)
    source = materials / "Current [Audio].webm"
    source.write_bytes(b"audio")
    expected = materials / "Current [Instrumental].flac"
    backend = UvrManualFallbackBackend(launcher)
    with pytest.raises(ManualActionRequiredError) as caught:
        backend.separate(source, expected, CancellationToken())
    assert caught.value.source == source
    assert caught.value.expected_output == expected
    assert not expected.exists()


def test_manual_fallback_rejects_input_from_another_project(tmp_path: Path):
    launcher = tmp_path / "UVR_Launcher.exe"
    launcher.write_bytes(b"exe")
    source = tmp_path / "Beppo" / "Материалы" / "old.webm"
    output = tmp_path / "MylesMC" / "Материалы" / "new.flac"
    source.parent.mkdir(parents=True)
    output.parent.mkdir(parents=True)
    source.write_bytes(b"audio")
    with pytest.raises(ValidationError):
        UvrManualFallbackBackend(launcher).separate(source, output, CancellationToken())


def test_worker_emits_cancelled_without_failed_or_traceback():
    def work(_progress):
        raise JobCancelledError("Операция отменена пользователем.")

    worker = FunctionWorker(work)
    events = []
    worker.cancelled.connect(lambda: events.append("cancelled"))
    worker.failed.connect(lambda *_args: events.append("failed"))
    worker.run()
    assert events == ["cancelled"]


def test_worker_emits_manual_action_instead_of_failed(tmp_path: Path):
    request = ManualActionRequiredError(tmp_path / "in.webm", tmp_path / "out.flac", tmp_path / "UVR.exe")

    def work(_progress):
        raise request

    worker = FunctionWorker(work)
    events = []
    worker.manual_action_required.connect(lambda value: events.append(value))
    worker.failed.connect(lambda *_args: events.append("failed"))
    worker.run()
    assert events == [request]


def test_no_output_watchdog_stops_silent_process():
    with pytest.raises(ProcessExecutionError, match="обработка не началась"):
        ProcessRunner().run(
            [__import__("sys").executable, "-c", "import time; time.sleep(10)"],
            no_output_timeout=0.2,
        )


def test_manual_dialog_shows_current_job_paths_and_required_actions(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    materials = tmp_path / "MylesMC" / "Делаю" / "Current" / "Материалы"
    materials.mkdir(parents=True)
    source = materials / "Current [Audio].webm"
    output = materials / "Current [Instrumental].flac"
    launcher = tmp_path / "UVR_Launcher.exe"
    dialog = ManualUvrDialog(source, output, launcher, lambda _path: None)
    visible_text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    button_texts = {button.text() for button in dialog.findChildren(QPushButton)}
    assert str(source) in visible_text
    assert str(output.parent) in visible_text
    assert "UVR-MDX-NET Inst HQ 3" in visible_text
    assert {"Открыть UVR", "Проверить результат", "Отмена"} <= button_texts
    dialog.close()
    app.processEvents()
