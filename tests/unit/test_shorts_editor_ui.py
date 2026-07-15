import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from creator_assistant.domain.shorts.models import Candidate, Transcript, TranscriptSegment
from creator_assistant.ui.shorts import candidate_editor as candidate_editor_module
from creator_assistant.ui.shorts.candidate_editor import CandidateEditor
from creator_assistant.ui.shorts.subtitle_editor import SubtitleEditor


def app():
    return QApplication.instance() or QApplication([])


def transcript():
    segment = TranscriptSegment(0, 10, 20, "Длинная русская строка для проверки интерфейса")
    return Transcript("ru", 30, segment.text, [segment])


def test_candidate_timeline_is_relative_to_selected_clip(tmp_path, monkeypatch):
    qt_app = app()
    monkeypatch.setattr(candidate_editor_module, "MULTIMEDIA_AVAILABLE", False)
    editor = CandidateEditor()
    candidate = Candidate("short_001", 10.0, 15.25, 90, "text")
    editor.set_candidate(candidate, tmp_path / "proxy.mp4")
    assert editor.timeline.minimum() == 0
    assert editor.timeline.maximum() == 5250
    assert editor.time_label.text() == "00:00.000 / 00:05.250"
    editor.end.setValue(16.0)
    assert editor.timeline.maximum() == 6000
    assert qt_app is QApplication.instance()


def test_candidate_settings_are_dirty_then_kept_separately(tmp_path):
    qt_app = app()
    paths = SimpleNamespace(subtitles=tmp_path)
    first = Candidate("short_001", 10, 20, 90, "one")
    second = Candidate("short_002", 10, 20, 80, "two")
    editor = SubtitleEditor()
    changed = []
    editor.configuration_changed.connect(changed.append)
    editor.set_context(first, transcript(), paths)
    editor.style.setCurrentIndex(editor.style.findData("gaming"))
    assert "автосохранение" in editor.dirty_label.text()
    editor._apply_configuration()
    assert first.subtitle_settings["style"] == "gaming"
    assert first.subtitle_settings["size"] == 68
    editor.set_context(second, transcript(), paths)
    editor.position.setCurrentIndex(editor.position.findData("upper"))
    editor._apply_configuration()
    assert second.subtitle_settings["position"] == "upper"
    assert first.subtitle_settings["style"] == "gaming"
    assert changed == [first, second]
    assert qt_app is QApplication.instance()
