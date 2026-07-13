"""Invoke the two completed-project UI actions against an explicitly supplied project."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from creator_assistant.app import ServiceContainer
from creator_assistant.ui.project_prep_tab import ProjectPrepTab, UiJobState


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: verify_real_open_actions.py PROJECT RPP")
    project, rpp = map(Path, sys.argv[1:])
    if not project.is_dir() or not rpp.is_file() or rpp.parent.resolve() != project.resolve():
        raise SystemExit("The exact project/RPP pair is not valid")
    app = QApplication.instance() or QApplication([])
    tab = ProjectPrepTab(ServiceContainer())
    tab.current_project_path = project
    tab.current_rpp_path = rpp
    tab._set_job_state(UiJobState.COMPLETED)
    tab._sync_project_actions()
    if not tab.open_folder_button.isEnabled() or not tab.open_rpp_button.isEnabled():
        raise RuntimeError("Completed-project actions are unexpectedly disabled")
    tab.open_current_project_folder()
    tab.open_current_rpp()
    print(f"folder={project}")
    print(f"rpp={rpp}")
    tab.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
