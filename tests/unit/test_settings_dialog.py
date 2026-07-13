import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from creator_assistant.infrastructure.settings_store import DEFAULT_SETTINGS
from creator_assistant.ui.settings_dialog import SettingsDialog
from creator_assistant.ui.diagnostics_dialog import DiagnosticsDialog


def app():
    return QApplication.instance() or QApplication([])


def test_paths_are_visible_copyable_and_start_at_the_beginning():
    application = app()
    settings = dict(DEFAULT_SETTINGS)
    settings["yt_dlp_path"] = r"E:\YouTube\Инструменты\yt-dlp\yt-dlp.exe"
    dialog = SettingsDialog(settings)
    edit = dialog.path_edits["yt_dlp_path"]
    assert edit.text() == settings["yt_dlp_path"]
    assert edit.toolTip() == settings["yt_dlp_path"]
    assert edit.cursorPosition() == 0
    dialog.close()
    application.processEvents()


def test_scroll_content_and_fixed_button_panel_are_separate():
    application = app()
    dialog = SettingsDialog(dict(DEFAULT_SETTINGS))
    assert dialog.scroll_area.widgetResizable()
    assert not dialog.scroll_area.isAncestorOf(dialog.buttons)
    assert dialog.minimumWidth() <= dialog.width()
    assert dialog.minimumHeight() <= dialog.height()
    dialog.close()
    application.processEvents()


def test_temp_root_and_proxy_height_are_saved(tmp_path):
    application = app()
    settings = dict(DEFAULT_SETTINGS)
    dialog = SettingsDialog(settings)
    dialog.path_edits["temp_root"].setText(str(tmp_path))
    dialog.proxy_height.setCurrentIndex(dialog.proxy_height.findData(1080))
    dialog._save()
    assert dialog.result_settings["temp_root"] == str(tmp_path)
    assert dialog.result_settings["reaper_proxy_height"] == 1080
    dialog.close()
    application.processEvents()


def test_vegas_defaults_and_auto_open_setting_are_saved():
    application = app()
    dialog = SettingsDialog(dict(DEFAULT_SETTINGS))
    assert dialog.create_vegas_default.isChecked()
    assert not dialog.auto_open_vegas.isChecked()
    dialog.create_vegas_default.setChecked(False)
    dialog.auto_open_vegas.setChecked(True)
    dialog._save()
    assert dialog.result_settings["create_vegas_project_default"] is False
    assert dialog.result_settings["auto_open_vegas_project"] is True
    dialog.close()
    application.processEvents()


def test_diagnostics_dialog_uses_supported_header_resize_mode(monkeypatch):
    application = app()
    monkeypatch.setattr(DiagnosticsDialog, "run_diagnostics", lambda self: None)
    dialog = DiagnosticsDialog(object())
    assert dialog.table.horizontalHeader().stretchLastSection() is False
    dialog.close()
    application.processEvents()
