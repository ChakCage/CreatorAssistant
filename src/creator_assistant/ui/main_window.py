from __future__ import annotations

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QTabWidget, QToolBar

from creator_assistant.app import ServiceContainer
from creator_assistant.ui.diagnostics_dialog import DiagnosticsDialog
from creator_assistant.ui.project_prep_tab import ProjectPrepTab
from creator_assistant.ui.shorts.shorts_tab import ShortsTab
from creator_assistant.ui.autopilot_tab import AutopilotTab
from creator_assistant.ui.settings_dialog import SettingsDialog
from creator_assistant.infrastructure.crash_logging import event as crash_event, safe_call
from creator_assistant.infrastructure.build_info import current_build_info


class MainWindow(QMainWindow):
    def __init__(self, container: ServiceContainer) -> None:
        super().__init__()
        self.container = container
        build = current_build_info()
        self.setWindowTitle(f"Creator Assistant · {build.commit}")
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
        self.shorts_tab = ShortsTab(container)
        self.tabs.addTab(self.shorts_tab, "Shorts")
        self.autopilot_tab = AutopilotTab(container)
        self.tabs.addTab(self.autopilot_tab, "Автопилот")
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage(f"Готов к работе · {build.executable}")
        self.statusBar().setToolTip(f"EXE: {build.executable}\nCommit: {build.commit}\nСборка: {build.build_date}")
        self._restore_or_size_window()
        if self.container.first_run:
            QTimer.singleShot(500, self.open_diagnostics)

    def open_diagnostics(self) -> None:
        DiagnosticsDialog(self.container, self).exec()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.container, self)
        if dialog.exec():
            self.prep_tab.reload_authors()

    def closeEvent(self, event) -> None:
        if self.prep_tab.shutdown_workers() and self.shorts_tab.shutdown_workers() and self.autopilot_tab.shutdown_workers():
            self.container.settings["window_geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self.container.settings_store.save(self.container.settings)
            crash_event("Main window closed after all QThreads finished")
            event.accept()
        else:
            crash_event("Main window close deferred because a QThread is still running")
            self.statusBar().showMessage("Завершаю активную операцию…")
            event.ignore()

    def _restore_or_size_window(self) -> None:
        encoded = str(self.container.settings.get("window_geometry") or "")
        if encoded:
            try:
                if self.restoreGeometry(QByteArray.fromBase64(encoded.encode("ascii"))):
                    if any(self.frameGeometry().intersects(screen.availableGeometry()) for screen in QApplication.screens()):
                        return
            except (ValueError, TypeError):
                pass
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else self.geometry()
        width = max(self.minimumWidth(), int(available.width() * 0.88))
        height = max(self.minimumHeight(), int(available.height() * 0.88))
        width, height = min(width, available.width()), min(height, available.height())
        self.resize(width, height)
        self.move(available.x() + (available.width() - width) // 2, available.y() + (available.height() - height) // 2)
