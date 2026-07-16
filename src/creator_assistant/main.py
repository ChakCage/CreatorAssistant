from __future__ import annotations

import multiprocessing
import sys
import json
import time
from copy import deepcopy
from pathlib import Path

from creator_assistant.infrastructure.crash_logging import initialize_early

initialize_early()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from creator_assistant.app import ServiceContainer
from creator_assistant.ui.main_window import MainWindow
from creator_assistant.ui.settings_dialog import SettingsDialog
from creator_assistant.domain.stages import JobStage, ORDERED_STAGES
from creator_assistant.infrastructure.crash_logging import exception as log_exception, install_qt_message_handler


STYLE = """
QWidget { background: #11151b; color: #e8edf3; font-family: "Segoe UI"; font-size: 10pt; }
QMainWindow, QDialog { background: #0d1117; }
QToolBar { background: #161c24; border: 0; border-bottom: 1px solid #2c3440; padding: 7px; spacing: 8px; }
QToolBar QLabel { color: #50d890; font-weight: 800; letter-spacing: 1px; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #151b23; color: #aeb7c4; padding: 11px 20px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #63e6a5; border-bottom-color: #42d88b; }
QTabBar::tab:disabled { color: #56606d; }
QFrame#panel, QGroupBox { background: #151b23; border: 1px solid #29313d; border-radius: 9px; }
QGroupBox { margin-top: 12px; padding: 14px 9px 9px 9px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #cbd5e1; }
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QListWidget, QTableWidget { background: #0e1319; border: 1px solid #313b49; border-radius: 6px; padding: 7px; selection-background-color: #2b7652; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: #42d88b; }
QPushButton { background: #252e3a; border: 1px solid #384454; border-radius: 6px; padding: 7px 13px; }
QPushButton:hover { background: #303c4b; border-color: #536276; }
QPushButton:pressed { background: #1d252f; }
QPushButton:disabled { color: #637080; background: #1a2028; }
QPushButton#primaryButton { background: #1b8c59; color: white; border-color: #38c982; font-weight: 700; padding: 10px; }
QPushButton#primaryButton:hover { background: #21a76a; }
QLabel#pageTitle { font-size: 21pt; font-weight: 750; color: #f4f7fb; }
QLabel#sectionTitle { font-size: 12pt; font-weight: 700; color: #dce4ee; }
QLabel#thumbnail { background: #090d12; border: 1px dashed #354151; border-radius: 7px; color: #738093; }
QFrame#infoCard { background: #10161d; border: 1px solid #27313d; border-radius: 7px; }
QLabel#infoValue { color: #63e6a5; font-size: 13pt; font-weight: 700; }
QLabel[class="muted"] { color: #8995a5; }
QLabel[class="errorText"] { color: #ff9197; font-size: 12pt; }
QProgressBar { border: 1px solid #303a47; border-radius: 6px; background: #0d1218; height: 17px; text-align: center; }
QProgressBar::chunk { background: #35c981; border-radius: 5px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QHeaderView::section { background: #202833; color: #bfc9d6; border: 0; padding: 7px; }
QScrollBar:vertical { background: #11161d; width: 11px; }
QScrollBar::handle:vertical { background: #344152; border-radius: 5px; min-height: 25px; }
"""


