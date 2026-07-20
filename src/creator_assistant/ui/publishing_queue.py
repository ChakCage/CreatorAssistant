from __future__ import annotations

import os
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QDateTime, QThread, QTimer, Qt
from PySide6.QtWidgets import (
    QDateTimeEdit, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from creator_assistant.domain.publishing import PublishingAttemptStatus
from creator_assistant.services.publishing.manager import PublishingManager
from creator_assistant.services.automation.schedule import SchedulePlanner, ScheduleValidationError
from creator_assistant.ui.workers import FunctionWorker


class PublishingQueueTab(QWidget):
    def __init__(self, container, parent=None) -> None:
        super().__init__(parent); self.container = container; self.manager: PublishingManager = container.publishing_manager; self._thread = None; self._worker = None
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Очередь публикаций — локальные задания, прогресс загрузки и remote processing"))
        actions = QHBoxLayout()
        for text, callback in (("Загрузить сейчас", self.upload_now), ("Перепланировать", self.reschedule), ("Перестроить расписание всей очереди", self.rebuild_schedule), ("Отменить", self.cancel), ("Повторить", self.retry), ("Открыть локальный файл", self.open_local), ("Открыть публикацию", self.open_remote)):
            button = QPushButton(text); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch(1); root.addLayout(actions)
        self.completion = QLabel()
        self.completion.setWordWrap(True)
        root.addWidget(self.completion)
        self.table = QTableWidget(0, 13)
        self.table.setHorizontalHeaderLabels(("Загрузить на YouTube", "Опубликовать на YouTube", "Short", "Платформа", "Аккаунт", "Local file", "Remote ID", "Upload progress", "Remote processing", "Remote publish status", "Режим", "Ошибка", "Attempt ID"))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection); root.addWidget(self.table, 1)
        self.refresh_timer = QTimer(self); self.refresh_timer.timeout.connect(self.refresh); self.refresh_timer.start(3000); self.refresh()

    def selected(self):
        item = self.table.item(self.table.currentRow(), 0); attempt_id = str(item.data(Qt.UserRole) or "") if item else ""
        return next((value for value in self.container.publishing_store.attempts() if value.attempt_id == attempt_id), None)

    def selected_attempts(self):
        ids = {
            str(self.table.item(index.row(), 0).data(Qt.UserRole) or "")
            for index in self.table.selectionModel().selectedRows()
            if self.table.item(index.row(), 0)
        }
        return [item for item in self.container.publishing_store.attempts() if item.attempt_id in ids]

    def refresh(self) -> None:
        attempts = sorted(self.container.publishing_store.attempts(), key=lambda value: value.scheduled_at or value.created_at)
        self.table.setRowCount(len(attempts))
        accounts = {item.account_id: item.display_name for item in self.container.publishing_store.accounts()}
        for row, attempt in enumerate(attempts):
            upload_when = "Сейчас" if attempt.upload_strategy == "REMOTE_SCHEDULE" else (attempt.scheduled_at or "Сейчас")
            values = (upload_when, attempt.scheduled_at or "Без расписания", attempt.short_id, attempt.platform.title(), accounts.get(attempt.account_id, attempt.account_id or "Не подключён"), attempt.local_file, attempt.remote_id or "—", f"{attempt.progress:.1f}%", attempt.processing_status or "—", attempt.status, attempt.mode, attempt.error or attempt.metadata.get("capability_notice", "—"), attempt.attempt_id)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, attempt.attempt_id); self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        active = [item for item in attempts if item.status not in {PublishingAttemptStatus.CANCELLED.value, PublishingAttemptStatus.FAILED.value}]
        ready = active and all(item.remote_id and item.status in {PublishingAttemptStatus.SCHEDULED_REMOTE.value, PublishingAttemptStatus.UPLOADED_PRIVATE.value, PublishingAttemptStatus.PUBLISHED.value} for item in active)
        self.completion.setText("Все файлы переданы YouTube. Компьютер можно выключить" if ready else "")

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
        if attempt.upload_strategy == "LOCAL_AT_TIME":
            attempt.scheduled_at = ""
        attempt.status = PublishingAttemptStatus.UPLOAD_QUEUED.value
        self.container.publishing_store.save_attempt(attempt); self._run(lambda: self.manager.execute(attempt.attempt_id))

    def retry(self) -> None:
        attempt = self.selected()
        if attempt: self._run(lambda: self.manager.retry(attempt.attempt_id))

    def cancel(self) -> None:
        attempt = self.selected()
        if attempt: self.manager.cancel(attempt.attempt_id); self.refresh()

    def reschedule(self) -> None:
        attempts = self.selected_attempts()
        if not attempts:
            return
        if any(item.status != PublishingAttemptStatus.PLANNED.value for item in attempts):
            QMessageBox.warning(self, "Перепланировать", "Изменять можно только задачи со статусом PLANNED.")
            return
        dialog = QDialog(self); dialog.setWindowTitle("Перепланировать")
        form = QFormLayout(dialog)
        editor = QDateTimeEdit(QDateTime.currentDateTime()); editor.setCalendarPopup(True); editor.setDisplayFormat("dd.MM.yyyy HH:mm")
        form.addRow("Новая дата и время первой записи", editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        first = min((datetime.fromisoformat(item.scheduled_at) for item in attempts if item.scheduled_at), default=datetime.now().astimezone())
        target = editor.dateTime().toPython().astimezone()
        delta = target - first
        for attempt in attempts:
            current = datetime.fromisoformat(attempt.scheduled_at) if attempt.scheduled_at else first
            attempt.scheduled_at = (current + delta).isoformat()
            attempt.status = PublishingAttemptStatus.PLANNED.value
        self.container.publishing_store.save_attempts_atomic(attempts)
        self.refresh()

    def rebuild_schedule(self) -> None:
        planned = [item for item in self.container.publishing_store.attempts() if item.status == PublishingAttemptStatus.PLANNED.value]
        if not planned:
            QMessageBox.information(self, "Расписание", "В очереди нет задач PLANNED.")
            return
        settings = dict(planned[0].metadata.get("schedule_settings", {}))
        try:
            self.manager.rebuild_planned_schedule(settings)
        except (ValueError, ScheduleValidationError) as exc:
            QMessageBox.warning(self, "Расписание", str(exc)); return
        self.refresh()

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
