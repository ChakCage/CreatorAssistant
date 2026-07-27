from __future__ import annotations

import datetime as dt
import subprocess
import urllib.parse
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QVBoxLayout,
)

from creator_assistant.infrastructure.build_info import current_build_info
from creator_assistant.infrastructure.release_updates import (
    ReleaseManifest, ReleaseUpdateService, UpdateError, UpdatePolicy,
)


class UpdatesDialog(QDialog):
    def __init__(self, container, parent=None) -> None:
        super().__init__(parent)
        self.container = container
        self.manifest: ReleaseManifest | None = None
        self.installer: Path | None = None
        build = current_build_info()
        self.setWindowTitle("Обновления Creator Assistant")
        self.resize(680, 460)
        layout = QVBoxLayout(self)
        self.current = QLabel(
            f"Текущая версия: {build.version} (сборка {build.build_number})\n"
            f"Канал: {build.channel} · редакция: {build.edition} · {build.architecture}\n"
            f"Последняя проверка: {container.settings.get('app_updates', {}).get('last_check') or 'ещё не выполнялась'}"
        )
        self.status = QLabel("Нажмите «Проверить обновления».")
        self.status.setWordWrap(True)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.current)
        layout.addWidget(self.status)
        layout.addWidget(self.notes, 1)
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        self.check_button = QPushButton("Проверить обновления")
        self.download_button = QPushButton("Скачать")
        self.install_button = QPushButton("Установить и перезапустить")
        self.later_button = QPushButton("Напомнить позже")
        self.download_button.setEnabled(False)
        self.install_button.setEnabled(False)
        for button in (self.check_button, self.download_button, self.install_button, self.later_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.check_button.clicked.connect(self.check)
        self.download_button.clicked.connect(self.download)
        self.install_button.clicked.connect(self.install)
        self.later_button.clicked.connect(self.reject)
        endpoint_host = urllib.parse.urlparse(build.update_backend_url).hostname or ""
        self.service = ReleaseUpdateService(
            build.update_backend_url,
            UpdatePolicy(
                edition=build.edition, channel=build.channel, architecture=build.architecture,
                current_version=build.version, current_build=build.build_number,
                public_keys=build.update_public_keys,
                allowed_hosts=(endpoint_host,) if endpoint_host else (),
                allow_local_http=build.build_variant == "staging",
            ),
            busy_check=getattr(container, "update_busy_check", None),
        ) if build.update_backend_url else None

    def check(self) -> None:
        if not self.service:
            QMessageBox.information(self, "Обновления", "Для этой локальной/непубличной сборки сервер обновлений не настроен.")
            return
        self.setEnabled(False)
        try:
            self.manifest = self.service.latest()
            now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            self.container.settings.setdefault("app_updates", {})["last_check"] = now
            self.container.settings_store.save(self.container.settings)
            if not self.manifest:
                self.status.setText("Установлена актуальная версия.")
                return
            value = self.manifest
            self.status.setText(
                f"Доступна версия {value.version}, {value.file_size / 1024**2:.1f} МБ. "
                + ("Обновление обязательное." if value.mandatory else "Обычное обновление.")
            )
            self.notes.setPlainText(value.release_notes)
            self.download_button.setEnabled(True)
        except UpdateError as exc:
            self._show_error(exc)
        finally:
            self.setEnabled(True)

    def download(self) -> None:
        if not self.service or not self.manifest:
            return
        self.progress.show()
        self.progress.setRange(0, max(1, self.manifest.file_size))
        try:
            def update_progress(current: int, total: int) -> None:
                self.progress.setMaximum(max(1, total))
                self.progress.setValue(current)
                QApplication.processEvents()
            self.installer = self.service.download(self.manifest, progress=update_progress)
            self.status.setText(f"Установщик проверен: {self.installer}")
            self.install_button.setEnabled(True)
        except UpdateError as exc:
            self._show_error(exc)
        finally:
            self.progress.hide()

    def install(self) -> None:
        if not self.installer or not self.installer.is_file():
            return
        try:
            subprocess.Popen([str(self.installer), "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"])
        except OSError:
            self._show_error(UpdateError("INSTALLER_START_FAILED", "Не удалось запустить проверенный установщик."))
            return
        QApplication.quit()

    def _show_error(self, exc: UpdateError) -> None:
        suffix = f"\n\nDiagnostic request ID: {exc.request_id}" if exc.request_id else ""
        QMessageBox.warning(self, "Обновление не выполнено", f"{exc}\nКод: {exc.code}{suffix}\n\nТекущая версия не изменена.")
