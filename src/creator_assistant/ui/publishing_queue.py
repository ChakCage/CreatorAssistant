from __future__ import annotations

import os
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from creator_assistant.domain.publishing import PublishingAttemptStatus
from creator_assistant.services.publishing.manager import PublishingManager
from creator_assistant.ui.workers import FunctionWorker


class PublishingQueueTab(QWidget):
    def __init__(self, container, parent=None) -> None:
        super().__init__(parent); self.container = container; self.manager: PublishingManager = container.publishing_manager; self._thread = None; self._worker = None
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Очередь публикаций — локальные задания, прогресс загрузки и remote processing"))
        actions = QHBoxLayout()
        for text, callback in (("Загрузить сейчас", self.upload_now), ("Перепланировать", self.reschedule), ("Отменить", self.cancel), ("Повторить", self.retry), ("Открыть локальный файл", self.open_local), ("Открыть публикацию", self.open_remote)):
            button = QPushButton(text); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch(1); root.addLayout(actions)
        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels(("Дата и время", "Short", "Платформа", "Аккаунт", "Режим", "Local file", "Remote ID", "Upload", "Processing", "Статус", "Ошибка", "Attempt ID"))
        self.table.setSelectionBehavior(QTableWidget.SelectRows); root.addWidget(self.table, 1)
        self.refresh_timer = QTimer(self); self.refresh_timer.timeout.connect(self.refresh); self.refresh_timer.start(3000); self.refresh()

    def selected(self):
        item = self.table.item(self.table.currentRow(), 0); attempt_id = str(item.data(Qt.UserRole) or "") if item else ""
        return next((value for value in self.container.publishing_store.attempts() if value.attempt_id == attempt_id), None)

    def refresh(self) -> None:
        attempts = sorted(self.container.publishing_store.attempts(), key=lambda value: value.scheduled_at or value.created_at)
        self.table.setRowCount(len(attempts))
        accounts = {item.account_id: item.display_name for item in self.container.publishing_store.accounts()}
        for row, attempt in enumerate(attempts):
            values = (attempt.scheduled_at or "Сейчас", attempt.short_id, attempt.platform.title(), accounts.get(attempt.account_id, attempt.account_id or "Не подключён"), attempt.mode, attempt.local_file, attempt.remote_id or "—", f"{attempt.progress:.1f}%", attempt.processing_status or "—", attempt.status, attempt.error or "—", attempt.attempt_id)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, attempt.attempt_id); self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()

    def _run(self, operation) -> None:
        if self._thread and self._thread.isRunning(): return
        thread = QThread(self); worker = FunctionWorker(lambda _progress: operation()); worker.moveToThread(thread)
        thread.started.connect(worker.run); worker.finished.connect(lambda _value: self.refresh()); worker.finished.connect(thread.quit)
        worker.failed.connect(lambda message, _details: QMessageBox.critical(self, "Публикация", message)); worker.failed.connect(thread.quit)
        thread.finished.connect(self._finished); self._thread, self._worker = thread, worker; thread.start()

    def _finished(self) -> None:
        thread = self._thread; self._thread = None; self._worker = None
        if thread: thread.deleteLater()

    def upload_now(self) -> None:
        attempt = self.selected()
        if not attempt: return
        if attempt.mode != "DRY_RUN":
            account = next((item for item in self.container.publishing_store.accounts() if item.account_id == attempt.account_id), None)
            name = account.display_name if account else "неподключённый аккаунт"
            if QMessageBox.question(self, "Подтверждение сетевой загрузки", f"Загрузить {Path(attempt.local_file).name} в {attempt.platform.title()} ({name}) в режиме {attempt.mode}?") != QMessageBox.Yes: return
            attempt.metadata["network_approved"] = True
        attempt.scheduled_at = ""; self.container.publishing_store.save_attempt(attempt); self._run(lambda: self.manager.execute(attempt.attempt_id))

    def retry(self) -> None:
        attempt = self.selected()
        if attempt: self._run(lambda: self.manager.retry(attempt.attempt_id))

    def cancel(self) -> None:
        attempt = self.selected()
        if attempt: self.manager.cancel(attempt.attempt_id); self.refresh()

    def reschedule(self) -> None:
        attempt = self.selected()
        if not attempt: return
        value, ok = QInputDialog.getText(self, "Перепланировать", "ISO 8601 дата и время", text=attempt.scheduled_at or datetime.now(timezone.utc).isoformat())
        if ok:
            try: datetime.fromisoformat(value)
            except ValueError: QMessageBox.warning(self, "Перепланировать", "Некорректная дата ISO 8601"); return
            attempt.scheduled_at = value; attempt.status = PublishingAttemptStatus.PLANNED.value; self.container.publishing_store.save_attempt(attempt); self.refresh()

    def open_local(self) -> None:
        attempt = self.selected()
        if attempt and Path(attempt.local_file).is_file() and os.name == "nt": os.startfile(attempt.local_file)

    def open_remote(self) -> None:
        attempt = self.selected()
        if attempt:
            url = self.manager.remote_url(attempt)
            if url: webbrowser.open(url)

    def shutdown_workers(self) -> bool:
        return not bool(self._thread and self._thread.isRunning())
