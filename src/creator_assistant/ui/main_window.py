from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget, QToolBar, QWidget

from creator_assistant.app import ServiceContainer
from creator_assistant.ui.diagnostics_dialog import DiagnosticsDialog
from creator_assistant.ui.project_prep_tab import ProjectPrepTab
from creator_assistant.ui.settings_dialog import SettingsDialog
from creator_assistant.infrastructure.crash_logging import event as crash_event, safe_call


class MainWindow(QMainWindow):
    def __init__(self, container: ServiceContainer) -> None:
        super().__init__()
        self.container = container
        self.setWindowTitle("Creator Assistant")
        self.resize(1320, 860)
        self.setMinimumSize(1040, 700)
        toolbar = QToolBar("Основное")
        toolbar.setMovable(False)
        toolbar.addWidget(QLabel("  CREATOR ASSISTANT  "))
        toolbar.addSeparator()
        diagnostics = QAction("Диагностика", self)
        settings = QAction("Настройки", self)
        diagnostics.triggered.connect(safe_call("open_diagnostics", self.open_diagnostics))
        settings.triggered.connect(safe_call("open_settings_main", self.open_settings))
        toolbar.addAction(diagnostics)
        toolbar.addAction(settings)
        self.addToolBar(toolbar)
        self.tabs = QTabWidget()
        self.prep_tab = ProjectPrepTab(container)
        self.container.settings_service.settings_changed.connect(
            lambda settings: self.statusBar().showMessage(
                f"Настройки применены: прокси {settings.get('reaper_proxy_height', 720)}p",
                5000,
            )
        )
        self.tabs.addTab(self.prep_tab, "Подготовка проекта")
        placeholder = QWidget()
        shorts_index = self.tabs.addTab(placeholder, "Shorts — будет добавлено позже")
        self.tabs.setTabEnabled(shorts_index, False)
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage("Готов к работе")
        if self.container.first_run:
            QTimer.singleShot(500, self.open_diagnostics)

    def open_diagnostics(self) -> None:
        DiagnosticsDialog(self.container, self).exec()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.container, self)
        if dialog.exec():
            self.prep_tab.reload_authors()

    def closeEvent(self, event) -> None:
        if self.prep_tab.shutdown_workers():
            crash_event("Main window closed after all QThreads finished")
            event.accept()
        else:
            crash_event("Main window close deferred because a QThread is still running")
            self.statusBar().showMessage("Завершаю активную операцию…")
            event.ignore()
