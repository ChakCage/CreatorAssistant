import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
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
    editor.resize(1000, 700)
    editor.show()
    qt_app.processEvents()
    values = []
    editor.timeline.seek_requested.connect(values.append)
    y = max(1, editor.timeline.height() // 2)
    QTest.mousePress(editor.timeline, Qt.LeftButton, pos=QPoint(20, y))
    QTest.mouseMove(editor.timeline, QPoint(max(30, editor.timeline.width() - 20), y))
    QTest.mouseRelease(editor.timeline, Qt.LeftButton, pos=QPoint(max(30, editor.timeline.width() - 20), y))
    assert values[-1] > 5000
    assert not editor.timeline._dragging
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
    assert first.subtitle_settings["size"] == 70
    editor.set_context(second, transcript(), paths)
    editor.position.setCurrentIndex(editor.position.findData("upper"))
    editor._apply_configuration()
    assert second.subtitle_settings["position"] == "upper"
    assert first.subtitle_settings["style"] == "gaming"
    assert changed == [first, second]
    assert qt_app is QApplication.instance()


def test_boundary_change_rebuilds_only_local_candidate_subtitles(tmp_path):
    qt_app = app()
    paths = SimpleNamespace(subtitles=tmp_path)
    source_transcript = Transcript("ru", 30, "all", [
        TranscriptSegment(0, 6, 8, "Новая ранняя реплика"),
        TranscriptSegment(1, 12, 14, "Реплика из старого диапазона"),
        TranscriptSegment(2, 22, 24, "Не должна попасть"),
    ])
    candidate = Candidate("short_001", 10, 20, 90, "one")
    editor = SubtitleEditor()
    editor.set_context(candidate, source_transcript, paths)
    candidate.start, candidate.end = 5, 15
    cues = editor.rebuild_for_boundaries(candidate, source_transcript)
    assert [cue.text for cue in cues] == ["Новая ранняя реплика", "Реплика из старого диапазона"]
    assert [(cue.start, cue.end) for cue in cues] == [(1, 3), (7, 9)]
    assert candidate.subtitle_settings["cues"][0]["text"] == "Новая ранняя реплика"
    assert editor.table.rowCount() == 2
    assert qt_app is QApplication.instance()


def test_layout_panel_exposes_solid_color_and_150_percent_scale():
    qt_app = app()
    editor = SubtitleEditor()
    assert editor.vertical.mode.findData("solid_color") >= 0
    assert editor.vertical.foreground.maximum() == 150
    editor.vertical.mode.setCurrentIndex(editor.vertical.mode.findData("solid_color"))
    editor.vertical.foreground.setValue(150)
    assert editor.vertical.value()["background_color"] == "black"
    assert editor.vertical.value()["foreground_scale"] == 150
    assert qt_app is QApplication.instance()


def test_solid_color_preview_has_black_canvas_and_scaled_foreground(tmp_path):
    qt_app = app()
    thumbnail = tmp_path / "frame.png"
    image = QImage(160, 90, QImage.Format_RGB32)
    image.fill(QColor("red"))
    assert image.save(str(thumbnail))
    candidate = Candidate("short_001", 0, 5, 1, "", thumbnail=str(thumbnail))
    editor = SubtitleEditor()
    editor.preview.resize(270, 480)
    editor.preview.set_preview(
        candidate,
        {"style": "clean", "position": "upper"},
        {"mode": "solid_color", "foreground_scale": 150, "background_color": "black"},
        "Preview",
    )
    editor.preview.show()
    qt_app.processEvents()
    preview = editor.preview.grab().toImage()
    assert preview.pixelColor(135, 460).lightness() < 20
    assert preview.pixelColor(135, 240).red() > 180
    assert qt_app is QApplication.instance()


def test_candidate_editor_shows_only_three_deduplicated_alternatives(tmp_path, monkeypatch):
    qt_app = app()
    monkeypatch.setattr(candidate_editor_module, "MULTIMEDIA_AVAILABLE", False)
    editor = CandidateEditor()
    candidate = Candidate(
        "short_001", 10, 55, 90, "основной текст",
        alternatives=[
            [8, 53], [8.4, 53.2], [12, 57], [20, 75], [1, 70], [30, 34], [14, 58],
        ],
    )
    editor.set_candidate(candidate, tmp_path / "proxy.mp4", transcript())
    assert editor.alternatives.count() == 4
    assert "Основные границы" in editor.alternatives.itemText(0)
    assert "Вариант 1" in editor.alternatives.itemText(1)
    assert qt_app is QApplication.instance()


def test_candidate_editor_alternative_selection_updates_active_range(tmp_path, monkeypatch):
    qt_app = app()
    monkeypatch.setattr(candidate_editor_module, "MULTIMEDIA_AVAILABLE", False)
    editor = CandidateEditor()
    candidate = Candidate("short_001", 10, 20, 90, "старый текст", alternatives=[[6, 16]])
    changed = []
    editor.active_boundary_changed.connect(lambda item, start, end, variant: changed.append((item.id, start, end, variant)))
    editor.set_candidate(candidate, tmp_path / "proxy.mp4", transcript())
    editor.alternatives.setCurrentIndex(1)
    assert editor.start.value() == 6
    assert editor.end.value() == 16
    assert changed[-1] == ("short_001", 6, 16, "alt_001")
    assert qt_app is QApplication.instance()


def test_subtitle_offset_autosaves_and_preview_uses_scaled_geometry(tmp_path):
    qt_app = app()
    paths = SimpleNamespace(subtitles=tmp_path)
    candidate = Candidate("short_001", 10, 20, 90, "one")
    editor = SubtitleEditor()
    editor.set_context(candidate, transcript(), paths)
    editor.offset.setValue(100)
    editor._apply_configuration()
    assert candidate.subtitle_settings["vertical_offset"] == 100
    editor.preview.resize(216, 384)
    editor.preview.set_preview(candidate, candidate.subtitle_settings, candidate.layout_settings, "Крупный пример")
    image = editor.preview.grab().toImage()
    assert image.width() == 216
    assert qt_app is QApplication.instance()
