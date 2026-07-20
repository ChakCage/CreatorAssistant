from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QDate, QThread, QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QFileDialog, QFormLayout, QHBoxLayout,
    QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHeaderView, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from creator_assistant.domain.automation.models import AutomationMode, AutomationProfile, AutomationStatus
from creator_assistant.domain.shorts.models import Candidate
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.manifest import ShortsManifestStore
from creator_assistant.services.shorts.project_template import ProjectShortsTemplate, composition_snapshot_hash
from creator_assistant.services.shorts.render_settings import VerticalRenderSettingsResolver
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore
from creator_assistant.ui.shorts.project_template_dialog import ProjectTemplateDialog, template_details
from creator_assistant.ui.workers import FunctionWorker


class AutopilotResultsDialog(QDialog):
    def __init__(self, job, parent=None) -> None:
        super().__init__(parent)
        self.job = job
        self.setWindowTitle(f"Результаты автопилота — {job.job_id}")
        self.resize(980, 460)
        layout = QVBoxLayout(self)
        summary = QLabel(job.result.summary or "")
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(summary)
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
        self._template_draft: ProjectShortsTemplate | None = None
        self._template_source = ""
        self._build_ui()
        self.refresh_jobs()
        QTimer.singleShot(500, self._offer_recovery)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        title = QLabel("Локальный автопилот Shorts — анализ, оформление, проверка, рендер и публикация")
        title.setObjectName("autopilotTitle")
        root.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(440)
        workspace = QWidget()
        cards = QGridLayout(workspace)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setHorizontalSpacing(10)
        cards.setVerticalSpacing(8)

        sources_group = QGroupBox("1. Источники")
        sources_layout = QVBoxLayout(sources_group)
        source_actions = QGridLayout()
        for index, (text, callback) in enumerate((
            ("Видео", self._add_video), ("Несколько", self._add_videos),
            ("Папка", self._add_folder), ("Удалить", self._remove_source),
            ("Очистить", self.sources_clear),
        )):
            button = QPushButton(text)
            button.clicked.connect(callback)
            source_actions.addWidget(button, index // 3, index % 3)
        sources_layout.addLayout(source_actions)
        self.sources = QListWidget()
        self.sources.setMaximumHeight(92)
        sources_layout.addWidget(self.sources)
        cards.addWidget(sources_group, 0, 0)

        mode_group = QGroupBox("2. Режим и профиль")
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.mode = QComboBox()
        self.mode.addItem("Подготовка с подтверждением", AutomationMode.APPROVAL_REQUIRED.value)
        self.mode.addItem("Полный автопилот", AutomationMode.FULL_AUTOPILOT.value)
        self.profile = QComboBox()
        self.profile.addItem("Определить автоматически", "")
        for channel in self.container.channel_assets.profiles():
            self.profile.addItem(channel.display_name, channel.id)
        self.profile.currentIndexChanged.connect(self._refresh_template_summary)
        self.mode.setMaximumWidth(320)
        self.profile.setMaximumWidth(320)
        form.addRow("Режим", self.mode)
        form.addRow("Профиль", self.profile)
        form.addRow("Количество", QLabel("Автоматически (AUTO)"))
        mode_group.setLayout(form)
        cards.addWidget(mode_group, 0, 1)

        selection_group = QGroupBox("3. Отбор Shorts")
        selection = QFormLayout(selection_group)
        selection.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.minimum_score = QDoubleSpinBox(); self.minimum_score.setRange(0, 100); self.minimum_score.setDecimals(1); self.minimum_score.setValue(80); self.minimum_score.setMaximumWidth(100)
        self.minimum_score.valueChanged.connect(self._update_score_forecast)
        self.weakest_score = QPushButton("Выбрать всех найденных кандидатов")
        self.weakest_score.setToolTip("Установить минимальную оценку по оценке самого слабого найденного кандидата, чтобы выбрать все текущие результаты")
        self.weakest_score.clicked.connect(self._set_weakest_score)
        self.maximum_per_source = QSpinBox(); self.maximum_per_source.setRange(0, 50); self.maximum_per_source.setValue(10); self.maximum_per_source.setMaximumWidth(100)
        self.score_forecast = QLabel("Нет результатов анализа")
        self.score_forecast.setWordWrap(True)
        selection.addRow("Минимальный score", self.minimum_score)
        selection.addRow("Максимум с источника", self.maximum_per_source)
        selection.addRow(self.weakest_score)
        selection.addRow(self.score_forecast)
        cards.addWidget(selection_group, 1, 0)

        schedule_group = QGroupBox("5. Расписание")
        schedule = QFormLayout(schedule_group)
        schedule.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.start_date = QDateEdit(QDate.currentDate()); self.start_date.setCalendarPopup(True)
        self.timezone = QLineEdit("Europe/Moscow"); self.timezone.setMaximumWidth(220)
        self.per_day = QSpinBox(); self.per_day.setRange(1, 10); self.per_day.setValue(2); self.per_day.setMaximumWidth(100)
        self.slots = QLineEdit("13:00, 19:00"); self.slots.setMaximumWidth(220)
        schedule.addRow("Дата начала", self.start_date)
        schedule.addRow("Timezone", self.timezone)
        schedule.addRow("В день", self.per_day)
        schedule.addRow("Слоты", self.slots)
        cards.addWidget(schedule_group, 1, 1)

        platforms_group = QGroupBox("6. Подключённые платформы")
        platforms_layout = QHBoxLayout(platforms_group)
        self.youtube = QCheckBox("YouTube"); self.youtube.setChecked(True)
        self.tiktok = QCheckBox("TikTok")
        platforms_layout.addWidget(self.youtube); platforms_layout.addWidget(self.tiktok); platforms_layout.addStretch(1)
        self.platform_status = QLabel()
        self.platform_status.setWordWrap(True)
        platforms_layout.addWidget(self.platform_status)
        cards.addWidget(platforms_group, 2, 1)

        template_group = QGroupBox("4. Оформление")
        template_group_layout = QHBoxLayout(template_group)
        self.template_summary = QLabel()
        self.template_summary.setWordWrap(True)
        self.template_summary.setMaximumWidth(240)
        self.template_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        template_group_layout.addWidget(self.template_summary)
        template_group_layout.addStretch(1)
        for text, callback, tooltip in (
            ("Редактировать", self._open_vertical_editor, "Открыть ProjectShortsTemplate во вкладке Shorts → Вертикальный редактор"),
            ("Просмотреть", self._show_template_summary, "Показать шаблон, который будет использован Autopilot"),
        ):
            button = QPushButton(text)
            button.setToolTip(tooltip)
            button.clicked.connect(callback)
            template_group_layout.addWidget(button)
        cards.addWidget(template_group, 2, 0)
        cards.setColumnStretch(0, 1)
        cards.setColumnStretch(1, 1)
        scroll.setWidget(workspace)
        root.addWidget(scroll)

        primary_group = QGroupBox("7. Задания — управление")
        primary = QHBoxLayout(primary_group)
        for text, callback in (
            ("Запустить", self.start_job), ("Пауза", self.pause_job), ("Продолжить", self.resume_job),
            ("Отменить", self.cancel_job),
        ):
            button = QPushButton(text); button.clicked.connect(callback); primary.addWidget(button)
        primary.addStretch(1)
        results_group = QGroupBox("Результаты")
        results = QHBoxLayout(results_group)
        for text, callback in (("Открыть результаты", self.open_results), ("Одобрить и запланировать", self.approve_job), ("Удалить задание", self.delete_job)):
            button = QPushButton(text); button.clicked.connect(callback); results.addWidget(button)
        results.addStretch(1)
        action_row = QHBoxLayout(); action_row.addWidget(primary_group); action_row.addWidget(results_group)
        root.addLayout(action_row)
        self.jobs = QTableWidget(0, 11)
        self.jobs.setHorizontalHeaderLabels(("Источник", "Режим", "Этап", "Прогресс", "Найдено", "Выбрано", "Отрендерено", "Проверено", "Проблемы", "ETA", "Ошибка"))
        self.jobs.setSelectionBehavior(QTableWidget.SelectRows)
        self.jobs.setContextMenuPolicy(Qt.ActionsContextMenu)
        delete_action = self.jobs.addAction("Удалить задание")
        delete_action.triggered.connect(self.delete_job)
        root.addWidget(self.jobs, 1)
        self._refresh_template_summary()
        self._update_score_forecast()
        self._refresh_platform_status()

    def _refresh_platform_status(self) -> None:
        if not hasattr(self, "platform_status"):
            return
        accounts = self.container.publishing_store.accounts() if hasattr(self.container, "publishing_store") else []
        parts = []
        for platform in ("youtube", "tiktok"):
            connected = [item for item in accounts if item.platform == platform and item.status == "CONNECTED"]
            parts.append(f"{platform.title()}: {len(connected)} подключено")
        mode = str(getattr(self.container, "settings", {}).get("publishing", {}).get("mode", "DRY_RUN"))
        self.platform_status.setText(" · ".join(parts) + f" · {mode}")

    def _refresh_template_summary(self) -> None:
        template, source = self._resolved_template()
        layout = template.layout or {}
        subtitle = template.subtitle or {}
        branding = template.branding or {}
        mode = str(layout.get("mode", "center_crop")).replace("_", " ").title()
        scale = int(float(layout.get("foreground_scale", 1.0)) * 100)
        preset = str(subtitle.get("preset", "clean")).title()
        profile = str(branding.get("profile_id") or self.profile.currentText() or "Авто")
        self.template_summary.setText(f"Шаблон проекта\n{mode} · {scale}% · {preset} · {profile}")
        self.template_summary.setToolTip(template_details(template, source))

    def _show_template_summary(self) -> None:
        template, source = self._resolved_template()
        ProjectTemplateDialog(template, source, self).exec()

    def _score_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.minimum_score)
        layout.addWidget(self.weakest_score)
        return row

    def _source_paths(self) -> list[Path]:
        return [Path(self.sources.item(index).text()) for index in range(self.sources.count())]

    def _resolved_template(self) -> tuple[ProjectShortsTemplate, str]:
        if self._template_draft:
            return self._template_draft, self._template_source or "Черновик Autopilot"
        paths = self._source_paths() if hasattr(self, "sources") else []
        if paths:
            root = ShortsProjectStore.suggested_root(paths[0])
            manifest = ShortsManifestStore(root / "shorts_manifest.json").load()
            stored = ProjectShortsTemplate.from_dict(manifest.shorts_template if manifest else None)
            if stored:
                return stored, f"Шаблон проекта: {root}"
        settings_store = getattr(self.container, "settings", {})
        branding = settings_store.get("shorts_branding_defaults", {})
        candidate = Candidate("defaults", 0, 30, 0, "")
        resolved = VerticalRenderSettingsResolver(settings_store, ChannelAssetStore()).resolve(
            candidate,
            selected_profile_id=str(self.profile.currentData() or "") if hasattr(self, "profile") else "",
            use_candidate_override=False,
        )
        candidate.subtitle_settings = resolved.subtitle
        candidate.layout_settings = resolved.layout
        candidate.branding_settings = {**resolved.branding, **branding}
        return ProjectShortsTemplate.from_candidate(candidate), "Пользовательские настройки"

    def _current_shorts_tab(self):
        return getattr(self.window(), "shorts_tab", None)

    def _open_vertical_editor(self) -> None:
        main = self.window()
        shorts_tab = self._current_shorts_tab()
        paths = self._source_paths()
        if not shorts_tab or not hasattr(main, "tabs"):
            return
        main.tabs.setCurrentWidget(shorts_tab)
        if paths:
            shorts_tab.open_source(paths[0])
        shorts_tab.workspace.setCurrentWidget(shorts_tab.subtitle_editor)

    def _take_current_short(self) -> None:
        shorts_tab = self._current_shorts_tab()
        candidate = getattr(getattr(shorts_tab, "subtitle_editor", None), "candidate", None)
        if not candidate:
            QMessageBox.information(self, "Шаблон оформления", "Сначала откройте Short в вертикальном редакторе.")
            return
        self._template_draft = ProjectShortsTemplate.from_candidate(candidate, {
            "width": 1080, "height": 1920, "fps_policy": "source",
            "encoder": "h264_nvenc" if self.container.shorts_render.prefer_nvenc else "libx264",
            "audio_codec": "aac",
        })
        self._template_source = f"Настройки только этого Short: {candidate.id}"
        self._refresh_template_summary()

    def _save_template_project(self) -> None:
        shorts_tab = self._current_shorts_tab()
        candidate = getattr(getattr(shorts_tab, "subtitle_editor", None), "candidate", None)
        if candidate and getattr(shorts_tab, "paths", None):
            shorts_tab._save_project_template(candidate)
            self._template_draft = None
            self._template_source = ""
            self._refresh_template_summary()
            return
        QMessageBox.information(self, "Шаблон оформления", "Откройте нужный проект и Short в вертикальном редакторе.")

    def _apply_template_all(self) -> None:
        shorts_tab = self._current_shorts_tab()
        if not shorts_tab or not getattr(shorts_tab, "paths", None):
            QMessageBox.information(self, "Шаблон оформления", "Сначала откройте проект в вертикальном редакторе.")
            return
        shorts_tab._apply_project_template_all()
        self._refresh_template_summary()

    def _reset_template_defaults(self) -> None:
        self._template_draft = None
        self._template_source = ""
        paths = self._source_paths()
        if paths:
            root = ShortsProjectStore.suggested_root(paths[0])
            manifest = ShortsManifestStore(root / "shorts_manifest.json").load()
            if manifest:
                manifest.shorts_template = {}
                ShortsManifestStore(root / "shorts_manifest.json").save(manifest)
        self._refresh_template_summary()

    def _set_weakest_score(self) -> None:
        scores = self._candidate_scores()
        if not scores:
            QMessageBox.information(self, "Минимальный score", "Сначала проанализируйте источник или выберите готовое задание.")
            return
        self.minimum_score.setValue(min(scores))
        self._update_score_forecast()

    def _candidate_scores(self) -> list[float]:
        scores: list[float] = []
        job = self.engine.store.load(self.selected_job_id()) if self.selected_job_id() else None
        if job:
            scores.extend(float(item.get("score", 0)) for item in job.resume_data.get("selection_details", []) if item.get("score") is not None)
        if not scores:
            for source in self._source_paths():
                path = ShortsProjectStore.suggested_root(source) / "Analysis" / "candidates.json"
                if not path.is_file():
                    continue
                try:
                    import json
                    scores.extend(float(item.get("final_score") or item.get("score") or 0) for item in json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError, TypeError):
                    continue
        return scores

    def _update_score_forecast(self, *_args) -> None:
        if not hasattr(self, "score_forecast"):
            return
        scores = self._candidate_scores()
        if not scores:
            self.score_forecast.setText("Нет результатов анализа для прогноза отбора")
            return
        threshold = float(self.minimum_score.value())
        selected = sum(score >= threshold for score in scores)
        self.score_forecast.setText(
            f"При пороге {threshold:g} будет выбрано {selected} из {len(scores)} кандидатов"
        )

    def sources_clear(self) -> None:
        self.sources.clear()
        self._template_draft = None
        self._refresh_template_summary()

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
        self._template_draft = None
        self._refresh_template_summary()

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
        template, source = self._resolved_template()
        job.composition_snapshot = {
            "schema_version": 1,
            "template": template.to_dict(),
            "selected_profile_id": profile_id,
            "source": source,
        }
        job.composition_snapshot_hash = composition_snapshot_hash(job.composition_snapshot)
        self.engine.store.save(job)
        self.refresh_jobs(select_id=job.job_id)
        self._run_background(job.job_id)

    def _run_background(self, job_id: str, resume: bool = False) -> None:
        if self._thread and self._thread.isRunning():
            return
        thread = QThread(self)
        worker = FunctionWorker(lambda _progress: self.engine.resume(job_id) if resume else self.engine.run(job_id))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda job: self._job_finished(job_id, job))
        worker.finished.connect(thread.quit)
        worker.failed.connect(lambda message, _details: QMessageBox.critical(self, "Автопилот", message))
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._thread_finished)
        self._thread, self._worker = thread, worker
        thread.start()

    def _job_finished(self, job_id: str, job) -> None:
        self.refresh_jobs(select_id=job_id)
        if getattr(job, "status", "") == "SCHEDULED":
            self._sync_publishing(job)

    def _sync_publishing(self, job, network_approved: bool = False) -> None:
        plan = getattr(job.result, "publishing_plan", None)
        if not plan:
            return
        accounts = self.container.publishing_store.accounts()
        mode = str(self.container.settings.get("publishing", {}).get("mode", "DRY_RUN"))
        shorts_by_id = {item.short_id: item for item in job.shorts if item.artifact and item.artifact.validated}
        for slot in plan.slots:
            short = shorts_by_id.get(slot.short_id)
            if not short:
                continue
            for platform in slot.platforms:
                account = next((item for item in accounts if item.platform == platform and item.status == "CONNECTED"), None)
                metadata = {"title": short.title or short.short_id, "short_id": short.short_id, "network_approved": network_approved}
                if platform == "tiktok":
                    metadata.update({"post_mode": "draft", "privacy_level": "SELF_ONLY"})
                self.container.publishing_manager.create_attempt(
                    short.short_id, platform, account.account_id if account else "",
                    short.platform_artifacts.get(platform, short.artifact.output_path), mode=mode, scheduled_at=slot.scheduled_at, metadata=metadata,
                )
        queue = getattr(self.window(), "publishing_queue_tab", None)
        if queue:
            queue.refresh()

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
            publishing_mode = str(getattr(self.container, "settings", {}).get("publishing", {}).get("mode", "DRY_RUN"))
            approved = publishing_mode == "DRY_RUN"
            if not approved:
                approved = QMessageBox.question(
                    self, "Подтверждение публикации",
                    f"Создать сетевые задания публикации в режиме {publishing_mode}? Перед отправкой проверьте выбранные аккаунты и приватность.",
                    QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
                ) == QMessageBox.Yes
                if not approved:
                    return
            try:
                job = self.engine.approve_and_schedule(self.selected_job_id())
                self._sync_publishing(job, network_approved=approved)
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
                if column == 5 and job.result.summary:
                    item.setToolTip(job.result.summary)
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
