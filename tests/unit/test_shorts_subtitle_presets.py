from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from creator_assistant.domain.shorts.models import Candidate, Transcript, TranscriptSegment
from creator_assistant.infrastructure.settings_store import SettingsStore
from creator_assistant.services.shorts.subtitle_layout import (
    SUBTITLE_PRESET_FIELDS,
    default_subtitle_preset,
)
from creator_assistant.ui.shorts.shorts_tab import ShortsTab
from creator_assistant.ui.shorts.subtitle_editor import SubtitleEditor


def app():
    return QApplication.instance() or QApplication([])


def transcript():
    text = "Проверка пользовательского пресета"
    return Transcript("ru", 20, text, [TranscriptSegment(0, 0, 20, text)])


def paths(tmp_path: Path):
    return SimpleNamespace(subtitles=tmp_path, cache=tmp_path)


def test_switching_styles_restores_explicitly_saved_values_only(tmp_path):
    qt_app = app()
    clean = {**default_subtitle_preset("clean"), "size": 66, "vertical_offset": -23}
    editor = SubtitleEditor(subtitle_presets={"clean": clean})
    candidate = Candidate("short_001", 0, 10, 1, "one", subtitle_settings=dict(clean))
    editor.set_context(candidate, transcript(), paths(tmp_path))

    editor.size.setValue(74)
    editor.offset.setValue(-41)
    assert editor._subtitle_presets["clean"]["size"] == 66
    assert editor._subtitle_presets["clean"]["vertical_offset"] == -23

    editor.style.setCurrentIndex(editor.style.findData("gaming"))
    editor.style.setCurrentIndex(editor.style.findData("clean"))
    assert editor.size.value() == 66
    assert editor.offset.value() == -23
    assert qt_app is QApplication.instance()


def test_save_button_persists_every_user_editable_preset_field(tmp_path):
    qt_app = app()
    editor = SubtitleEditor()
    candidate = Candidate("short_001", 0, 10, 1, "one", subtitle_settings=default_subtitle_preset("clean"))
    editor.set_context(candidate, transcript(), paths(tmp_path))
    editor.subtitle_font.setCurrentIndex(editor.subtitle_font.findData("Tahoma"))
    editor.size.setValue(66)
    editor.position.setCurrentIndex(editor.position.findData("center"))
    editor.subtitle_offset_x.setValue(17)
    editor.offset.setValue(-23)
    editor.subtitle_alignment.setCurrentIndex(editor.subtitle_alignment.findData("left"))
    editor.outline.setValue(6)
    editor.shadow.setValue(4)
    editor.maximum.setValue(29)
    editor.lines.setValue(1)
    editor.banner_gap.setValue(45)
    emitted = []
    editor.subtitle_preset_changed.connect(lambda name, value: emitted.append((name, value)))

    QTest.mouseClick(editor.subtitle_preset_save, Qt.LeftButton)
    assert emitted and emitted[-1][0] == "clean"
    saved = emitted[-1][1]
    assert set(SUBTITLE_PRESET_FIELDS) <= set(saved)
    assert saved["font_family"] == "Tahoma"
    assert saved["size"] == 66
    assert saved["position"] == "center"
    assert saved["horizontal_offset"] == 17
    assert saved["vertical_offset"] == -23
    assert saved["alignment"] == "left"
    assert saved["outline"] == 6 and saved["shadow"] == 4
    assert saved["maximum"] == 29 and saved["lines"] == 1
    assert saved["banner_gap"] == 45

    editor.style.setCurrentIndex(editor.style.findData("gaming"))
    editor.style.setCurrentIndex(editor.style.findData("clean"))
    assert editor.size.value() == 66 and editor.offset.value() == -23
    assert qt_app is QApplication.instance()


def test_reset_button_restores_factory_and_emits_persistent_value(tmp_path):
    qt_app = app()
    custom = {**default_subtitle_preset("large"), "size": 111, "vertical_offset": -50}
    editor = SubtitleEditor(subtitle_presets={"large": custom})
    candidate = Candidate("short_001", 0, 10, 1, "one", subtitle_settings=dict(custom))
    editor.set_context(candidate, transcript(), paths(tmp_path))
    emitted = []
    editor.subtitle_preset_changed.connect(lambda name, value: emitted.append((name, value)))
    QTest.mouseClick(editor.subtitle_preset_reset, Qt.LeftButton)
    assert emitted[-1] == ("large", default_subtitle_preset("large"))
    assert editor.size.value() == 80
    assert editor.offset.value() == 0
    assert qt_app is QApplication.instance()


def test_subtitle_presets_survive_settings_store_restart(tmp_path):
    path = tmp_path / "settings.json"
    first = SettingsStore(path)
    settings = first.load()
    settings["shorts_subtitle_presets"]["clean"].update({"size": 66, "vertical_offset": -23})
    first.save(settings)
    reloaded = SettingsStore(path).load()
    assert reloaded["shorts_subtitle_presets"]["clean"]["size"] == 66
    assert reloaded["shorts_subtitle_presets"]["clean"]["vertical_offset"] == -23
    assert set(reloaded["shorts_subtitle_presets"]) == {"clean", "large", "gaming"}


def test_autopilot_defaults_resolve_selected_custom_preset():
    clean = {**default_subtitle_preset("clean"), "size": 66, "vertical_offset": -23}
    fake_tab = SimpleNamespace(container=SimpleNamespace(settings={
        "shorts_subtitle_defaults": {"style": "clean", "layout_mode": "center_crop"},
        "shorts_subtitle_presets": {"clean": clean},
    }))
    candidate = Candidate("short_001", 0, 10, 1, "one")
    ShortsTab._apply_subtitle_defaults(fake_tab, candidate)
    assert candidate.subtitle_settings["style"] == "clean"
    assert candidate.subtitle_settings["size"] == 66
    assert candidate.subtitle_settings["vertical_offset"] == -23


def test_preset_signal_handler_updates_live_settings_and_atomic_store():
    saved = []
    updates = []
    container = SimpleNamespace(
        settings={
            "shorts_subtitle_defaults": {"style": "clean"},
            "shorts_subtitle_presets": {},
        },
        settings_store=SimpleNamespace(save=lambda value: saved.append(value)),
    )
    fake_tab = SimpleNamespace(
        container=container,
        progress_panel=SimpleNamespace(update_state=lambda *args: updates.append(args)),
    )
    custom = {**default_subtitle_preset("clean"), "size": 66, "vertical_offset": -23}
    ShortsTab._subtitle_preset_changed(fake_tab, "clean", custom)
    assert container.settings["shorts_subtitle_presets"]["clean"]["size"] == 66
    assert container.settings["shorts_subtitle_defaults"]["vertical_offset"] == -23
    assert saved and saved[-1] is container.settings
    assert updates