def _proxy_ui_verification(window: MainWindow, container: ServiceContainer, report_path: Path) -> None:
    """Exercise live Save/Cancel behavior inside the packaged application."""
    tab = window.prep_tab
    original = deepcopy(container.settings)
    events = []
    signal_snapshots = []
    container.settings_service.settings_changed.connect(signal_snapshots.append)

    def snapshot(action: str, expected_height: int, signal_count_before: int) -> None:
        stage = tab.stage_list.item(ORDERED_STAGES.index(JobStage.CREATE_PROXY)).text()
        events.append({
            "action": action,
            "expected_height": expected_height,
            "setting": int(container.settings.get("reaper_proxy_height", 720)),
            "checkbox": tab.proxy_check.text(),
            "card": tab.info_captions["proxy_size"].text(),
            "stage": stage,
            "project_threads": len(tab._threads),
            "signals_emitted": len(signal_snapshots) - signal_count_before,
        })

    def save(height: int) -> None:
        dialog = SettingsDialog(container, window)
        dialog.proxy_height.setCurrentIndex(dialog.proxy_height.findData(height))
        signal_count_before = len(signal_snapshots)
        started = time.monotonic()
        dialog._save()
        tab.reload_authors()
        snapshot(f"save_{height}", height, signal_count_before)
        events[-1]["ui_latency_ms"] = container.settings_service.last_timing.get("ui_latency_ms")
        events[-1]["save_return_ms"] = (time.monotonic() - started) * 1000.0

    for height in (480, 720, 1080):
        save(height)

    cancelled = SettingsDialog(container, window)
    cancelled.proxy_height.setCurrentIndex(cancelled.proxy_height.findData(480))
    signal_count_before_cancel = len(signal_snapshots)
    cancelled.reject()
    snapshot("cancel_480", 1080, signal_count_before_cancel)

    for height in (480, 720, 1080, 480, 720, 1080, 480):
        save(height)

    saved_events = [event for event in events if event["action"].startswith("save_")]
    cancel_events = [event for event in events if event["action"].startswith("cancel_")]
    all_saved_under_200_ms = all(
        event.get("ui_latency_ms") is not None
        and float(event["ui_latency_ms"]) < 200
        and event.get("save_return_ms") is not None
        and float(event["save_return_ms"]) < 200
        for event in saved_events
    )
    report = {
        "pid": __import__("os").getpid(),
        "single_process": True,
        "events": events,
        "save_count": len(saved_events),
        "signal_count": len(signal_snapshots),
        "max_ui_latency_ms": max(float(event["ui_latency_ms"]) for event in saved_events),
        "max_save_return_ms": max(float(event["save_return_ms"]) for event in saved_events),
        "success": len(saved_events) >= 10
        and all(event["setting"] == event["expected_height"] for event in events)
        and all(event["signals_emitted"] == 1 for event in saved_events)
        and all(event["signals_emitted"] == 0 for event in cancel_events)
        and all(event["project_threads"] == 0 for event in events)
        and all(f"{event['setting']}p" in event["stage"] for event in events)
        and all_saved_under_200_ms,
        "all_saved_under_200_ms": all_saved_under_200_ms,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    container.save_settings(original)


def main() -> int:
    multiprocessing.freeze_support()
    install_qt_message_handler()
    automation_args = {"--run-job", "--resume-job", "--list-jobs", "--show-job"}
    if any(value in automation_args for value in sys.argv[1:]):
        try:
            from creator_assistant.services.automation.cli import run_automation_cli
            return run_automation_cli(sys.argv[1:], ServiceContainer())
        except Exception as exc:
            log_exception("Automation CLI failed", exc)
            print(f"Automation CLI error: {exc}", file=sys.stderr)
            return 1
    app = QApplication(sys.argv)
    app.setApplicationName("Creator Assistant")
    app.setOrganizationName("CreatorAssistant")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    try:
        container = ServiceContainer()
        if "--smoke-test" in sys.argv:
            container.first_run = False
        window = MainWindow(container)
        window.show()
        verification_arg = next(
            (value.split("=", 1)[1] for value in sys.argv if value.startswith("--verify-proxy-ui-log=")),
            "",
        )
        if verification_arg:
            container.first_run = False
            QTimer.singleShot(
                250,
                lambda: (
                    _proxy_ui_verification(window, container, Path(verification_arg)),
                    app.quit(),
                ),
            )
        if "--smoke-test" in sys.argv:
            QTimer.singleShot(250, app.quit)
        return app.exec()
    except Exception as exc:
        log_exception("Application startup failed", exc)
        QMessageBox.critical(None, "Creator Assistant", f"Приложение не удалось запустить:\n{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
