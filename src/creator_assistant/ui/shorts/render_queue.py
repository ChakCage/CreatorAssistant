from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from creator_assistant.domain.shorts.models import Candidate, RenderJob


STATUS = {"waiting": "Ожидает", "rendering": "Рендерится", "done": "Готов", "error": "Ошибка", "cancelled": "Отменён"}


class RenderQueue(QWidget):
    render_requested = Signal(object)
    cancel_requested = Signal()
    retry_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.candidates: list[Candidate] = []
        self.jobs: dict[str, RenderJob] = {}
        self.renders_folder: Path | None = None
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(("Выбрать", "Кандидат", "Длина", "Статус", "Прогресс", "Скорость", "Файл / ошибка"))
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        render = QPushButton("Отрендерить выбранные")
        cancel = QPushButton("Отменить текущий")
        retry = QPushButton("Повторить ошибки")
        open_folder = QPushButton("Открыть папку Renders")
        render.clicked.connect(self._render)
        cancel.clicked.connect(self.cancel_requested)
        retry.clicked.connect(self._retry)
        open_folder.clicked.connect(self._open_folder)
        for button in (render, cancel, retry, open_folder):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

    def set_context(self, candidates: list[Candidate], renders_folder: Path, jobs: list[RenderJob] | None = None) -> None:
        self.candidates, self.renders_folder = candidates, renders_folder
        if jobs is not None:
            self.jobs = {job.candidate_id: job for job in jobs}
        self.refresh()

    def refresh(self) -> None:
        approved = [item for item in self.candidates if item.status == "approved"]
        self.table.setRowCount(len(approved))
        for row, candidate in enumerate(approved):
            choose = QTableWidgetItem()
            choose.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            choose.setCheckState(Qt.Checked)
            choose.setData(Qt.UserRole, candidate)
            job = self.jobs.get(candidate.id)
            values = (candidate.id, f"{candidate.duration:.1f} с", STATUS.get(job.status, job.status) if job else "Не добавлен", f"{job.progress:.1f}%" if job and job.progress is not None else "—", job.speed if job else "", (job.error or job.output_path) if job else "")
            self.table.setItem(row, 0, choose)
            for column, value in enumerate(values, 1):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))

    def update_job(self, job: RenderJob) -> None:
        self.jobs[job.candidate_id] = job
        self.refresh()

    def _render(self) -> None:
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                selected.append(item.data(Qt.UserRole))
        if selected:
            self.render_requested.emit(selected)

    def _retry(self) -> None:
        failed_ids = {job.candidate_id for job in self.jobs.values() if job.status in {"error", "cancelled"}}
        failed = [item for item in self.candidates if item.id in failed_ids and item.status == "approved"]
        if failed:
            self.retry_requested.emit(failed)

    def _open_folder(self) -> None:
        if self.renders_folder:
            self.renders_folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(self.renders_folder))
