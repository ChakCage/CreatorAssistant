from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QDate, QThread, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from creator_assistant.domain.automation.models import AutomationMode, AutomationProfile, AutomationStatus
from creator_assistant.ui.workers import FunctionWorker


class AutopilotTab(QWidget):
    def __init__(self, container, parent=None) -> None:
        super().__init__(parent)
        self.container = container
        self.engine = container.automation_engine
        self._thread: QThread | None = None
        self._worker = None
        self._build_ui()
        self.refresh_jobs()
        QTimer.singleShot(500, self._offer_recovery)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Локальный автопилот Shorts — анализ, оформление, проверка, рендер и внутреннее расписание"))
        source_actions = QHBoxLayout()
        for text, callback in (
            ("Добавить видео", self._add_video), ("Добавить несколько видео", self._add_videos),
            ("Добавить папку", self._add_folder), ("Удалить источник", self._remove_source),
            ("Очистить список", self.sources_clear),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            source_actions.addWidget(button)
        root.addLayout(source_actions)
        self.sources = QListWidget()
        self.sources.setMaximumHeight(110)
        root.addWidget(self.sources)

        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("Подготовка с подтверждением", AutomationMode.APPROVAL_REQUIRED.value)
        self.mode.addItem("Полный автопилот", AutomationMode.FULL_AUTOPILOT.value)
        self.profile = QComboBox()
        self.profile.addItem("Определить автоматически", "")
        for channel in self.container.channel_assets.profiles():
            self.profile.addItem(channel.display_name, channel.id)
        self.minimum_score = QSpinBox(); self.minimum_score.setRange(0, 100); self.minimum_score.setValue(80)
        self.maximum_per_source = QSpinBox(); self.maximum_per_source.setRange(0, 50); self.maximum_per_source.setValue(10)
        self.start_date = QDateEdit(QDate.currentDate()); self.start_date.setCalendarPopup(True)
        self.timezone = QLineEdit("Europe/Moscow")
        self.per_day = QSpinBox(); self.per_day.setRange(1, 10); self.per_day.setValue(2)
        self.slots = QLineEdit("13:00, 19:00")
        self.youtube = QCheckBox("YouTube"); self.youtube.setChecked(True)
        self.tiktok = QCheckBox("TikTok")
        platforms = QWidget(); platform_layout = QHBoxLayout(platforms); platform_layout.setContentsMargins(0, 0, 0, 0); platform_layout.addWidget(self.youtube); platform_layout.addWidget(self.tiktok); platform_layout.addStretch(1)
        form.addRow("Режим", self.mode)
        form.addRow("Профиль", self.profile)
        form.addRow("Количество", QLabel("Автоматически (AUTO)"))
        form.addRow("Минимальный score", self.minimum_score)
        form.addRow("Максимум с источника", self.maximum_per_source)
        form.addRow("Дата начала", self.start_date)
        form.addRow("Timezone", self.timezone)
        form.addRow("Публикаций в день", self.per_day)
        form.addRow("Временные слоты", self.slots)
        form.addRow("Платформы", platforms)
        root.addLayout(form)

        actions = QHBoxLayout()
        for text, callback in (
            ("Запустить", self.start_job), ("Пауза", self.pause_job), ("Продолжить", self.resume_job),
            ("Отменить", self.cancel_job), ("Открыть результаты", self.open_results),
            ("Одобрить и запланировать", self.approve_job),
        ):
            button = QPushButton(text); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch(1)
        root.addLayout(actions)
        self.jobs = QTableWidget(0, 10)
        self.jobs.setHorizontalHeaderLabels(("Источник", "Режим", "Этап", "Прогресс", "Найдено", "Выбрано", "Рендер", "Проверка", "ETA", "Ошибка"))
        self.jobs.setSelectionBehavior(QTableWidget.SelectRows)
        root.addWidget(self.jobs, 1)

    def sources_clear(self) -> None:
        self.sources.clear()

    def _add_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Добавить видео", "", "Video (*.mp4 *.mkv *.mov *.webm)")
        if path:
            self._append_sources([path])

    def _add_videos(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Добавить видео", "", "Video (*.mp4 *.mkv *.mov *.webm)")
        self._append_sources(paths)

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Добавить папку")
        if folder:
            extensions = {".mp4", ".mkv", ".mov", ".webm"}
            self._append_sources([str(path) for path in Path(folder).iterdir() if path.suffix.lower() in extensions])

    def _append_sources(self, paths) -> None:
        existing = {self.sources.item(index).text() for index in range(self.sources.count())}
        for path in paths:
            if path not in existing:
                self.sources.addItem(path); existing.add(path)

    def _remove_source(self) -> None:
        for item in self.sources.selectedItems():
            self.sources.takeItem(self.sources.row(item))

    def start_job(self) -> None:
        paths = [self.sources.item(index).text() for index in range(self.sources.count())]
        if not paths:
            QMessageBox.information(self, "Автопилот", "Добавьте хотя бы одно видео.")
            return
        profile_id = str(self.profile.currentData() or "")
        platforms = [name for name, control in (("youtube", self.youtube), ("tiktok", self.tiktok)) if control.isChecked()]
        job = self.engine.create_job(
            paths, mode=str(self.mode.currentData()),
            profile=AutomationProfile(channel_profile_id=profile_id, auto_detect=not bool(profile_id)),
            selection_settings={"minimum_score": self.minimum_score.value(), "maximum_per_source": self.maximum_per_source.value()},
            schedule_settings={
                "start_date": self.start_date.date().toString("yyyy-MM-dd"), "timezone": self.timezone.text().strip(),
                "publications_per_day": self.per_day.value(),
                "preferred_time_slots": [value.strip() for value in self.slots.text().split(",") if value.strip()],
            }, platforms=platforms,
        )
        self.refresh_jobs(select_id=job.job_id)
        self._run_background(job.job_id)

    def _run_background(self, job_id: str, resume: bool = False) -> None:
        if self._thread and self._thread.isRunning():
            return
        thread = QThread(self)
        worker = FunctionWorker(lambda _progress: self.engine.resume(job_id) if resume else self.engine.run(job_id))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda _job: self.refresh_jobs(select_id=job_id))
        worker.finished.connect(thread.quit)
        worker.failed.connect(lambda message, _details: QMessageBox.critical(self, "Автопилот", message))
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._thread_finished)
        self._thread, self._worker = thread, worker
        thread.start()

    def _thread_finished(self) -> None:
        thread = self._thread
        self._thread = None; self._worker = None
        if thread:
            thread.deleteLater()
        self.refresh_jobs()

    def selected_job_id(self) -> str:
        item = self.jobs.item(self.jobs.currentRow(), 0)
        return str(item.data(256) or "") if item else ""

    def pause_job(self) -> None:
        if self.selected_job_id(): self.engine.pause(self.selected_job_id()); self.refresh_jobs()

    def resume_job(self) -> None:
        if self.selected_job_id(): self._run_background(self.selected_job_id(), resume=True)

    def cancel_job(self) -> None:
        if self.selected_job_id(): self.engine.cancel(self.selected_job_id()); self.refresh_jobs()

    def approve_job(self) -> None:
        if self.selected_job_id():
            try: self.engine.approve_and_schedule(self.selected_job_id())
            except ValueError as exc: QMessageBox.information(self, "Автопилот", str(exc))
            self.refresh_jobs()

    def open_results(self) -> None:
        job = self.engine.store.load(self.selected_job_id()) if self.selected_job_id() else None
        artifact = next((short.artifact for short in (job.shorts if job else []) if short.artifact), None)
        if artifact and os.name == "nt":
            os.startfile(str(Path(artifact.output_path).parent))

    def refresh_jobs(self, select_id: str = "") -> None:
        values = self.engine.store.list()
        self.jobs.setRowCount(len(values))
        for row, job in enumerate(values):
            found = sum(source.candidates_found for source in job.sources)
            selected = len(job.shorts)
            rendered = job.result.rendered_count
            review = job.result.needs_review_count
            source_label = Path(job.sources[0].path).name if len(job.sources) == 1 else f"{len(job.sources)} видео"
            cells = (source_label, job.mode, job.status, f"{job.progress:.0f}%", found, selected, rendered, review, str(job.resume_data.get("eta", "—")), job.error)
            for column, value in enumerate(cells):
                item = QTableWidgetItem(str(value)); item.setData(256, job.job_id); self.jobs.setItem(row, column, item)
            if job.job_id == select_id:
                self.jobs.selectRow(row)
        self.jobs.resizeColumnsToContents()

    def _offer_recovery(self) -> None:
        unfinished = self.engine.store.unfinished()
        if not unfinished:
            return
        job = unfinished[0]
        box = QMessageBox(self)
        box.setWindowTitle("Незавершённый автопилот")
        box.setText(f"Найдено незавершённое задание {job.job_id}.")
        resume = box.addButton("Продолжить", QMessageBox.AcceptRole)
        cancel = box.addButton("Отменить задание", QMessageBox.DestructiveRole)
        open_results = box.addButton("Открыть результаты", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() is resume:
            self._run_background(job.job_id, resume=True)
        elif box.clickedButton() is cancel:
            self.engine.cancel(job.job_id); self.refresh_jobs()
        elif box.clickedButton() is open_results:
            self.refresh_jobs(select_id=job.job_id)
            self.open_results()

    def shutdown_workers(self) -> bool:
        if not self._thread or not self._thread.isRunning():
            return True
        job_id = self.selected_job_id()
        if job_id:
            self.engine.pause(job_id)
        self._thread.quit()
        return self._thread.wait(5000)
