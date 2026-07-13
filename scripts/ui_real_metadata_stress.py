from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from creator_assistant.infrastructure.crash_logging import initialize_early

initialize_early()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from creator_assistant.app import ServiceContainer
from creator_assistant.infrastructure.crash_logging import install_qt_message_handler
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.ui.project_prep_tab import ProjectPrepTab, UiJobState


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    project = args.project.resolve()
    if not project.is_dir():
        raise SystemExit(f"Test project does not exist: {project}")
    install_qt_message_handler()
    app = QApplication(sys.argv[:1])
    container = ServiceContainer()
    container.settings["open_folder_after_completion"] = False
    test_store = JobStore(project.parent / "ui_real_metadata_state")
    test_store.save("5nTuu0FzAUg", {
        "created_by": "CreatorAssistant",
        "video_id": "5nTuu0FzAUg",
        "status": "completed",
        "project_path": str(project),
        "project_paths": [str(project)],
    })
    container.projects.job_store = test_store
    tab = ProjectPrepTab(container)
    tab.author_combo.blockSignals(True)
    tab.author_combo.clear()
    tab.author_combo.addItem(f"Test — {project.parent}", str(project.parent))
    tab.author_combo.setCurrentIndex(0)
    tab.author_combo.blockSignals(False)
    for option in (tab.max_check, tab.proxy_check, tab.audio_check, tab.instrumental_check, tab.reaper_check):
        option.setChecked(False)
    tab.show()
    # Completion notifications would otherwise require a second manual click.
    QMessageBox.information = staticmethod(lambda *_args, **_kwargs: QMessageBox.Ok)
    state = {
        "cycle": 0,
        "phase": "start",
        "started": time.monotonic(),
        "results": [],
        "error": "",
    }
    url = "https://youtu.be/5nTuu0FzAUg"

    def start_cycle():
        state["cycle"] += 1
        state["phase"] = "metadata"
        tab.reset_for_new_project(url)
        tab.request_metadata("stress")

    def click_dialogs():
        for widget in QApplication.topLevelWidgets():
            if not isinstance(widget, QMessageBox) or not widget.isVisible():
                continue
            buttons = widget.buttons()
            accepted = next(
                (button for button in buttons if widget.buttonRole(button) == QMessageBox.AcceptRole),
                None,
            )
            if accepted:
                accepted.click()
            else:
                widget.accept()

    def tick():
        click_dialogs()
        if time.monotonic() - state["started"] > args.timeout:
            state["error"] = f"timeout phase={state['phase']} cycle={state['cycle']}"
            app.quit()
            return
        if state["phase"] == "metadata":
            if tab.project_selection == "resume" and tab.create_button.isEnabled() and not tab._threads:
                state["phase"] = "project"
                # The stability check must not download even a thumbnail.
                tab.metadata.thumbnails = []
                tab.create_button.click()
        elif state["phase"] == "project":
            if tab.job_state == UiJobState.COMPLETED and not tab._threads:
                state["results"].append({
                    "cycle": state["cycle"],
                    "threads": len(tab._threads),
                    "metadata_thread": tab.metadata_thread is not None,
                    "project_thread": tab.active_thread is not None,
                    "state": tab.job_state.value,
                })
                if state["cycle"] >= args.cycles:
                    app.quit()
                else:
                    start_cycle()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(20)
    QTimer.singleShot(0, start_cycle)
    exit_code = app.exec()
    timer.stop()
    tab.shutdown_workers()
    summary = {
        "requested_cycles": args.cycles,
        "completed_cycles": len(state["results"]),
        "cycles": state["results"],
        "error": state["error"],
        "event_loop_exit_code": exit_code,
        "remaining_threads": len(tab._threads),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not state["error"] and len(state["results"]) == args.cycles and not tab._threads else 1


if __name__ == "__main__":
    raise SystemExit(main())
