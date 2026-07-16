import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from creator_assistant.domain.shorts.models import Candidate, SourceInfo, Transcript, TranscriptSegment
from creator_assistant.services.shorts.cache import ShortsCache
from creator_assistant.domain.shorts.models import ShortsManifest
from creator_assistant.ui.shorts import candidate_editor as candidate_editor_module
from creator_assistant.ui.shorts.candidate_editor import CandidateEditor
from creator_assistant.ui.shorts.subtitle_editor import SubtitleEditor
from creator_assistant.ui.shorts.render_queue import RenderQueue


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


def test_layout_panel_exposes_solid_color_and_550_percent_scale():
    qt_app = app()
    editor = SubtitleEditor()
    assert editor.vertical.mode.findData("solid_color") >= 0
    assert editor.vertical.foreground.maximum() == 550
    assert editor.vertical.foreground.minimum() == 50
    editor.vertical.mode.setCurrentIndex(editor.vertical.mode.findData("solid_color"))
    editor.vertical.foreground.setValue(550)
    assert editor.vertical.value()["background_color"] == "black"
    assert editor.vertical.value()["foreground_scale"] == 550
    editor.vertical.mode.setCurrentIndex(editor.vertical.mode.findData("center_crop"))
    assert not editor.vertical.foreground.isEnabled()
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
    assert abs(preview.width() / preview.height() - 9 / 16) < 0.01
    assert preview.pixelColor(preview.width() // 2, preview.height() // 2).red() > 180
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
    assert abs(image.width() / image.height() - 9 / 16) < 0.01
    assert qt_app is QApplication.instance()


def test_stage_fingerprints_survive_candidate_settings_update(tmp_path):
    proxy = tmp_path / "analysis_proxy.mp4"
    proxy.write_bytes(b"proxy")
    manifest = ShortsManifest(1, "shorts", "source.mp4", "fp", 10, 1, 120)
    cache = ShortsCache(manifest)
    proxy_settings = {"height": 720, "codec": "h264"}
    cache.mark_complete("proxy", proxy_settings)
    stage_fingerprints = dict(manifest.analysis_settings.get("stage_fingerprints", {}))
    candidate_config = {"minimum": 25, "desired": 60, "maximum": 100, "content_type": "gaming"}
    manifest.analysis_settings = {**candidate_config, "stage_fingerprints": stage_fingerprints}
    assert ShortsCache(manifest).stage_valid("proxy", proxy, proxy_settings)
    assert not ShortsCache(manifest).stage_valid("candidates", tmp_path / "candidates.json", {"minimum": 25})


def test_subtitle_defaults_and_candidate_override_are_separate(tmp_path):
    qt_app = app()
    paths = SimpleNamespace(subtitles=tmp_path)
    candidate = Candidate("short_001", 10, 20, 90, "one")
    editor = SubtitleEditor()
    saved_defaults = []
    editor.defaults_requested.connect(saved_defaults.append)
    editor.set_context(candidate, transcript(), paths)
    editor.offset.setValue(120)
    editor._save_defaults()
    assert saved_defaults == [candidate]
    assert candidate.subtitle_settings["vertical_offset"] == 120
    other = Candidate("short_002", 10, 20, 90, "two")
    assert other.subtitle_settings == {}
    assert qt_app is QApplication.instance()


def test_render_queue_checkbox_is_independent_from_approved_status(tmp_path):
    qt_app = app()
    first = Candidate("short_001", 0, 10, 90, "one", status="approved")
    second = Candidate("short_002", 0, 10, 90, "two", status="approved")
    queue = RenderQueue()
    queue.set_context([first, second], tmp_path)
    assert queue.table.item(0, 0).checkState() == Qt.Unchecked
    assert queue.table.item(1, 0).checkState() == Qt.Unchecked
    queue.table.item(0, 0).setCheckState(Qt.Checked)
    emitted = []
    queue.render_requested.connect(emitted.append)
    queue._render()
    assert emitted and [item.id for item in emitted[-1]] == ["short_001"]
    assert first.selected_for_render is True
    assert second.selected_for_render is False
    assert qt_app is QApplication.instance()


def test_vertical_editor_timeline_drags_and_subtitle_row_seeks(tmp_path):
    qt_app = app()
    paths = SimpleNamespace(subtitles=tmp_path, cache=tmp_path)
    candidate = Candidate("short_001", 10, 20, 90, "one")
    editor = SubtitleEditor()
    editor.set_context(candidate, transcript(), paths)
    assert editor.timeline.maximum() == 10_000
    editor.resize(1100, 900)
    editor.show()
    qt_app.processEvents()
    y = max(1, editor.timeline.height() // 2)
    QTest.mousePress(editor.timeline, Qt.LeftButton, pos=QPoint(15, y))
    QTest.mouseMove(editor.timeline, QPoint(max(30, editor.timeline.width() - 15), y))
    QTest.mouseRelease(editor.timeline, Qt.LeftButton, pos=QPoint(max(30, editor.timeline.width() - 15), y))
    assert editor._preview_position_ms > 8_000
    editor._subtitle_row_clicked(0, 2)
    assert editor._preview_position_ms == 0
    assert editor.table.currentRow() == 0


def test_vertical_editor_shows_actual_preview_render_and_font_technical_info(tmp_path):
    qt_app = app()
    editor = SubtitleEditor()
    source = SourceInfo("source.mp4", "source.mp4", 10, 1, 120, 3840, 2160, 59.94, "h264", "aac", 2, 48000)
    proxy = SourceInfo(str(tmp_path / "analysis_proxy.mp4"), "analysis_proxy.mp4", 10, 1, 120, 1280, 720, 59.94, "h264", "aac", 2, 48000)
    editor.set_technical_context(
        source_info=source,
        proxy_info=proxy,
        proxy_path=tmp_path / "analysis_proxy.mp4",
        render_encoder="H.264 NVENC",
    )
    text = editor.preview_technical.text()
    tooltip = editor.preview_technical.toolTip()
    composition = editor.preview.composition_size
    assert f"{composition[0]}×{composition[1]}" in text
    assert "59,94 FPS" in text
    assert "Proxy" in text
    assert "1080×1920" in text
    assert "H.264 NVENC" in text
    assert "analysis_proxy.mp4" in tooltip
    assert "ASS FontName=Segoe UI" in tooltip
    assert "segoeuib.ttf" in tooltip.casefold()
    assert "fallback=False" in tooltip
    assert qt_app is QApplication.instance()


def test_full_hd_quality_does_not_resize_fit_viewport_or_splitter():
    qt_app = app()
    editor = SubtitleEditor()
    editor.resize(1200, 900)
    editor.show()
    qt_app.processEvents()
    editor.preview_zoom.blockSignals(True)
    editor.preview_zoom.setCurrentIndex(editor.preview_zoom.findData("fit"))
    editor.preview_zoom.blockSignals(False)
    editor._apply_preview_zoom()
    qt_app.processEvents()
    before_widget = editor.preview.size()
    before_splitter = editor.top_splitter.sizes()
    editor.preview_quality.blockSignals(True)
    editor.preview_quality.setCurrentIndex(editor.preview_quality.count() - 1)
    editor.preview_quality.blockSignals(False)
    editor._apply_preview_quality()
    qt_app.processEvents()
    assert editor.preview.composition_size == (1080, 1920)
    assert editor.preview.size() == before_widget
    assert editor.top_splitter.sizes() == before_splitter
    editor.preview_zoom.blockSignals(True)
    editor.preview_zoom.setCurrentIndex(editor.preview_zoom.findData(100))
    editor.preview_zoom.blockSignals(False)
    editor._apply_preview_zoom()
    qt_app.processEvents()
    assert editor.preview.size().width() == 1080
    assert max(
        editor.preview_scroll.horizontalScrollBar().maximum(),
        editor.preview_scroll.verticalScrollBar().maximum(),
    ) > 0
    assert editor.top_splitter.sizes() == before_splitter


def test_editor_persists_separate_title_and_subtitle_alignment(tmp_path):
    qt_app = app()
    candidate = Candidate("short_001", 10, 20, 90, "one")
    editor = SubtitleEditor()
    paths = SimpleNamespace(subtitles=tmp_path)
    editor.set_context(candidate, transcript(), paths)
    editor.title_alignment.setCurrentIndex(editor.title_alignment.findData("center"))
    editor.subtitle_alignment.setCurrentIndex(editor.subtitle_alignment.findData("left"))
    editor.subtitle_offset_x.setValue(120)
    editor._apply_configuration()
    assert candidate.branding_settings["title_alignment"] == "center"
    assert candidate.subtitle_settings["alignment"] == "left"
    assert candidate.subtitle_settings["horizontal_offset"] >= 0
    editor.set_context(candidate, transcript(), paths)
    assert editor.title_alignment.currentData() == "center"
    assert editor.subtitle_alignment.currentData() == "left"
    assert qt_app is QApplication.instance()


def test_title_presets_are_distinct_and_legacy_banner_x_is_centered(tmp_path):
    qt_app = app()
    paths = SimpleNamespace(subtitles=tmp_path)
    candidate = Candidate(
        "short_001", 10, 20, 90, "one",
        branding_settings={"original_video_title": "Good title", "banner_x": 50},
    )
    editor = SubtitleEditor()
    editor.set_context(candidate, transcript(), paths)
    assert editor.banner_x.value() == 0
    values = []
    for name in ("clean", "large", "gaming"):
        editor.title_style.setCurrentIndex(editor.title_style.findData(name))
        values.append((editor.title_size.value(), editor.title_outline.value(), editor.title_shadow.value()))
    assert len(set(values)) == 3


def test_title_buttons_use_cached_values_without_ollama_calls(tmp_path, monkeypatch):
    qt_app = app()
    paths = SimpleNamespace(subtitles=tmp_path)
    candidate = Candidate(
        "short_001", 10, 20, 90, "one",
        branding_settings={
            "original_video_title": "I Mined 48,235 Obsidian - Hardcore",
            "translated_video_title": "Я добыл 48 235 обсидиана — хардкор",
            "title_suggestions": {"suggestions": []},
        },
    )
    editor = SubtitleEditor(semantic_backend=object())
    calls = []
    monkeypatch.setattr(editor, "_run_ai_title_task", lambda *_args, **_kwargs: calls.append("ollama"))
    monkeypatch.setattr("creator_assistant.ui.shorts.subtitle_editor.QMessageBox.information", lambda *_args, **_kwargs: None)
    editor.set_context(candidate, transcript(), paths)
    editor._title_translate_clicked()
    assert editor.title_text.text().startswith("Я добыл")
    editor._title_hook_clicked()
    assert calls == []
    assert qt_app is QApplication.instance()


def test_boundary_change_marks_title_suggestions_stale(tmp_path):
    from creator_assistant.services.shorts.title_assets import ShortTitleAssetService

    candidate = Candidate(
        "short_001", 10, 20, 90, "one",
        branding_settings={
            "title_suggestions": {
                "status": "ready",
                "suggestions": [{"id": "hook_1", "text": "ГОТОВЫЙ ХУК", "score": 90, "reason": "ok"}],
            }
        },
    )
    candidate.start, candidate.end = 5, 15
    ShortTitleAssetService().mark_stale(candidate, transcript())
    assert candidate.branding_settings["title_suggestions"]["status"] == "stale"


def test_stale_exact_preview_generation_cannot_replace_current_frame():
    qt_app = app()
    editor = SubtitleEditor()
    old_generation = editor._preview_generation_id
    editor._preview_generation_id += 1
    image = QImage(32, 32, QImage.Format_RGB32)
    image.fill(QColor("green"))
    assert not editor.apply_exact_preview_frame(image, old_generation)
    assert editor.apply_exact_preview_frame(image, editor._preview_generation_id)


def test_quality_modes_are_real_backing_resolutions_and_zoom_is_independent():
    qt_app = app()
    editor = SubtitleEditor()
    viewport_size = editor.preview_scroll.size()
    for index, expected in enumerate(((360, 640), (540, 960), (720, 1280), (1080, 1920))):
        editor.preview_quality.setCurrentIndex(index)
        assert editor.preview.composition_size == expected
    editor.preview_zoom.setCurrentIndex(editor.preview_zoom.findData("fit"))
    assert editor.preview_scroll.size() == viewport_size
    editor.preview_zoom.setCurrentIndex(editor.preview_zoom.findData(200))
    assert editor.preview.size() == QSize(2160, 3840)
    assert editor.preview.composition_size == (1080, 1920)
    assert qt_app is QApplication.instance()


def test_realtime_blur_uses_reduced_background_branch():
    source = Path(__file__).parents[2] / "src" / "creator_assistant" / "ui" / "shorts" / "subtitle_editor.py"
    text = source.read_text(encoding="utf-8")
    assert "background.scaled(45, 80" in text
    assert "background.scaled(270, 480" in text
