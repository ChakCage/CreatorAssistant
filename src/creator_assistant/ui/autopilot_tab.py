from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QDate, QThread, QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QFileDialog, QFormLayout, QHBoxLayout,
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from creator_assistant.domain.automation.models import AutomationMode, AutomationProfile, AutomationStatus
from creator_assistant.ui.workers import FunctionWorker


class AutopilotResultsDialog(QDialog):
    def __init__(self, job, parent=None) -> None:
        super().__init__(parent)
        self.job = job
        self.setWindowTitle(f"Результаты автопилота — {job.job_id}")
        self.resize(980, 460)
        layout = QVBoxLayout(self)
        table = QTableWidget(0, 14)
        table.setHorizontalHeaderLabels(("Место", "Short ID", "Candidate ID", "Start", "End", "Score", "Selected", "Шаблон", "Субтитры", "Рендер", "QC", "Итог", "Файл", "Проблема"))
        rows = self.rows(job)
        table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(str(value)))
        table.horizontalHeader().setSectionResizeMode(12, QHeaderView.Stretch)
        table.cellDoubleClicked.connect(lambda row, _column: self._open_file(rows[row][12]))
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        open_folder = buttons.addButton("Открыть папку", QDialogButtonBox.ActionRole)
        open_folder.clicked.connect(self._open_folder)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.table = table

    @staticmethod
    def rows(job) -> list[tuple]:
        return [
            (
                short.candidate_rank or "—", short.short_id, short.candidate_id,
                f"{short.start:.3f}", f"{short.end:.3f}", f"{short.score:.1f}", "Да",
                short.composition_snapshot_hash[:10] or "—", short.subtitle_status,
                "Готов" if short.artifact else "Не создан",
                "Пройдена" if short.artifact and short.artifact.validated else "Не пройдена",
                short.status, short.artifact.output_path if short.artifact else "—",
                "; ".join(issue.message for issue in short.issues) or "—",
            )
            for short in job.shorts
        ]

    @staticmethod
    def _open_file(path: str) -> None:
        target = Path(path)
        if os.name == "nt" and target.is_file():
            os.startfile(str(target))

    def _open_folder(self) -> None:
        artifact = next((short.artifact for short in self.job.shorts if short.artifact), None)
        if artifact and os.name == "nt":
            os.startfile(str(Path(artifact.output_path).parent))


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
        self.template_summary = QLabel()
        self.template_preview = QPushButton("Просмотреть настройки")
        self.template_preview.clicked.connect(self._show_template_summary)
        self.profile.currentIndexChanged.connect(self._refresh_template_summary)
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
        template_row = QWidget(); template_layout = QHBoxLayout(template_row); template_layout.setContentsMargins(0, 0, 0, 0)
        template_layout.addWidget(self.template_summary, 1); template_layout.addWidget(self.template_preview)
        form.addRow("Шаблон оформления", template_row)
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
            ("Одобрить и создать тестовое расписание", self.approve_job),
            ("Удалить задание", self.delete_job),
        ):
            button = QPushButton(text); button.clicked.connect(callback); actions.addWidget(button)
        actions.addStretch(1)
        root.addLayout(actions)
        self.jobs = QTableWidget(0, 11)
        self.jobs.setHorizontalHeaderLabels(("Источник", "Режим", "Этап", "Прогресс", "Найдено", "Выбрано", "Отрендерено", "Проверено", "Проблемы", "ETA", "Ошибка"))
        self.jobs.setSelectionBehavior(QTableWidget.SelectRows)
        self.jobs.setContextMenuPolicy(Qt.ActionsContextMenu)
        delete_action = self.jobs.addAction("Удалить задание")
        delete_action.triggered.connect(self.delete_job)
        root.addWidget(self.jobs, 1)
        self._refresh_template_summary()

    def _refresh_template_summary(self) -> None:
        profile_name = self.profile.currentText() if hasattr(self, "profile") else "Автоматически"
        self.template_summary.setText(f"Текущие сохранённые настройки · {profile_name}")

    def _show_template_summary(self) -> None:
        defaults = self.container.settings.get("shorts_subtitle_defaults", {})
        branding = self.container.settings.get("shorts_branding_defaults", {})
        mode = str(defaults.get("layout_mode", "center_crop"))
        profile = self.profile.currentText()
        QMessageBox.information(
            self, "Шаблон оформления",
            f"Кадр: {mode}\nПередний слой: {int(defaults.get('foreground_scale', 100))}%\n"
            f"Субтитры: {defaults.get('style', 'clean')}\nБаннер: {profile}\n"
            f"Заголовок: русский перевод исходного названия\nРендер: 1080×1920 · source FPS · "
            f"{'H.264 NVENC' if self.container.shorts_render.prefer_nvenc else 'H.264 libx264'} · AAC",
        )

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

    def delete_job(self) -> None:
        job_id = self.selected_job_id()
        if not job_id:
            return
        answer = QMessageBox.question(
            self, "Удалить задание",
            "Удалить запись задания? Готовые MP4 и файлы проекта останутся на диске.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.engine.delete_job(job_id)
        except ValueError as exc:
            QMessageBox.information(self, "Автопилот", str(exc))
            return
        self.refresh_jobs()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Delete:
            self.delete_job()
            return
        super().keyPressEvent(event)

    def approve_job(self) -> None:
        if self.selected_job_id():
            try: self.engine.approve_and_schedule(self.selected_job_id())
            except ValueError as exc: QMessageBox.information(self, "Автопилот", str(exc))
            self.refresh_jobs()

    def open_results(self) -> None:
        job = self.engine.store.load(self.selected_job_id()) if self.selected_job_id() else None
        if not job:
            return
        self._results_dialog = AutopilotResultsDialog(job, self)
        self._results_dialog.show()
        self._results_dialog.raise_()

    def refresh_jobs(self, select_id: str = "") -> None:
        values = self.engine.store.list()
        self.jobs.setRowCount(len(values))
        for row, job in enumerate(values):
            found = sum(source.candidates_found for source in job.sources)
            selected = len(job.shorts)
            rendered = job.result.rendered_count
            checked = sum(bool(short.artifact and short.artifact.validated) for short in job.shorts)
            problems = sum(short.status in {"NEEDS_REVIEW", "FAILED"} for short in job.shorts)
            source_label = Path(job.sources[0].path).name if len(job.sources) == 1 else f"{len(job.sources)} видео"
            cells = (source_label, job.mode, job.status, f"{job.progress:.0f}%", found, selected, f"{rendered} / {selected}", f"{checked} / {rendered}", problems, str(job.resume_data.get("eta", "—")), job.error)
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
