from __future__ import annotations

import importlib

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QInputDialog, QLabel, QMainWindow, QMessageBox, QTabWidget, QToolBar

from creator_assistant.app import ServiceContainer
from creator_assistant.ui.project_prep_tab import ProjectPrepTab
from creator_assistant.ui.shorts.shorts_tab import ShortsTab
from creator_assistant.ui.settings_dialog import SettingsDialog
from creator_assistant.infrastructure.crash_logging import event as crash_event, safe_call
from creator_assistant.infrastructure.build_info import current_build_info
from creator_assistant.product import Feature, FeatureRegistry


class MainWindow(QMainWindow):
    def __init__(self, container: ServiceContainer) -> None:
        super().__init__()
        self.container = container
        build = current_build_info()
        edition = container.edition
        self.setWindowTitle(
            f"{edition.application_name} · {edition.display_name} · v{build.version} · "
            f"{build.commit} · {build.build_date}"
        )
        self.setMinimumSize(1040, 700)
        toolbar = QToolBar("Основное")
        toolbar.setMovable(False)
        toolbar.addWidget(QLabel(f"  {edition.application_name.upper()}  "))
        toolbar.addSeparator()
        settings = QAction("Настройки", self)
        settings.triggered.connect(safe_call("open_settings_main", self.open_settings))
        if FeatureRegistry.is_available(Feature.DIAGNOSTICS, edition):
            diagnostics = QAction("Диагностика", self)
            diagnostics.triggered.connect(safe_call("open_diagnostics", self.open_diagnostics))
            toolbar.addAction(diagnostics)
        toolbar.addAction(settings)
        if FeatureRegistry.is_available(Feature.LICENSING, edition):
            license_action = QAction("Лицензия", self)
            license_action.triggered.connect(safe_call("open_license", self.open_license))
            toolbar.addAction(license_action)
        help_action = QAction("Справка", self)
        updates_action = QAction("Обновления", self)
        about_action = QAction("О программе", self)
        help_action.triggered.connect(self.open_help)
        updates_action.triggered.connect(self.check_updates)
        about_action.triggered.connect(self.open_about)
        toolbar.addAction(help_action); toolbar.addAction(updates_action); toolbar.addAction(about_action)
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
        if FeatureRegistry.is_available(Feature.AUTOPILOT, edition):
            module = importlib.import_module("creator_assistant.ui." + "autopilot_tab")
            self.autopilot_tab = module.AutopilotTab(container)
            self.tabs.addTab(self.autopilot_tab, "Автопилот")
        if FeatureRegistry.is_available(Feature.PUBLISHING_QUEUE, edition):
            module = importlib.import_module("creator_assistant.ui." + "publishing_queue")
            self.publishing_queue_tab = module.PublishingQueueTab(container)
            self.tabs.addTab(self.publishing_queue_tab, "Очередь публикаций")
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage(f"Готов к работе · {build.executable}")
        self.statusBar().setToolTip(f"EXE: {build.executable}\nCommit: {build.commit}\nСборка: {build.build_date}")
        self._restore_or_size_window()
        if self.container.first_run:
            QTimer.singleShot(500, self.open_diagnostics)

    def open_diagnostics(self) -> None:
        module = importlib.import_module("creator_assistant.ui." + "diagnostics_dialog")
        module.DiagnosticsDialog(self.container, self).exec()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.container, self)
        if dialog.exec():
            self.prep_tab.reload_authors()
            if hasattr(self, "autopilot_tab"):
                self.autopilot_tab._refresh_platform_status()
            if hasattr(self, "publishing_queue_tab"):
                self.publishing_queue_tab.refresh()

    def open_about(self) -> None:
        build = current_build_info()
        QMessageBox.about(
            self, "О программе",
            f"{self.container.edition.application_name}\n{self.container.edition.display_name}\n"
            f"Версия: {build.version}\nCommit: {build.commit}\nДата сборки: {build.build_date}",
        )

    def open_help(self) -> None:
        QMessageBox.information(self, "Справка", "Подготовка проекта и Shorts используют общий формат проектов в обеих редакциях.")

    def check_updates(self) -> None:
        build = current_build_info()
        QMessageBox.information(self, "Обновления", f"Текущая версия: {build.version}\nРедакция: {self.container.edition.display_name}")

    def open_license(self) -> None:
        status = self.container.licensing.status()
        prompt = "Лицензия активна." if status.active else "Введите лицензионный ключ:"
        key, accepted = QInputDialog.getText(self, "Лицензия", prompt)
        if not accepted or (status.active and not key):
            return
        try:
            updated = self.container.licensing.activate(key)
        except ValueError as exc:
            QMessageBox.warning(self, "Лицензия", str(exc)); return
        QMessageBox.information(self, "Лицензия", f"Лицензия активирована · {updated.fingerprint}")

    def closeEvent(self, event) -> None:
        workers = [self.prep_tab.shutdown_workers(), self.shorts_tab.shutdown_workers()]
        if hasattr(self, "autopilot_tab"):
            workers.append(self.autopilot_tab.shutdown_workers())
        if hasattr(self, "publishing_queue_tab"):
            workers.append(self.publishing_queue_tab.shutdown_workers())
        if all(workers):
            self.container.settings["window_geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self.container.settings_store.save(self.container.settings)
            if hasattr(self.container, "publishing_agent"):
                self.container.publishing_agent.stop()
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
