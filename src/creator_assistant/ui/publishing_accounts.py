from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from creator_assistant.ui.workers import FunctionWorker


class TikTokClientDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent); self.setWindowTitle("TikTok Developer App")
        layout = QFormLayout(self)
        self.client_key = QLineEdit(); self.client_secret = QLineEdit(); self.client_secret.setEchoMode(QLineEdit.Password)
        self.redirect_uri = QLineEdit("http://127.0.0.1:8765/")
        layout.addRow("Client key", self.client_key); layout.addRow("Client secret", self.client_secret); layout.addRow("Redirect URI", self.redirect_uri)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel); buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addRow(buttons)


class PublishingAccountsPanel(QGroupBox):
    changed = Signal()

    def __init__(self, container, parent=None) -> None:
        super().__init__("Публикация и аккаунты", parent); self.container = container; self._threads = []
        root = QVBoxLayout(self)
        warning = QLabel("OAuth-токены и client secrets хранятся только в Windows Credential Manager. По умолчанию публикация работает в режиме DRY_RUN.")
        warning.setWordWrap(True); warning.setProperty("class", "muted"); root.addWidget(warning)
        status_row = QHBoxLayout()
        self.youtube_status = QLabel(); self.youtube_status.setWordWrap(True)
        self.tiktok_status = QLabel(); self.tiktok_status.setWordWrap(True)
        status_row.addWidget(self.youtube_status, 1); status_row.addWidget(self.tiktok_status, 1)
        root.addLayout(status_row)
        actions = QHBoxLayout()
        for text, callback in (("Импортировать OAuth-конфигурацию Google", self.import_google), ("Подключить канал YouTube", self.connect_google), ("Настроить TikTok App", self.import_tiktok), ("Подключить TikTok", self.connect_tiktok)):
            button = QPushButton(text); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch(1); root.addLayout(actions)
        self.table = QTableWidget(0, 7); self.table.setHorizontalHeaderLabels(("Платформа", "Аккаунт", "ID", "Статус", "Доступ", "Scopes", "Истекает")); self.table.setMaximumHeight(190)
        root.addWidget(self.table)
        account_actions = QHBoxLayout()
        for text, callback in (("Проверить", self.validate_selected), ("Переподключить", self.reconnect_selected), ("Отключить", self.disconnect_selected)):
            button = QPushButton(text); button.clicked.connect(callback); account_actions.addWidget(button)
        account_actions.addStretch(1); root.addLayout(account_actions); self.refresh()

    def selected_account(self):
        item = self.table.item(self.table.currentRow(), 0)
        account_id = str(item.data(Qt.UserRole) or "") if item else ""
        return next((item for item in self.container.publishing_store.accounts() if item.account_id == account_id), None)

    def refresh(self) -> None:
        accounts = self.container.publishing_store.accounts(); self.table.setRowCount(len(accounts))
        youtube = [item for item in accounts if item.platform == "youtube"]
        tiktok = [item for item in accounts if item.platform == "tiktok"]
        self.youtube_status.setText("YouTube\n" + ("\n".join(f"{item.display_name} · {item.status} · {item.capability}" for item in youtube) if youtube else "Статус: Не подключён"))
        self.tiktok_status.setText("TikTok\n" + ("\n".join(f"{item.display_name} · {item.status} · {item.capability}" for item in tiktok) if tiktok else "Статус: Не подключён"))
        for row, account in enumerate(accounts):
            values = (account.platform.title(), account.display_name, account.remote_user_id, account.status, account.capability, ", ".join(account.granted_scopes), account.token_expires_at or "—")
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, account.account_id); self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents(); self.changed.emit()

    def import_google(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Google OAuth client JSON", "", "JSON (*.json)")
        if not path: return
        try:
            config_id = self.container.publishing_oauth.import_google_client(path)
            self.container.settings.setdefault("publishing", {})["google_client_config_id"] = config_id; self.container.settings_store.save(self.container.settings)
            QMessageBox.information(self, "Google OAuth", "Конфигурация импортирована в Windows Credential Manager.")
        except Exception as exc: QMessageBox.critical(self, "Google OAuth", str(exc))

    def import_tiktok(self) -> None:
        dialog = TikTokClientDialog(self)
        if not dialog.exec(): return
        try:
            config_id = self.container.publishing_oauth.import_tiktok_client(dialog.client_key.text(), dialog.client_secret.text(), dialog.redirect_uri.text())
            self.container.settings.setdefault("publishing", {})["tiktok_client_config_id"] = config_id; self.container.settings_store.save(self.container.settings)
            QMessageBox.information(self, "TikTok OAuth", "Конфигурация сохранена в Windows Credential Manager.")
        except Exception as exc: QMessageBox.critical(self, "TikTok OAuth", str(exc))

    def connect_google(self) -> None:
        config_id = str(self.container.settings.get("publishing", {}).get("google_client_config_id") or "")
        if not config_id: QMessageBox.information(self, "YouTube", "Сначала импортируйте Google OAuth client JSON типа Desktop app."); return
        self._run(lambda: self.container.publishing_accounts.connect_google(config_id), "Подключаю YouTube…")

    def connect_tiktok(self) -> None:
        config_id = str(self.container.settings.get("publishing", {}).get("tiktok_client_config_id") or "")
        if not config_id: QMessageBox.information(self, "TikTok", "Сначала настройте TikTok Developer App."); return
        self._run(lambda: self.container.publishing_accounts.connect_tiktok(config_id), "Подключаю TikTok…")

    def validate_selected(self) -> None:
        account = self.selected_account()
        if account: self._run(lambda: self.container.publishing_accounts.validate(account.account_id), "Проверяю аккаунт…")

    def reconnect_selected(self) -> None:
        account = self.selected_account()
        if not account: return
        config_key = "google_client_config_id" if account.platform == "youtube" else "tiktok_client_config_id"
        config_id = str(self.container.settings.get("publishing", {}).get(config_key) or "")
        operation = (lambda: self.container.publishing_accounts.connect_google(config_id, account.account_id)) if account.platform == "youtube" else (lambda: self.container.publishing_accounts.connect_tiktok(config_id, account.account_id))
        self._run(operation, "Переподключаю аккаунт…")

    def disconnect_selected(self) -> None:
        account = self.selected_account()
        if not account: return
        if QMessageBox.question(self, "Отключить аккаунт", f"Удалить OAuth-токены аккаунта {account.display_name} из Windows Credential Manager?") == QMessageBox.Yes:
            self.container.publishing_accounts.disconnect(account.account_id); self.refresh()

    def _run(self, operation, status: str) -> None:
        thread = QThread(self); worker = FunctionWorker(lambda _progress: operation()); worker.moveToThread(thread)
        thread.started.connect(worker.run); worker.finished.connect(lambda _value: self.refresh()); worker.finished.connect(thread.quit)
        worker.failed.connect(lambda message, _details: QMessageBox.critical(self, "Публикация", message)); worker.failed.connect(thread.quit)
        thread.finished.connect(lambda: self._threads.remove((thread, worker)) if (thread, worker) in self._threads else None); thread.finished.connect(thread.deleteLater)
        self._threads.append((thread, worker)); thread.start()
