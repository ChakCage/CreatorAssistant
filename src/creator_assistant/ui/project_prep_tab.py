from __future__ import annotations

import os
import json
import subprocess
import re
import time
import traceback
import uuid
from enum import Enum
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from creator_assistant.app import ServiceContainer
from creator_assistant.domain.author_presets import AuthorMatchKind, AuthorPreset, merge_presets
from creator_assistant.domain.errors import JobCancelledError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import ProgressInfo, ProjectOptions, VideoMetadata
from creator_assistant.domain.progress import WeightedProgressTracker, format_bytes, format_duration
from creator_assistant.domain.stages import JobStage, ORDERED_STAGES, stage_display_name
from creator_assistant.infrastructure.windows_paths import discover_author_folders
from creator_assistant.infrastructure.project_index import ProjectRoot
from creator_assistant.infrastructure.crash_logging import event as crash_event, safe_call, update_context
from creator_assistant.services.format_selector import build_format_plan
from creator_assistant.services.metadata_service import validate_youtube_url, youtube_video_id
from creator_assistant.services.author_preset_resolver import AuthorPresetResolver
from creator_assistant.ui.settings_dialog import SettingsDialog
from creator_assistant.ui.youtube_auth_dialog import YouTubeAuthDialog
from creator_assistant.ui.manual_uvr_dialog import ManualUvrDialog
from creator_assistant.ui.media_role_resolution_dialog import MediaRoleResolutionDialog
from creator_assistant.ui.widgets.error_dialog import ErrorDialog
from creator_assistant.ui.workers import FunctionWorker, UiWorkerBridge


class UiJobState(str, Enum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"
    WAITING_FOR_DISK_SPACE = "WAITING_FOR_DISK_SPACE"
    WAITING_FOR_MEDIA_SELECTION = "WAITING_FOR_MEDIA_SELECTION"


class PlanStatus(str, Enum):
    ACTION_REQUIRED = "ACTION_REQUIRED"
    ALREADY_COMPLETE = "ALREADY_COMPLETE"


class ProjectPrepTab(QWidget):
    def __init__(self, container: ServiceContainer, parent=None) -> None:
        super().__init__(parent)
        self.container = container
        self.metadata: Optional[VideoMetadata] = None
        self.token: Optional[CancellationToken] = None
        self._threads: list[QThread] = []
        self.progress_tracker: Optional[WeightedProgressTracker] = None
        self._last_progress_paint = 0.0
        self._last_painted_stage = ""
        self.metadata_request_in_progress = False
        self.current_request_id: Optional[str] = None
        self.metadata_thread: Optional[QThread] = None
        self.metadata_worker: Optional[FunctionWorker] = None
        self._metadata_generation_id = 0
        self._metadata_terminal = None
        self._metadata_requested_url = ""
        self._metadata_requested_video_id = ""
        self._metadata_request_started = 0.0
        self._auth_retried_video_ids = set()
        self._manual_anonymous_retried_video_ids = set()
        self.job_state = UiJobState.IDLE
        self._job_generation = 0
        self.active_job_id: Optional[str] = None
        self.active_job_proxy_height: Optional[int] = None
        self.active_worker = None
        self.active_thread: Optional[QThread] = None
        self._reset_after_cancel = False
        self._pending_url_after_cancel = ""
        self.selected_project_path: Optional[Path] = None
        self.current_project_path: Optional[Path] = None
        self.current_rpp_path: Optional[Path] = None
        self.current_veg_path: Optional[Path] = None
        self._auto_opened_vegas_path = ""
        self.project_selection = "new"
        self._resume_states = {}
        self.plan_status = PlanStatus.ACTION_REQUIRED
        self._active_project_url = ""
        self.author_presets: list[AuthorPreset] = []
        self._author_combo_programmatic = False
        self._author_manual_override_video_id = ""
        self._author_resolution_kind = AuthorMatchKind.NO_MATCH
        self._discovery_thread: Optional[QThread] = None
        self._discovery_generation = 0
        self._discovery_video_id = ""
        self._pending_discovery_video_id = ""
        self.network = QNetworkAccessManager(self)
        self._build_ui()
        settings_service = getattr(self.container, "settings_service", None)
        if settings_service is not None:
            settings_service.settings_changed.connect(
                self.apply_settings,
                Qt.ConnectionType.UniqueConnection,
            )
        self.reload_authors()

    def _safe_ui_action(self, name: str, callback):
        return safe_call(name, callback, self._ui_action_failed)

    def _ui_action_failed(self, exc: BaseException, details: str) -> None:
        if self.token:
            self.token.cancel()
        self.token = None
        self._set_job_state(UiJobState.FAILED)
        self._set_busy(False)
        self.progress_label.setText("Операция завершилась с внутренней ошибкой. Приложение продолжает работу.")
        ErrorDialog(str(exc) or exc.__class__.__name__, details, self).exec()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Подготовка проекта для озвучки")
        title.setObjectName("pageTitle")
        self.subtitle = QLabel()
        self.subtitle.setProperty("class", "muted")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.dry_run_button = QPushButton("Dry Run")
        self.dry_run_button.setEnabled(False)
        self.dry_run_button.clicked.connect(self._safe_ui_action("dry_run", self.run_dry_run))
        header.addWidget(self.dry_run_button)
        root.addLayout(header)

        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        destination_group = QGroupBox("Назначение")
        destination_layout = QGridLayout(destination_group)
        self.author_combo = QComboBox()
        self.author_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.author_combo.currentIndexChanged.connect(self._author_changed)
        add_button = QPushButton("Добавить путь")
        remove_button = QPushButton("Удалить пресет")
        settings_button = QPushButton("Настройки")
        add_button.clicked.connect(self._safe_ui_action("add_author_path", self.add_path))
        remove_button.clicked.connect(self._safe_ui_action("remove_author_path", self.remove_custom_path))
        settings_button.clicked.connect(self._safe_ui_action("open_settings", self.open_settings))
        destination_layout.addWidget(self.author_combo, 0, 0, 1, 3)
        self.author_status_label = QLabel("Автор: не определён")
        self.author_status_label.setProperty("class", "muted")
        self.author_status_label.setWordWrap(True)
        destination_layout.addWidget(self.author_status_label, 1, 0, 1, 3)
        destination_layout.addWidget(add_button, 2, 0)
        destination_layout.addWidget(remove_button, 2, 1)
        destination_layout.addWidget(settings_button, 2, 2)
        left_layout.addWidget(destination_group)

        source_group = QGroupBox("Видео YouTube")
        source_layout = QFormLayout(source_group)
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://www.youtube.com/watch?v=…")
        self.url_edit.textChanged.connect(self._safe_ui_action("url_changed", self._url_changed))
        self.url_edit.returnPressed.connect(self._safe_ui_action("metadata_enter", lambda: self.request_metadata("enter")))
        self.fetch_button = QPushButton("Получить информацию")
        self.fetch_button.setEnabled(False)
        self.fetch_button.clicked.connect(self._safe_ui_action("metadata_button", lambda: self.request_metadata("button")))
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.fetch_button)
        source_layout.addRow("Ссылка", url_row)
        self.title_edit = QLineEdit()
        self.title_edit.setReadOnly(True)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        source_layout.addRow("Название", self.title_edit)
        source_layout.addRow("Путь проекта", self.path_edit)
        self.preflight_label = QLabel("Проверка проекта ещё не выполнялась")
        self.preflight_label.setProperty("class", "muted")
        self.preflight_label.setWordWrap(True)
        source_layout.addRow("Предварительная проверка", self.preflight_label)
        left_layout.addWidget(source_group)

        info_group = QGroupBox("Найденная информация")
        info_layout = QGridLayout(info_group)
        self.thumbnail_label = QLabel("Превью появится после проверки ссылки")
        self.thumbnail_label.setObjectName("thumbnail")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setMinimumSize(320, 180)
        self.thumbnail_label.setMaximumHeight(240)
        info_layout.addWidget(self.thumbnail_label, 0, 0, 1, 4)
        self.info_labels = {}
        self.info_captions = {}
        for column, (key, caption) in enumerate(
            (
                ("resolution", "Максимум SDR"),
                ("fps", "FPS"),
                ("duration", "Длительность"),
                ("max_size", "Макс. видео"),
                ("proxy_size", "Видео-прокси"),
            )
        ):
            box = QFrame()
            box.setObjectName("infoCard")
            card = QVBoxLayout(box)
            cap = QLabel(caption)
            cap.setProperty("class", "muted")
            value = QLabel("—")
            value.setObjectName("infoValue")
            card.addWidget(cap)
            card.addWidget(value)
            info_layout.addWidget(box, 1, column)
            self.info_labels[key] = value
            self.info_captions[key] = cap
        left_layout.addWidget(info_group)

        options_group = QGroupBox("Что создать")
        options_layout = QGridLayout(options_group)
        self.max_check = QCheckBox("Максимальное SDR-видео")
        self.proxy_check = QCheckBox("Видео-прокси для REAPER")
        self.audio_check = QCheckBox("Оригинальная аудиодорожка")
        self.instrumental_check = QCheckBox("Инструментал через UVR")
        self.reaper_check = QCheckBox("Проект REAPER")
        self.vegas_check = QCheckBox("Проект VEGAS")
        for check in (self.max_check, self.proxy_check, self.audio_check, self.instrumental_check, self.reaper_check):
            check.setChecked(True)
        self.vegas_check.setChecked(bool(self.container.settings.get("create_vegas_project_default", True)))
        self.audio_check.toggled.connect(self._sync_options)
        self.proxy_check.toggled.connect(self._sync_options)
        self.instrumental_check.toggled.connect(self._sync_options)
        self.max_check.toggled.connect(self._sync_options)
        self.vegas_check.toggled.connect(self._sync_options)
        self.reaper_check.toggled.connect(self._sync_options)
        options_layout.addWidget(self.max_check, 0, 0)
        options_layout.addWidget(self.proxy_check, 0, 1)
        options_layout.addWidget(self.audio_check, 1, 0)
        options_layout.addWidget(self.instrumental_check, 1, 1)
        options_layout.addWidget(self.reaper_check, 2, 0)
        options_layout.addWidget(self.vegas_check, 2, 1)
        self.backend_label = QLabel()
        self.backend_label.setProperty("class", "muted")
        self.backend_label.setWordWrap(True)
        options_layout.addWidget(self.backend_label, 3, 0, 1, 2)
        self._refresh_backend_label()
        left_layout.addWidget(options_group)
        actions = QHBoxLayout()
        self.create_button = QPushButton("Создать проект")
        self.create_button.setObjectName("primaryButton")
        self.create_button.setEnabled(False)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setEnabled(False)
        self.new_project_button = QPushButton("Новый проект")
        self.new_project_button.setEnabled(False)
        self.open_rpp_button = QPushButton("Открыть RPP")
        self.open_vegas_button = QPushButton("Открыть проект VEGAS")
        self.open_folder_button = QPushButton("Открыть папку проекта")
        self.rebuild_index_button = QPushButton("Пересоздать индекс материалов")
        self.open_rpp_button.setEnabled(False)
        self.open_vegas_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)
        self.rebuild_index_button.setEnabled(False)
        self.create_button.clicked.connect(self._safe_ui_action("continue_or_create_project", self.create_project))
        self.cancel_button.clicked.connect(self._safe_ui_action("cancel", self.cancel_job))
        self.new_project_button.clicked.connect(self._safe_ui_action("new_project", self.new_project))
        self.open_rpp_button.clicked.connect(self._safe_ui_action("open_current_rpp", self.open_current_rpp))
        self.open_vegas_button.clicked.connect(self._safe_ui_action("open_current_vegas", self.open_current_vegas))
        self.open_folder_button.clicked.connect(self._safe_ui_action("open_current_project_folder", self.open_current_project_folder))
        self.rebuild_index_button.clicked.connect(self._safe_ui_action("rebuild_media_index", self.rebuild_media_index))
        actions.addWidget(self.create_button, 1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.open_rpp_button)
        actions.addWidget(self.open_vegas_button)
        actions.addWidget(self.open_folder_button)
        actions.addWidget(self.rebuild_index_button)
        actions.addWidget(self.new_project_button)
        left_layout.addLayout(actions)
        self._refresh_proxy_labels()

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        progress_title = QLabel("Ход выполнения")
        progress_title.setObjectName("sectionTitle")
        right_layout.addWidget(progress_title)
        self.stage_list = QListWidget()
        for stage in ORDERED_STAGES:
            self.stage_list.addItem("○  " + stage_display_name(stage, self._proxy_height()))
        right_layout.addWidget(self.stage_list, 2)
        self.progress_label = QLabel("Ожидание")
        self.progress_label.setWordWrap(True)
        self.stage_progress_caption = QLabel("Текущий этап: 0%")
        self.stage_progress_bar = QProgressBar()
        self.stage_progress_bar.setRange(0, 100)
        self.overall_progress_caption = QLabel("Общий прогресс проекта: 0%")
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setRange(0, 100)
        self.metrics_label = QLabel("Скорость: —\nЗагружено: —\nОсталось: рассчитывается")
        self.metrics_label.setProperty("class", "muted")
        right_layout.addWidget(self.progress_label)
        right_layout.addWidget(self.stage_progress_caption)
        right_layout.addWidget(self.stage_progress_bar)
        right_layout.addWidget(self.overall_progress_caption)
        right_layout.addWidget(self.overall_progress_bar)
        right_layout.addWidget(self.metrics_label)
        self.progress_bar = self.stage_progress_bar
        log_title = QLabel("Краткий журнал")
        log_title.setObjectName("sectionTitle")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(800)
        right_layout.addWidget(log_title)
        right_layout.addWidget(self.log, 1)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

    def reload_authors(self) -> None:
        selected = self.container.settings.get("selected_author_path", "")
        custom = self.container.settings.get("author_paths", [])
        folders = discover_author_folders(Path(self.container.settings.get("youtube_root", r"E:\YouTube")), custom)
        self.author_presets = merge_presets(self.container.settings.get("author_presets", []), folders)
        self.author_combo.blockSignals(True)
        self.author_combo.clear()
        for preset in self.author_presets:
            custom_marker = " • пользовательский" if preset.root_path in custom else ""
            self.author_combo.addItem(f"{preset.display_name}{custom_marker} — {preset.root_path}", preset.root_path)
        index = self.author_combo.findData(selected)
        self.author_combo.setCurrentIndex(index if index >= 0 else (0 if self.author_presets else -1))
        self.author_combo.blockSignals(False)
        self.update_project_path()

    def _author_changed(self) -> None:
        if self.metadata and not self._author_combo_programmatic:
            self._author_manual_override_video_id = self.metadata.video_id
            self.selected_project_path = None
            self.project_selection = "new"
            self.author_status_label.setText(f"Автор выбран вручную: {self._current_preset_name()}")
            self.preflight_label.setText("Назначение изменено — выполняется повторная проверка проекта")
            self.create_button.setEnabled(True)
            QTimer.singleShot(
                0,
                self._safe_ui_action(
                    "rediscover_after_author_override",
                    lambda video_id=self.metadata.video_id: self._start_project_discovery(video_id),
                ),
            )
        self.update_project_path()

    def _current_preset(self) -> Optional[AuthorPreset]:
        path = str(self.author_combo.currentData() or "").casefold()
        return next((item for item in self.author_presets if item.root_path.casefold() == path), None)

    def _current_preset_name(self) -> str:
        preset = self._current_preset()
        return preset.display_name if preset else "не выбран"

    def _project_roots(self) -> list[ProjectRoot]:
        return [ProjectRoot(Path(item.root_path), item.preset_id, item.display_name) for item in self.author_presets]

    def _url_changed(self, text: str) -> None:
        try:
            video_id = youtube_video_id(text)
            valid = True
        except Exception:
            video_id = ""
            valid = False
        if self.job_state in {UiJobState.PREPARING, UiJobState.RUNNING, UiJobState.CANCELLING}:
            current_id = self.metadata.video_id if self.metadata else ""
            if not valid or video_id == current_id:
                self.fetch_button.setEnabled(False)
                return
            answer = QMessageBox.question(
                self, "Переключиться на другое видео?",
                "Остановить текущую задачу и начать новый проект для вставленной ссылки?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer == QMessageBox.Yes:
                self._pending_url_after_cancel = text
                self._reset_after_cancel = True
                self.cancel_job()
            else:
                self.url_edit.blockSignals(True)
                self.url_edit.setText(self._active_project_url)
                self.url_edit.blockSignals(False)
            return
        current_id = self.metadata.video_id if self.metadata else ""
        if not video_id or video_id != current_id:
            self.metadata = None
            self.title_edit.clear()
            self.thumbnail_label.clear()
            self.thumbnail_label.setText("Превью появится после проверки ссылки")
            for value in self.info_labels.values():
                value.setText("—")
            self.create_button.setEnabled(False)
            self.dry_run_button.setEnabled(False)
            self.update_project_path()
        if self.metadata_request_in_progress and video_id != self._metadata_requested_video_id:
            if self.token:
                self.token.cancel()
            self.current_request_id = None
            self.metadata_request_in_progress = False
            self.token = None
            self.fetch_button.setText("Получить информацию")
            self.log.appendPlainText("URL изменён — запоздавший ответ предыдущего запроса будет проигнорирован.")
        self.fetch_button.setEnabled(valid and not self.metadata_request_in_progress)

    def _sync_options(self) -> None:
        busy = self.job_state in {
            UiJobState.PREPARING, UiJobState.RUNNING, UiJobState.CANCELLING
        }
        for checkbox in (
            self.max_check,
            self.proxy_check,
            self.audio_check,
            self.instrumental_check,
            self.reaper_check,
            self.vegas_check,
        ):
            checkbox.setEnabled(not busy)
        if self.project_selection == "resume" and self._resume_states:
            self._refresh_preflight_plan()

    def current_destination(self) -> Optional[Path]:
        raw = self.author_combo.currentData()
        return Path(str(raw)) if raw else None

    def add_path(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Выберите папку проектов", self.container.settings.get("youtube_root", r"E:\YouTube"))
        if not selected:
            return
        paths = self.container.settings.setdefault("author_paths", [])
        if selected not in paths:
            paths.append(selected)
        configured = self.container.settings.setdefault("author_presets", [])
        if not any(isinstance(item, dict) and str(item.get("root_path") or "").casefold() == selected.casefold() for item in configured):
            configured.append(AuthorPreset.from_dict({"root_path": selected}).to_dict())
        self.container.settings["selected_author_path"] = selected
        self.container.save_settings(self.container.settings)
        self.reload_authors()

    def remove_custom_path(self) -> None:
        raw = str(self.author_combo.currentData() or "")
        paths = self.container.settings.setdefault("author_paths", [])
        if raw not in paths:
            QMessageBox.information(self, "Пресеты", "Автоматически найденные папки нельзя удалить. Можно удалить только пользовательский пресет.")
            return
        paths.remove(raw)
        self.container.settings["author_presets"] = [
            item for item in self.container.settings.get("author_presets", [])
            if not isinstance(item, dict) or str(item.get("root_path") or "").casefold() != raw.casefold()
        ]
        self.container.save_settings(self.container.settings)
        self.reload_authors()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.container, self)
        if dialog.exec():
            self.reload_authors()

    def fetch_metadata(self) -> None:
        """Compatibility entry point; explicit UI actions use request_metadata()."""
        self.request_metadata("button")

    def request_metadata(self, source: str = "button", force: bool = False) -> None:
        if self.metadata_request_in_progress or (self.metadata_thread and self.metadata_thread.isRunning()):
            self.progress_label.setText("Запрос уже выполняется")
            return
        try:
            url = validate_youtube_url(self.url_edit.text())
            video_id = youtube_video_id(url)
        except Exception as exc:
            self.progress_label.setText(str(exc))
            self.fetch_button.setEnabled(False)
            return
        request_id = uuid.uuid4().hex
        generation_id = self._job_generation
        self.current_request_id = request_id
        self._metadata_generation_id = generation_id
        self._metadata_terminal = None
        self._metadata_requested_url = url
        self._metadata_requested_video_id = video_id
        self._metadata_request_started = time.monotonic()
        self.metadata_request_in_progress = True
        self.token = CancellationToken()
        self.fetch_button.setEnabled(False)
        self.fetch_button.setText("Получение информации…")
        self.cancel_button.setEnabled(True)
        self.create_button.setEnabled(False)
        self.dry_run_button.setEnabled(False)
        self.log.appendPlainText("Проверяю ссылку и получаю метаданные…")
        self.container.logger.info(
            "Metadata request started request_id=%s source=%s video_id=%s yt_dlp=%s cookies_mode=%s",
            request_id, source, video_id, self.container.paths.get("yt_dlp", ""), self.container.youtube_auth.summary,
        )
        update_context(
            generation_id=generation_id, video_id=video_id, metadata_worker="starting",
            last_ui_action=f"metadata:{source}",
        )
        token = self.token

        def work(progress):
            always_use = bool(self.container.settings.get("youtube_access", {}).get("always_use", False))
            outcome = self.container.metadata_controller.request(
                url,
                token,
                request_id,
                on_status=lambda message: progress(ProgressInfo("metadata", message)),
                force=force,
                manual_extra_attempt=source in ("manual_anonymous", "retry"),
                anonymous_first=source != "retry" and not always_use,
            )
            return {
                "request_id": request_id,
                "metadata": outcome.metadata,
                "outcome": outcome,
                "url": url,
                "video_id": video_id,
                "source": source,
                "generation_id": generation_id,
            }

        self._start_metadata_worker(work, request_id, generation_id)

    def _start_metadata_worker(self, function, request_id: str, generation_id: int) -> None:
        thread = QThread(self)
        worker = FunctionWorker(function)
        thread.setObjectName(f"metadata-thread-{request_id[:8]}")
        worker.setObjectName(f"metadata-worker-{request_id[:8]}")
        predicate = lambda: (
            request_id == self.current_request_id
            and generation_id == self._job_generation
        )
        bridge = UiWorkerBridge(
            {
                "finished": lambda payload: self._queue_metadata_terminal("finished", payload),
                "failed": lambda message, details: self._queue_metadata_terminal("failed", message, details),
                "cancelled": lambda: self._queue_metadata_terminal("cancelled"),
                "authentication_required": lambda details: self._queue_metadata_terminal("authentication_required", details),
                "cookies_unavailable": lambda details: self._queue_metadata_terminal("cookies_unavailable", details),
                "progress": lambda info: self._metadata_attempt_progress(info, request_id, generation_id),
                "thread_finished": lambda: self._metadata_thread_finished(thread, worker, bridge, request_id, generation_id),
            },
            predicate=predicate,
            on_error=self._ui_action_failed,
            parent=self,
        )
        thread.worker = worker
        thread.bridge = bridge
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(bridge.finished)
        worker.failed.connect(bridge.failed)
        worker.cancelled.connect(bridge.cancelled)
        worker.authentication_required.connect(bridge.authentication_required)
        worker.cookies_unavailable.connect(bridge.cookies_unavailable)
        worker.progress.connect(bridge.progress)
        for signal in (
            worker.finished, worker.failed, worker.cancelled, worker.authentication_required,
            worker.cookies_unavailable, worker.manual_action_required, worker.runtime_install_required,
            worker.waiting_for_disk_space, worker.gpu_memory_required,
            worker.system_memory_required, worker.audio_output_missing,
        ):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(bridge.thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        self.metadata_thread = thread
        self.metadata_worker = worker
        update_context(metadata_worker=f"running:{worker.objectName()}")
        crash_event(f"Metadata QThread starting request_id={request_id} generation={generation_id}")
        thread.start()

    def _queue_metadata_terminal(self, kind: str, *args) -> None:
        self._metadata_terminal = (kind, args)
        crash_event(f"Metadata worker terminal signal queued: {kind}")

    def _metadata_thread_finished(
        self, thread: QThread, worker: FunctionWorker, bridge: UiWorkerBridge,
        request_id: str, generation_id: int,
    ) -> None:
        crash_event(f"Metadata QThread finished request_id={request_id} generation={generation_id}")
        if thread in self._threads:
            self._threads.remove(thread)
        if self.metadata_thread is thread:
            self.metadata_thread = None
            self.metadata_worker = None
        update_context(metadata_worker="absent")
        terminal = self._metadata_terminal
        self._metadata_terminal = None
        bridge.deleteLater()
        if request_id != self.current_request_id or generation_id != self._job_generation:
            return
        if not terminal:
            self._metadata_failed(request_id, "Поток метаданных завершился без результата.", "Terminal signal is missing")
            return
        kind, args = terminal
        callbacks = {
            "finished": lambda: self._metadata_worker_finished(args[0]),
            "failed": lambda: self._metadata_failed(request_id, args[0], args[1]),
            "cancelled": lambda: self._metadata_cancelled(request_id),
            "authentication_required": lambda: self._authentication_required(request_id, args[0]),
            "cookies_unavailable": lambda: self._cookies_unavailable(request_id, args[0]),
        }
        callback = callbacks.get(kind)
        if callback:
            self._safe_ui_action(f"metadata_terminal:{kind}", callback)()

    def _metadata_worker_finished(self, payload) -> None:
        request_id = str(payload.get("request_id", ""))
        if request_id != self.current_request_id:
            self._finish_metadata_request(request_id, "stale")
            return
        metadata = payload["metadata"]
        url = str(payload["url"])
        outcome = payload.get("outcome")
        if outcome and outcome.from_cache:
            self.log.appendPlainText(f"Метаданные {metadata.video_id} взяты из 10-минутного кэша; yt-dlp не запускался.")
        elif outcome:
            self.log.appendPlainText(f"Metadata subprocess: {outcome.attempts}; результат: {', '.join(item.value for item in outcome.categories)}")
        self._finish_metadata_request(request_id, "success")
        self._apply_metadata(metadata, url)

    def _finish_metadata_request(self, request_id: str, result: str) -> None:
        elapsed = max(0.0, time.monotonic() - self._metadata_request_started)
        self.container.logger.info(
            "Metadata request finished request_id=%s duration=%.3f result=%s", request_id, elapsed, result
        )
        if request_id != self.current_request_id:
            return
        self.token = None
        self.metadata_request_in_progress = False
        if request_id == self.current_request_id:
            self.current_request_id = None
        self.fetch_button.setText("Получить информацию")
        self.cancel_button.setEnabled(False)
        try:
            valid = bool(youtube_video_id(self.url_edit.text()))
        except Exception:
            valid = False
        self.fetch_button.setEnabled(valid)

    def _metadata_failed(self, request_id: str, message: str, details: str) -> None:
        if request_id != self.current_request_id:
            self._finish_metadata_request(request_id, "stale_failed")
            return
        self._finish_metadata_request(request_id, "failed")
        self.log.appendPlainText("Ошибка получения информации: " + message)
        ErrorDialog(message, details, self).exec()

    def _metadata_cancelled(self, request_id: str) -> None:
        self._finish_metadata_request(request_id, "cancelled")
        self.progress_label.setText("Получение информации отменено")

    def _metadata_attempt_progress(self, info: ProgressInfo, request_id: str = "", generation_id: Optional[int] = None) -> None:
        if request_id and request_id != self.current_request_id:
            return
        if generation_id is not None and generation_id != self._job_generation:
            return
        self.progress_label.setText(info.message)
        self.log.appendPlainText(info.message)

    def _authentication_required(self, request_id: str, details) -> None:
        if request_id != self.current_request_id:
            self._finish_metadata_request(request_id, "stale_authentication_required")
            return
        video_id = details.video_id
        self._finish_metadata_request(request_id, "authentication_required")
        self.progress_label.setText("⏸ Получение информации приостановлено — требуется браузерная сессия YouTube")
        if video_id in self._auth_retried_video_ids:
            QMessageBox.warning(self, "YouTube запросил подтверждение", details.reason + "\n\nАвтоматический повтор уже выполнялся.")
            return
        access = self.container.settings.get("youtube_access", {})
        dialog = YouTubeAuthDialog(str(access.get("browser", "")) or "firefox", str(access.get("browser_profile", "")), self)
        if not dialog.exec():
            return
        if dialog.action == YouTubeAuthDialog.SETTINGS:
            self.open_settings()
            return
        if dialog.action == YouTubeAuthDialog.ANONYMOUS:
            if video_id in self._manual_anonymous_retried_video_ids:
                QMessageBox.information(self, "YouTube", "Дополнительная анонимная попытка уже выполнялась.")
                return
            self._manual_anonymous_retried_video_ids.add(video_id)
            self.request_metadata("manual_anonymous", force=True)
            return
        self.container.enable_youtube_auth("browser", dialog.selected_browser, dialog.selected_profile)
        try:
            self.container.youtube_auth.arguments(validate=True)
        except Exception as exc:
            QMessageBox.warning(self, "Доступ к YouTube", str(exc))
            return
        self._auth_retried_video_ids.add(video_id)
        self.log.appendPlainText(f"Повторяю только metadata-запрос через {self.container.youtube_auth.summary}.")
        self.request_metadata("retry", force=True)

    def _cookies_unavailable(self, request_id: str, details) -> None:
        if request_id != self.current_request_id:
            self._finish_metadata_request(request_id, "stale_cookies_unavailable")
            return
        self._finish_metadata_request(request_id, "cookies_unavailable")
        box = QMessageBox(self)
        box.setWindowTitle("Cookies браузера недоступны")
        box.setText(details.reason)
        retry = box.addButton("Повторить", QMessageBox.AcceptRole)
        settings = box.addButton("Открыть настройки", QMessageBox.ActionRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == retry:
            self.request_metadata("retry", force=True)
        elif box.clickedButton() == settings:
            self.open_settings()

    def _project_authentication_required(self, details) -> None:
        self.token = None
        self._set_job_state(UiJobState.ABANDONED)
        self._set_busy(False)
        self.progress_label.setText("⏸ YouTube требует подтверждение браузерной сессии")
        dialog = YouTubeAuthDialog(getattr(details, "browser", "") or "firefox", "", self)
        if dialog.exec() and dialog.action == YouTubeAuthDialog.USE:
            self.container.enable_youtube_auth("browser", dialog.selected_browser, dialog.selected_profile)
            try:
                self.container.youtube_auth.arguments(validate=True)
            except Exception as exc:
                QMessageBox.warning(self, "Доступ к YouTube", str(exc))
                return
            QTimer.singleShot(0, self._safe_ui_action("retry_project", self.create_project))
        elif dialog.action == YouTubeAuthDialog.SETTINGS:
            self.open_settings()

    def _project_cookies_unavailable(self, details) -> None:
        self.token = None
        self._set_job_state(UiJobState.ABANDONED)
        self._set_busy(False)
        box = QMessageBox(self)
        box.setWindowTitle("Cookies браузера недоступны")
        box.setText(details.reason)
        anonymous = box.addButton("Продолжить анонимно", QMessageBox.AcceptRole)
        settings = box.addButton("Выбрать Firefox или cookies.txt", QMessageBox.ActionRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == anonymous:
            self.container.youtube_auth.disable_for_anonymous_job()
            if self.metadata:
                self.container.projects.clear_job_auth(self.metadata.video_id)
            QTimer.singleShot(0, self._safe_ui_action("retry_project", self.create_project))
        elif box.clickedButton() == settings:
            self.open_settings()

    def _media_forbidden(self, details) -> None:
        self.token = None
        self._set_job_state(UiJobState.ABANDONED)
        self._set_busy(False)
        part_text = f"\n\nЧастичный файл сохранён:\n{details.part_path}" if details.part_path else ""
        box = QMessageBox(self)
        box.setWindowTitle("YouTube прервал загрузку")
        box.setText(str(details) + part_text)
        resume = box.addButton("Продолжить загрузку", QMessageBox.AcceptRole)
        browser = box.addButton("Использовать браузерную сессию", QMessageBox.ActionRole)
        folder = box.addButton("Открыть папку", QMessageBox.ActionRole)
        technical = box.addButton("Технические подробности", QMessageBox.ActionRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == resume:
            QTimer.singleShot(0, self._safe_ui_action("retry_project", self.create_project))
        elif box.clickedButton() == browser:
            self._project_authentication_required(details)
        elif box.clickedButton() == folder and details.part_path:
            try:
                os.startfile(str(Path(details.part_path).parent))
            except OSError as exc:
                QMessageBox.warning(self, "Папка", str(exc))
        elif box.clickedButton() == technical:
            ErrorDialog(str(details), details.stderr, self).exec()

    def _apply_metadata(self, metadata: VideoMetadata, requested_url: str) -> None:
        try:
            if youtube_video_id(self.url_edit.text()) != metadata.video_id:
                return
        except Exception:
            return
        previous_video_id = self.metadata.video_id if self.metadata else ""
        if previous_video_id != metadata.video_id:
            self._author_manual_override_video_id = ""
        self.metadata = metadata
        update_context(video_id=metadata.video_id, last_ui_action="metadata_applied")
        self.selected_project_path = None
        self.project_selection = "new"
        self._active_project_url = requested_url
        self.title_edit.setText(metadata.title)
        self.create_button.setEnabled(True)
        self.dry_run_button.setEnabled(True)
        self._resolve_author_for_metadata(metadata)
        self.update_project_path()
        plan = build_format_plan(metadata.formats, self._proxy_height())
        self.info_labels["resolution"].setText(f"{plan.maximum_video.height or '?'}p")
        self.info_labels["fps"].setText(f"{plan.maximum_video.fps or '?'}")
        seconds = int(metadata.duration or 0)
        self.info_labels["duration"].setText(f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}")
        maximum_size = sum(value for value in (plan.maximum_video.size, plan.maximum_audio.size) if value)
        proxy_size = sum(value for value in (plan.proxy_video.size, plan.proxy_audio.size) if value)
        self.info_labels["max_size"].setText(f"≈ {maximum_size / 1024**3:.1f} ГБ" if maximum_size else "неизвестно")
        self.info_labels["proxy_size"].setText(f"≈ {proxy_size / 1024**3:.1f} ГБ" if proxy_size else "неизвестно")
        suffix = ""
        if self.container.youtube_auth.enabled:
            suffix = f" — подтверждено через {self.container.youtube_auth.summary}"
        self.log.appendPlainText(f"Найдено: {metadata.title}{suffix}")
        if metadata.best_thumbnail:
            reply = self.network.get(QNetworkRequest(QUrl(metadata.best_thumbnail.url)))
            reply.finished.connect(lambda r=reply, video_id=metadata.video_id: self._thumbnail_ready(r, video_id))
        self.preflight_label.setText("Идёт поиск проекта по всем настроенным папкам…")
        QTimer.singleShot(
            0,
            self._safe_ui_action(
                "discover_existing_project",
                lambda video_id=metadata.video_id: self._start_project_discovery(video_id),
            ),
        )

    def _resolve_author_for_metadata(self, metadata: VideoMetadata) -> None:
        if self._author_manual_override_video_id == metadata.video_id:
            self.author_status_label.setText(f"Автор выбран вручную: {self._current_preset_name()}")
            return
        has_identity = any((metadata.channel_id, metadata.channel_handle, metadata.uploader_id, metadata.channel, metadata.uploader))
        if not has_identity or not self.author_presets:
            self.author_status_label.setText(f"Автор: {metadata.channel or metadata.uploader or 'не определён'}")
            return
        resolution = AuthorPresetResolver().resolve(metadata, self.author_presets)
        self._author_resolution_kind = resolution.kind
        selected = resolution.preset
        if resolution.kind == AuthorMatchKind.MULTIPLE_MATCHES:
            selected = self._choose_preset_dialog(
                list(resolution.matches),
                "Несколько пресетов подходят этому каналу. Выберите автора:",
            )
        elif resolution.kind == AuthorMatchKind.NO_MATCH:
            selected = self._unknown_author_choice(metadata, resolution.suggestion)
        if selected:
            self._select_preset(selected)
            mode = "точное совпадение" if resolution.kind == AuthorMatchKind.EXACT_MATCH else (
                "совпадение по псевдониму" if resolution.kind == AuthorMatchKind.ALIAS_MATCH else "выбрано пользователем"
            )
            self.author_status_label.setText(f"Автор: {selected.display_name} — {mode}")
        else:
            if resolution.kind == AuthorMatchKind.MULTIPLE_MATCHES:
                self.author_status_label.setText("Автор не выбран: найдено несколько точных совпадений")
            else:
                self.author_status_label.setText(
                    f"Канал не привязан: {metadata.channel or metadata.uploader or metadata.channel_id}"
                )
            self.create_button.setEnabled(False)
            self.dry_run_button.setEnabled(False)

    def _select_preset(self, preset: AuthorPreset) -> None:
        index = self.author_combo.findData(preset.root_path)
        if index < 0:
            return
        self._author_combo_programmatic = True
        try:
            self.author_combo.setCurrentIndex(index)
        finally:
            self._author_combo_programmatic = False

    def _choose_preset_dialog(self, presets: list[AuthorPreset], prompt: str) -> Optional[AuthorPreset]:
        if not presets:
            return None
        labels = [f"{item.display_name} — {item.root_path}" for item in presets]
        selected, accepted = QInputDialog.getItem(self, "Выбор автора", prompt, labels, 0, False)
        if not accepted:
            return None
        try:
            return presets[labels.index(selected)]
        except ValueError:
            return None

    def _unknown_author_choice(self, metadata: VideoMetadata, suggestion: Optional[AuthorPreset]) -> Optional[AuthorPreset]:
        current = self._current_preset()
        if not current:
            return self._choose_preset_dialog(self.author_presets, "Канал не найден в пресетах. Выберите автора:")
        channel_name = metadata.channel or metadata.uploader or metadata.channel_id
        box = QMessageBox(self)
        box.setWindowTitle("Неизвестный автор YouTube")
        hint = f"\n\nВозможная подсказка: {suggestion.display_name}" if suggestion else ""
        box.setText(f"Канал «{channel_name}» не привязан ни к одному пресету.{hint}\n\nКак продолжить?")
        current_button = box.addButton(f"Использовать {current.display_name} один раз", QMessageBox.AcceptRole)
        choose_button = box.addButton("Выбрать другой пресет", QMessageBox.ActionRole)
        bind_button = box.addButton(f"Привязать канал к {current.display_name}", QMessageBox.ActionRole)
        box.addButton("Отмена", QMessageBox.RejectRole)
        box.setDefaultButton(current_button)
        box.exec()
        if box.clickedButton() == bind_button:
            self._bind_channel_to_preset(metadata, current)
            return current
        if box.clickedButton() == choose_button:
            chosen = self._choose_preset_dialog(self.author_presets, "Выберите пресет для этого видео:")
            if chosen and self.container.settings.get("suggest_remember_author", True):
                remember = QMessageBox.question(
                    self,
                    "Запомнить автора?",
                    f"Привязать канал «{channel_name}» к пресету {chosen.display_name}?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if remember == QMessageBox.Yes:
                    self._bind_channel_to_preset(metadata, chosen)
            return chosen
        return current if box.clickedButton() == current_button else None

    def _bind_channel_to_preset(self, metadata: VideoMetadata, preset: AuthorPreset) -> None:
        presets = self.container.settings.setdefault("author_presets", [])
        target = next((item for item in presets if isinstance(item, dict) and str(item.get("preset_id")) == preset.preset_id), None)
        if target is None:
            target = preset.to_dict()
            presets.append(target)
        if metadata.channel_id:
            values = target.setdefault("youtube_channel_ids", [])
            if metadata.channel_id not in values:
                values.append(metadata.channel_id)
        handle = metadata.channel_handle or metadata.uploader_id
        if handle:
            values = target.setdefault("youtube_handles", [])
            if handle not in values:
                values.append(handle)
        self.container.settings_store.save(self.container.settings)
        projects = getattr(self.container, "projects", None)
        if projects is not None:
            projects.project_roots = presets
        self.reload_authors()
        rebound = next((item for item in self.author_presets if item.preset_id == preset.preset_id), preset)
        self._select_preset(rebound)

    def _thumbnail_ready(self, reply: QNetworkReply, video_id: str = "") -> None:
        data = reply.readAll()
        pixmap = QPixmap()
        if self.metadata and self.metadata.video_id == video_id and pixmap.loadFromData(data):
            self.thumbnail_label.setPixmap(
                pixmap.scaled(self.thumbnail_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        reply.deleteLater()

    def update_project_path(self) -> None:
        destination = self.current_destination()
        if destination:
            if self.container.settings.get("selected_author_path") != str(destination):
                self.container.settings["selected_author_path"] = str(destination)
                self.container.settings_store.save(self.container.settings)
            if self.metadata:
                path = self.selected_project_path or self.container.projects.planned_path(destination, self.metadata)
                self.path_edit.setText(str(path))
            else:
                self.path_edit.setText(str(destination / "<Название видео>"))

    def _start_project_discovery(self, video_id: str) -> None:
        if not self.metadata or self.metadata.video_id != video_id:
            return
        if self._discovery_thread and self._discovery_thread.isRunning():
            if self._discovery_video_id != video_id:
                self._pending_discovery_video_id = video_id
            return
        projects = getattr(self.container, "projects", None)
        finder = getattr(projects, "find_existing_projects_across", None)
        if not callable(finder):
            self._detect_existing_project(video_id)
            return
        self._discovery_generation += 1
        generation = self._discovery_generation
        roots = self._project_roots()
        metadata = self.metadata
        self._discovery_video_id = video_id
        thread = QThread(self)
        worker = FunctionWorker(lambda _progress: finder(roots, metadata, refresh_index=True))
        terminal: dict[str, object] = {}
        thread.setObjectName(f"project-discovery-{video_id}")
        worker.setObjectName(f"project-discovery-worker-{video_id}")

        def cleanup() -> None:
            if thread in self._threads:
                self._threads.remove(thread)
            if self._discovery_thread is thread:
                self._discovery_thread = None
                self._discovery_video_id = ""
            bridge.deleteLater()
            pending = self._pending_discovery_video_id
            self._pending_discovery_video_id = ""
            if pending and self.metadata and self.metadata.video_id == pending:
                QTimer.singleShot(0, lambda: self._start_project_discovery(pending))
            if generation != self._discovery_generation or not self.metadata or self.metadata.video_id != video_id:
                return
            if "result" in terminal:
                self._detect_existing_project(video_id, candidates=terminal["result"])
            elif "error" in terminal:
                message, _details = terminal["error"]
                self.preflight_label.setText(f"Поиск проекта не завершён: {message}")

        bridge = UiWorkerBridge(
            {
                "finished": lambda result: terminal.update(result=result),
                "failed": lambda message, details: terminal.update(error=(message, details)),
                "thread_finished": cleanup,
            },
            predicate=lambda: generation == self._discovery_generation,
            on_error=self._ui_action_failed,
            parent=self,
        )
        thread.worker = worker
        thread.bridge = bridge
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(bridge.finished)
        worker.failed.connect(bridge.failed)
        for signal in (worker.finished, worker.failed):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(bridge.thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        self._discovery_thread = thread
        thread.start()

    def _detect_existing_project(self, video_id: str, candidates=None) -> None:
        if not self.metadata or self.metadata.video_id != video_id:
            return
        destination = self.current_destination()
        projects = getattr(self.container, "projects", None)
        finder = getattr(projects, "find_existing_projects_across", None)
        fallback_finder = getattr(projects, "find_existing_projects", None)
        if not destination:
            return
        if candidates is None:
            if callable(finder):
                candidates = finder(self._project_roots(), self.metadata, refresh_index=False)
            elif callable(fallback_finder):
                candidates = fallback_finder(destination, self.metadata)
            else:
                return
        if not candidates:
            self.selected_project_path = self.container.projects.planned_path(destination, self.metadata)
            self.project_selection = "new"
            self.preflight_label.setText(
                f"Автор: {self._current_preset_name()}\nСуществующий проект: нет\nБудет создано: {self.path_edit.text()}"
            )
            self.update_project_path()
            return
        candidate = candidates[0]
        path = Path(candidate["path"])
        found_preset = str(candidate.get("preset_name") or path.parent.parent.name)
        self.preflight_label.setText(
            f"Автор проекта: {found_preset}\nСуществующий проект: да\n"
            "Проверка существующего проекта…"
        )
        self.log.appendPlainText(
            f"Найден существующий проект: {path}. Быстрый Preflight запущен автоматически."
        )
        self._start_project_migration(path)
        return

    def _start_project_migration(self, path: Path) -> None:
        if not self.metadata or self.active_thread or self.metadata_thread:
            return
        self._job_generation += 1
        self.active_job_id = "migration-" + uuid.uuid4().hex
        migration_proxy_height = self._proxy_height()
        self.active_job_proxy_height = migration_proxy_height
        self.token = CancellationToken()
        self.selected_project_path = path
        self.project_selection = "migrating"
        self.create_button.setEnabled(False)
        self.dry_run_button.setEnabled(False)
        self.progress_label.setText("Подключение существующего проекта…")
        self._set_busy(True)
        self._set_job_state(UiJobState.PREPARING)
        metadata = self.metadata
        token = self.token
        author_preset = path.parent.parent.name
        matching_preset = next((item for item in self.author_presets if Path(item.root_path) == path.parent), None)
        preset_id = matching_preset.preset_id if matching_preset else ""
        if matching_preset:
            author_preset = matching_preset.display_name
        migration_job_id = self.active_job_id or ""
        migration_options = self.options()
        migration_options.reaper_proxy_height = migration_proxy_height

        def work(progress):
            result = self.container.projects.migrate_existing(
                metadata,
                path,
                job_id=migration_job_id,
                author_preset=author_preset,
                cancellation=token,
                on_progress=progress,
                reaper_proxy_height=migration_proxy_height,
                preset_id=preset_id,
            )
            try:
                result["states"] = self.container.projects.reconcile_existing(
                    metadata, migration_options, path, token
                )
            except JobCancelledError:
                raise
            except Exception as exc:
                result["states"] = {}
                result["inspect_error"] = str(exc)
            return result

        self._start_worker(work, self._migration_ready, self._migration_failed, self._migration_progress)

    def _migration_progress(self, info: ProgressInfo) -> None:
        self.progress_label.setText("Подключение существующего проекта…\n" + info.message)
        self.stage_progress_bar.setRange(0, 100)
        self.stage_progress_bar.setValue(int(info.percent or 0))
        self.log.appendPlainText("Миграция: " + info.message)

    def _migration_ready(self, result) -> None:
        self.token = None
        self._set_busy(False)
        self._set_job_state(UiJobState.IDLE)
        self.project_selection = "resume"
        self.selected_project_path = Path(result["project_path"])
        self.current_project_path = self.selected_project_path
        discovered = result.get("discovered_files", [])
        scan_stats = result.get("scan_stats", {})
        self._resume_states = result.get("states", {})
        rpp_state = self._resume_states.get("reaper", {})
        rpp_value = rpp_state.get("path") if rpp_state.get("status") in {"VALID", "INVALID"} else None
        self.current_rpp_path = Path(rpp_value) if rpp_value else None
        vegas_state = self._resume_states.get("vegas", {})
        vegas_value = vegas_state.get("path") if vegas_state.get("status") == "VALID" else None
        self.current_veg_path = Path(vegas_value) if vegas_value else None
        self._sync_project_actions()
        self.progress_label.setText(
            "Существующий проект подключён. "
            f"Разрешённых файлов: {scan_stats.get('allowed_files', len(discovered))}; "
            f"FFprobe: {scan_stats.get('ffprobe_calls', 0)}; "
            f"sidecar проигнорировано: {scan_stats.get('ignored_sidecar_files', 0)}."
        )
        self.dry_run_button.setEnabled(True)
        self._refresh_preflight_plan()
        self.update_project_path()

    def _refresh_preflight_plan(self) -> None:
        """Recalculate the plan from cached Preflight states; never scans or starts a Job."""
        if not self.metadata or not self.selected_project_path or not self._resume_states:
            return
        options = self.options()
        projects = self.container.projects
        selected = projects.selected_output_roles(options)
        required = projects.required_roles(options, self._resume_states)
        self.plan_status = PlanStatus(projects.plan_status(options, self._resume_states))
        labels = {
            "maximum": "MAX-видео",
            "proxy": f"Видео {options.reaper_proxy_height}p для REAPER",
            "audio": "Оригинальное аудио",
            "instrumental": "Instrumental",
            "reaper": "Проект REAPER",
            "vegas": "Проект VEGAS",
        }
        ready = [labels[key] for key in required if self._resume_states.get(key, {}).get("status") == "VALID"]
        unresolved = [
            labels[key] for key in required
            if self._resume_states.get(key, {}).get("status") in {"AMBIGUOUS", "CONFIRMATION_REQUIRED"}
        ]
        missing = [
            labels[key] for key in required
            if self._resume_states.get(key, {}).get("status") not in {
                "VALID", "AMBIGUOUS", "CONFIRMATION_REQUIRED"
            }
        ]
        not_required = [labels[key] for key in labels if key not in required]
        planned = [
            labels[key] for key in selected
            if self._resume_states.get(key, {}).get("status") != "VALID"
        ]
        proxy_info = self._resume_states.get("proxy_variants", {})
        variants = proxy_info.get("variants", {}) if isinstance(proxy_info, dict) else {}
        found_proxy_heights = sorted(int(key) for key in variants if str(key).isdigit())
        effective_height = int(proxy_info.get("effective_height") or options.reaper_proxy_height) if isinstance(proxy_info, dict) else options.reaper_proxy_height
        proxy_summary = (
            f"\nВыбранный proxy: {options.reaper_proxy_height}p"
            + (f" → {effective_height}p без увеличения" if effective_height != options.reaper_proxy_height else "")
            + f"\nНайденные варианты proxy: {', '.join(f'{height}p' for height in found_proxy_heights) or 'нет'}"
        )
        project_preset = next(
            (
                item.display_name for item in self.author_presets
                if Path(item.root_path) == self.selected_project_path.parent
            ),
            self.selected_project_path.parent.parent.name,
        )
        if self.plan_status == PlanStatus.ALREADY_COMPLETE:
            status_text = "Все выбранные элементы уже готовы."
            self.create_button.setText("Проект уже готов")
            self.create_button.setEnabled(False)
            self.progress_label.setText("Проект готов")
            self.stage_progress_bar.setRange(0, 100)
            self.stage_progress_bar.setValue(100)
            self.stage_progress_caption.setText("Текущий этап: 100%")
            self.overall_progress_bar.setValue(100)
            self.overall_progress_caption.setText("Общий прогресс проекта: 100%")
        elif not selected:
            status_text = "Выберите хотя бы один результат."
            self.create_button.setText("Выберите элементы")
            self.create_button.setEnabled(False)
            self.progress_label.setText("Ожидание выбора")
        else:
            status_text = "Требуется создать отсутствующие элементы."
            self.create_button.setText("Создать недостающие элементы")
            self.create_button.setEnabled(True)
            self.progress_label.setText("План готов к подтверждению")
            self.stage_progress_bar.setRange(0, 100)
            self.stage_progress_bar.setValue(0)
            self.stage_progress_caption.setText("Текущий этап: 0%")
            self.overall_progress_bar.setValue(20)
            self.overall_progress_caption.setText("Общий прогресс проекта: 20%")
        self.preflight_label.setText(
            f"Автор: {project_preset}\nСуществующий проект: да\n"
            f"Готово: {', '.join(ready) or 'нет'}\n"
            f"Требует выбора: {', '.join(unresolved) or 'нет'}\n"
            f"Отсутствует: {', '.join(missing) or 'нет'}\n"
            f"Не требуется: {', '.join(not_required) or 'нет'}\n"
            f"Запланировано: {', '.join(planned) or 'ничего'}\n"
            f"Статус: {status_text}{proxy_summary}"
        )
        self._render_preflight_stages(required)
        self.new_project_button.setEnabled(True)
        self._sync_project_actions()

    def _render_preflight_stages(self, required: set[str]) -> None:
        role_stages = {
            "maximum": JobStage.DOWNLOAD_MAXIMUM,
            "proxy": JobStage.CREATE_PROXY,
            "audio": JobStage.DOWNLOAD_AUDIO,
            "instrumental": JobStage.SEPARATE_STEMS,
            "reaper": JobStage.CREATE_REAPER,
            "vegas": JobStage.CREATE_VEGAS,
        }
        stage_roles = {stage: role for role, stage in role_stages.items()}
        complete = self.plan_status == PlanStatus.ALREADY_COMPLETE
        common_complete = {
            JobStage.VALIDATE_URL,
            JobStage.FETCH_METADATA,
            JobStage.CHECK_DEPENDENCIES,
            JobStage.CHECK_DISK_SPACE,
            JobStage.FINAL_VALIDATION,
            JobStage.DONE,
        }
        for index, stage in enumerate(ORDERED_STAGES):
            role = stage_roles.get(stage)
            if role and role in required:
                status = self._resume_states.get(role, {}).get("status")
                if status == "VALID":
                    text = "✓  " + self._stage_name(stage) + " — готово ранее"
                elif status in {"AMBIGUOUS", "CONFIRMATION_REQUIRED"}:
                    text = "?  " + self._stage_name(stage) + " — требуется выбор"
                else:
                    text = "○  " + self._stage_name(stage) + " — будет создано"
            elif stage in {JobStage.VALIDATE_URL, JobStage.FETCH_METADATA} or (complete and stage in common_complete):
                text = "✓  " + self._stage_name(stage)
            elif role:
                text = "—  " + self._stage_name(stage) + " — не требуется"
            elif complete:
                text = "—  " + self._stage_name(stage) + " — не требуется"
            else:
                text = "○  " + self._stage_name(stage)
            self.stage_list.item(index).setText(text)

    def _migration_failed(self, message: str, details: str) -> None:
        self.token = None
        self._set_busy(False)
        self._set_job_state(UiJobState.FAILED)
        self.project_selection = "cancel"
        self.create_button.setEnabled(False)
        self.progress_label.setText("Не удалось подключить существующий проект. Пользовательские файлы не изменены.")
        ErrorDialog(message, details, self, folder=self.selected_project_path).exec()

    def _set_job_state(self, state: UiJobState) -> None:
        self.job_state = state
        update_context(
            job_state=state.value,
            job_id=self.active_job_id or "",
            generation_id=self._job_generation,
            video_id=self.metadata.video_id if self.metadata else "",
            project_path=str(self.selected_project_path or ""),
        )
        crash_event(f"UI job state changed: {state.value}")
        busy = state in {UiJobState.PREPARING, UiJobState.RUNNING, UiJobState.CANCELLING}
        if not busy:
            self.active_job_proxy_height = None
        self.new_project_button.setEnabled(state != UiJobState.IDLE)
        self.new_project_button.setText("Остановить и начать новый" if busy else "Новый проект")
        self._sync_project_actions()
        self.refresh_proxy_quality_ui()

    def _proxy_height(self) -> int:
        value = int(self.container.settings.get("reaper_proxy_height", 720))
        return value if value in {480, 720, 1080} else 720

    def _display_proxy_height(self) -> int:
        if self.job_state in {UiJobState.PREPARING, UiJobState.RUNNING, UiJobState.CANCELLING}:
            return self.active_job_proxy_height or self._proxy_height()
        return self._proxy_height()

    def _stage_name(self, stage: JobStage) -> str:
        return stage_display_name(stage, self._display_proxy_height())

    @Slot(object)
    def apply_settings(self, updated_settings) -> None:
        """Apply saved settings to the open tab without starting any work."""
        height = int(updated_settings.get("reaper_proxy_height", 720))
        if height not in {480, 720, 1080}:
            height = 720
        if self.job_state in {UiJobState.PREPARING, UiJobState.RUNNING, UiJobState.CANCELLING}:
            self.progress_label.setText(
                f"Качество {height}p будет применено к следующему проекту"
            )
        else:
            self.refresh_proxy_quality_ui(height)
            if self.project_selection == "resume" and self._resume_states:
                self._apply_cached_proxy_profile(height)
        settings_service = getattr(self.container, "settings_service", None)
        if settings_service is not None:
            settings_service.mark_ui_refreshed()

    def refresh_proxy_quality_ui(self, proxy_height: Optional[int] = None) -> None:
        """Refresh every proxy-dependent UI element; never starts I/O or a worker."""
        height = proxy_height or self._display_proxy_height()
        height = height if height in {480, 720, 1080} else 720
        self.subtitle.setText(f"YouTube → SDR-видео, {height}p-прокси, аудио, инструментал и REAPER")
        self.proxy_check.setText(f"Видео {height}p для REAPER")
        self.proxy_check.setToolTip(f"Скачать или создать видео {height}p; ожидаемое имя содержит [{height}p].")
        self.info_captions["proxy_size"].setText(f"Видео {height}p")
        if hasattr(self, "stage_list"):
            proxy_index = ORDERED_STAGES.index(JobStage.CREATE_PROXY)
            item = self.stage_list.item(proxy_index)
            current = item.text()
            replacement = stage_display_name(JobStage.CREATE_PROXY, height)
            if re.search(r"Создание видео (?:480|720|1080)p", current):
                item.setText(re.sub(r"Создание видео (?:480|720|1080)p", replacement, current))
            else:
                item.setText("○  " + replacement)
        if self.metadata and self.metadata.formats:
            try:
                plan = build_format_plan(self.metadata.formats, height)
            except Exception:
                self.info_labels["proxy_size"].setText("неизвестно")
            else:
                proxy_size = sum(value for value in (plan.proxy_video.size, plan.proxy_audio.size) if value)
                self.info_labels["proxy_size"].setText(
                    f"≈ {proxy_size / 1024**3:.1f} ГБ" if proxy_size else "неизвестно"
                )
        else:
            self.info_labels["proxy_size"].setText("—")
        self.update()
        if hasattr(self, "stage_list"):
            self.stage_list.update()

    def _apply_cached_proxy_profile(self, requested_height: int) -> None:
        """Switch the selected proxy profile using cached Preflight data only."""
        if not self.metadata:
            return
        plan = build_format_plan(self.metadata.formats, requested_height)
        source_height = int(plan.maximum_video.height or requested_height)
        effective_height = min(requested_height, source_height)
        variants_state = self._resume_states.get("proxy_variants", {})
        variants = variants_state.get("variants", {}) if isinstance(variants_state, dict) else {}
        selected = variants.get(str(effective_height), {}) if isinstance(variants, dict) else {}
        if isinstance(selected, dict) and selected.get("status") == "VALID" and Path(str(selected.get("path"))).is_file():
            self._resume_states["proxy"] = {
                **selected,
                "status": "VALID",
                "requested_height": requested_height,
                "effective_height": effective_height,
            }
        else:
            self._resume_states["proxy"] = {
                "status": "MISSING",
                "requested_height": requested_height,
                "effective_height": effective_height,
                "other_variants": sorted(int(key) for key in variants if str(key).isdigit()),
            }
        variants_state.update({
            "requested_height": requested_height,
            "effective_height": effective_height,
        })
        self._resume_states["proxy_variants"] = variants_state
        if "reaper" in self.container.projects.required_roles(self.options(), self._resume_states):
            rpp = self.current_rpp_path
            proxy_path = self._resume_states["proxy"].get("path")
            instrumental_path = self._resume_states.get("instrumental", {}).get("path")
            if rpp and proxy_path and instrumental_path and self.container.projects.reaper.validate_project(
                rpp, Path(str(proxy_path)), Path(str(instrumental_path))
            ):
                self._resume_states["reaper"] = {
                    "status": "VALID", "path": str(rpp), "size": rpp.stat().st_size,
                    "proxy_path": str(proxy_path), "proxy_height": effective_height,
                }
            elif rpp and rpp.is_file():
                self._resume_states["reaper"] = {
                    "status": "INVALID", "path": str(rpp), "size": rpp.stat().st_size,
                    "reason": f"Проект REAPER существует, но не использует proxy {effective_height}p.",
                }
        self._refresh_preflight_plan()

    # Compatibility alias for older callers/tests.
    def _refresh_proxy_labels(self) -> None:
        self.refresh_proxy_quality_ui()

    def _sync_project_actions(self) -> None:
        busy = self.job_state in {UiJobState.PREPARING, UiJobState.RUNNING, UiJobState.CANCELLING}
        project_ok = bool(self.current_project_path and self.current_project_path.is_dir())
        rpp_ok = bool(self.current_rpp_path and self.current_rpp_path.is_file())
        vegas_ok = bool(
            self.current_veg_path
            and self.current_veg_path.is_file()
            and self._belongs_to_current_project(self.current_veg_path)
        )
        self.open_folder_button.setEnabled(project_ok and not busy)
        self.rebuild_index_button.setEnabled(project_ok and not busy)
        self.open_rpp_button.setEnabled(rpp_ok and not busy)
        self.open_vegas_button.setEnabled(vegas_ok and not busy)

    def _belongs_to_current_project(self, path: Path) -> bool:
        if not self.current_project_path:
            return False
        try:
            path.resolve().relative_to(self.current_project_path.resolve())
            return True
        except (OSError, ValueError):
            return False

    def open_current_project_folder(self) -> None:
        path = self.current_project_path
        if not path or not path.is_dir():
            QMessageBox.warning(self, "Creator Assistant", "Точная папка текущего проекта не найдена.")
            self._sync_project_actions()
            return
        os.startfile(str(path))

    def rebuild_media_index(self) -> None:
        path = self.current_project_path
        if not path or not path.is_dir():
            return
        answer = QMessageBox.question(
            self,
            "Пересоздать индекс материалов",
            "Будут удалены только scan_index.json и media_probe_cache.json. "
            "Manifest, медиа, RPP и VEGAS не изменятся. Продолжить?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        removed = self.container.projects.clear_media_index(path)
        self.log.appendPlainText(f"Удалено служебных файлов индекса: {removed}. Запускается повторная проверка.")
        self._start_project_migration(path)

    def open_current_rpp(self) -> None:
        rpp = self.current_rpp_path
        if not rpp or not rpp.is_file():
            QMessageBox.warning(self, "Creator Assistant", "RPP текущего проекта не найден.")
            self._sync_project_actions()
            return
        reaper = Path(str(self.container.settings.get("reaper_path", "")))
        if reaper.is_file():
            subprocess.Popen([str(reaper), str(rpp)], shell=False)
        else:
            os.startfile(str(rpp))

    def open_current_vegas(self) -> None:
        veg = self.current_veg_path
        if not veg or not veg.is_file() or not self._belongs_to_current_project(veg):
            QMessageBox.warning(self, "Creator Assistant", "Файл VEGAS текущего проекта не найден или находится вне папки проекта.")
            self._sync_project_actions()
            return
        configured = Path(str(self.container.settings.get("vegas_path", "")))
        if not configured.is_file():
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "Выберите VEGAS Pro",
                str(configured.parent) if str(configured) else "",
                "VEGAS Pro (vegas*.exe);;Программы (*.exe)",
            )
            if selected:
                updated = dict(self.container.settings)
                updated["vegas_path"] = selected
                self.container.save_settings(updated)
                configured = Path(selected)
        if configured.is_file():
            subprocess.Popen([str(configured), str(veg)], shell=False)
        else:
            os.startfile(str(veg))

    def reset_for_new_project(self, url: str = "") -> None:
        """Detach the UI from the previous job while preserving author/settings/options."""
        self._job_generation += 1
        if self.token:
            self.token.cancel()
        self.token = None
        self.active_job_id = None
        self.active_job_proxy_height = None
        self.active_worker = None
        self.active_thread = None
        self.current_request_id = None
        self.metadata_request_in_progress = False
        self._metadata_requested_url = ""
        self._metadata_requested_video_id = ""
        self.metadata = None
        self.selected_project_path = None
        self.current_project_path = None
        self.current_rpp_path = None
        self.current_veg_path = None
        self._auto_opened_vegas_path = ""
        self.project_selection = "new"
        self._resume_states = {}
        self.plan_status = PlanStatus.ACTION_REQUIRED
        self.progress_tracker = None
        self._active_project_url = ""
        self._author_manual_override_video_id = ""
        self._author_resolution_kind = AuthorMatchKind.NO_MATCH
        self._discovery_generation += 1
        self._discovery_video_id = ""
        self._pending_discovery_video_id = ""
        self._reset_after_cancel = False
        self._pending_url_after_cancel = ""
        self.url_edit.blockSignals(True)
        self.url_edit.setText(url)
        self.url_edit.blockSignals(False)
        self.title_edit.clear()
        self.path_edit.clear()
        self.author_status_label.setText("Автор: не определён")
        self.preflight_label.setText("Проверка проекта ещё не выполнялась")
        self.thumbnail_label.clear()
        self.thumbnail_label.setText("Превью появится после проверки ссылки")
        for value in self.info_labels.values():
            value.setText("—")
        self.log.clear()
        self.progress_label.setText("Ожидание")
        self.stage_progress_bar.setRange(0, 100)
        self.stage_progress_bar.setValue(0)
        self.overall_progress_bar.setValue(0)
        self.stage_progress_caption.setText("Текущий этап: 0%")
        self.overall_progress_caption.setText("Общий прогресс проекта: 0%")
        for index, stage in enumerate(ORDERED_STAGES):
            self.stage_list.item(index).setText("○  " + self._stage_name(stage))
        self.create_button.setText("Создать проект")
        self.create_button.setEnabled(False)
        self.dry_run_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.fetch_button.setText("Получить информацию")
        try:
            valid = bool(youtube_video_id(url))
        except Exception:
            valid = False
        self.fetch_button.setEnabled(valid)
        self._set_job_state(UiJobState.IDLE)
        self._refresh_proxy_labels()

    def new_project(self) -> None:
        if self.job_state in {UiJobState.PREPARING, UiJobState.RUNNING, UiJobState.CANCELLING}:
            answer = QMessageBox.question(
                self, "Остановить текущую задачу?",
                "Текущий процесс будет остановлен. Готовые файлы и частичные загрузки сохранятся.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
            self._reset_after_cancel = True
            self.cancel_job()
            return
        self.reset_for_new_project()

    def options(self, dry_run: bool = False) -> ProjectOptions:
        return ProjectOptions(
            download_maximum=self.max_check.isChecked(),
            create_proxy=self.proxy_check.isChecked(),
            download_audio=self.audio_check.isChecked(),
            create_instrumental=self.instrumental_check.isChecked(),
            create_reaper_project=self.reaper_check.isChecked(),
            create_vegas_project=self.vegas_check.isChecked(),
            dry_run=dry_run,
            reaper_proxy_height=self._proxy_height(),
            temp_root=str(self.container.settings.get("temp_root", "")),
        )

    def run_dry_run(self) -> None:
        if not self._ready_to_start():
            return
        assert self.metadata is not None
        assert self.current_destination() is not None
        try:
            result = self.container.projects.dry_run(self.current_destination(), self.metadata, self.options(True))
        except Exception as exc:
            ErrorDialog(str(exc), str(exc), self).exec()
            return
        self.log.appendPlainText("\nDRY RUN — файлы и папки не создавались:")
        self.log.appendPlainText("\n".join("• " + line for line in result.plan_lines))

    def create_project(self) -> None:
        if not self._ready_to_start():
            return
        if self.project_selection == "resume" and self.plan_status == PlanStatus.ALREADY_COMPLETE:
            self.progress_label.setText("Проект готов — повторный workflow не запускался.")
            return
        try:
            require_entitlement = getattr(self.container, "require_entitlement", None)
            if require_entitlement is not None:
                require_entitlement("project_preparation")
        except Exception as exc:
            QMessageBox.warning(self, "Подписка", str(exc))
            return
        assert self.metadata is not None
        destination = self.current_destination()
        assert destination is not None
        options = self.options()
        if self.project_selection == "cancel":
            self._detect_existing_project(self.metadata.video_id)
            return
        resume = self.selected_project_path if self.project_selection == "resume" else None
        new_project_path = self.selected_project_path if self.project_selection == "copy" else None
        resume_states = dict(self._resume_states) if resume else {}
        if resume:
            loaded_manifest = self.container.projects.manifest_loader.load(
                resume / self.container.projects.MANIFEST_NAME
            )
            if loaded_manifest.status.value != "VALID":
                self.progress_label.setText("Manifest требует проверки или восстановления перед продолжением.")
                self._start_project_migration(resume)
                return
            if not self._resolve_preflight_ambiguities(options):
                return
            resume_states = dict(self._resume_states)
        elif new_project_path is None:
            new_project_path = self.container.projects.planned_path(destination, self.metadata)
            if new_project_path.exists():
                self._detect_existing_project(self.metadata.video_id)
                return
        self._job_generation += 1
        self.active_job_id = uuid.uuid4().hex
        options.job_id = self.active_job_id
        self.active_job_proxy_height = options.reaper_proxy_height
        update_context(
            job_id=self.active_job_id,
            generation_id=self._job_generation,
            video_id=self.metadata.video_id,
            project_path=str(resume or new_project_path or ""),
            last_ui_action="start_project_job",
        )
        self.token = CancellationToken()
        self.create_button.setText("Создать проект")
        self._set_busy(True)
        self._set_job_state(UiJobState.PREPARING)
        metadata = self.metadata
        token = self.token
        self._reset_progress(options)
        if resume and resume_states:
            self._apply_resumed_progress(resume_states)

        def work(progress):
            return self.container.projects.execute(
                destination, metadata, options, token, progress, resume, new_project_path
            )

        self._start_worker(work, self._project_ready, self._task_failed, self._progress)

    def _resolve_preflight_ambiguities(self, options: ProjectOptions) -> bool:
        if not self.metadata or not self.selected_project_path:
            return True
        required = self.container.projects.required_roles(options, self._resume_states)
        while True:
            ambiguous = next(
                (
                    (key, details) for key, details in self._resume_states.items()
                    if key in required
                    and details.get("status") in {"AMBIGUOUS", "CONFIRMATION_REQUIRED"}
                ),
                None,
            )
            if not ambiguous:
                break
            role, details = ambiguous
            if role != "audio":
                QMessageBox.warning(
                    self,
                    "Требуется выбор материала",
                    "Найдено несколько файлов для роли " + role + ". "
                    "Отключите этот этап или назначьте файл вручную перед продолжением.",
                )
                self._set_job_state(UiJobState.WAITING_FOR_MEDIA_SELECTION)
                return False
            try:
                dialog = MediaRoleResolutionDialog(
                    title=self.metadata.title,
                    duration=self.metadata.duration,
                    candidates=list(details.get("candidates", [])),
                    project_path=self.selected_project_path,
                    parent=self,
                )
                result = dialog.exec()
            except Exception as exc:
                technical = traceback.format_exc()
                self.log.appendPlainText("Ошибка диалога выбора материала: " + str(exc))
                self._set_job_state(UiJobState.WAITING_FOR_MEDIA_SELECTION)
                self.progress_label.setText("Не удалось открыть выбор аудио. Preflight сохранён; workflow не запускался.")
                ErrorDialog("Не удалось открыть выбор аудио.", technical, self, folder=self.selected_project_path).exec()
                return False
            if (
                result != QDialog.DialogCode.Accepted
                or dialog.resolution_action == MediaRoleResolutionDialog.CANCEL
            ):
                self._set_job_state(UiJobState.WAITING_FOR_MEDIA_SELECTION)
                self.progress_label.setText("Ожидание выбора оригинальной аудиодорожки. Новые файлы не создавались.")
                self.create_button.setText("Продолжить")
                self.create_button.setEnabled(True)
                return False
            try:
                state = self.container.projects.resolve_media_role(
                    self.metadata,
                    self.selected_project_path,
                    role,
                    selected_path=dialog.selected_path,
                    action=dialog.resolution_action,
                    cancellation=CancellationToken(),
                )
            except Exception as exc:
                ErrorDialog(str(exc), str(exc), self, folder=self.selected_project_path).exec()
                self._set_job_state(UiJobState.WAITING_FOR_MEDIA_SELECTION)
                return False
            self._resume_states[role] = state
            if dialog.resolution_action == MediaRoleResolutionDialog.SKIP:
                self.audio_check.setChecked(False)
                options.download_audio = False
                options.create_instrumental = self.instrumental_check.isChecked()
                required = self.container.projects.required_roles(options, self._resume_states)
            elif dialog.resolution_action == MediaRoleResolutionDialog.DOWNLOAD_NEW:
                self.audio_check.setChecked(True)
                options.download_audio = True
            required = self.container.projects.required_roles(options, self._resume_states)
            self._refresh_preflight_plan()
        producers = {
            "maximum": options.download_maximum,
            "proxy": options.create_proxy,
            "audio": options.download_audio,
            "instrumental": options.create_instrumental,
        }
        missing_unproducible = [
            key for key in required
            if key in producers
            and self._resume_states.get(key, {}).get("status") not in {"VALID", "NOT_REQUIRED"}
            and not producers[key]
        ]
        if missing_unproducible:
            QMessageBox.warning(
                self,
                "Неполный план",
                "Для выбранных этапов не найдены готовые зависимости: " + ", ".join(missing_unproducible) +
                ". Включите их создание или выберите готовые файлы.",
            )
            return False
        return True

    def _choose_existing_project(self, path: Path, options: ProjectOptions):
        assert self.metadata is not None
        try:
            states = self.container.projects.inspect_existing(self.metadata, options, path, CancellationToken())
        except Exception as exc:
            states = {}
            self.log.appendPlainText("Не удалось полностью проверить существующий проект: " + str(exc))
        labels = {
            "maximum": "Максимальное видео",
            "proxy": f"Видео {options.reaper_proxy_height}p",
            "audio": "Оригинальное аудио",
            "instrumental": "Инструментал",
            "thumbnail": "Превью",
            "reaper": "Проект REAPER",
            "vegas": "Проект VEGAS",
        }
        symbols = {"VALID": "✓", "PARTIAL": "◐", "INVALID": "✕", "DISABLED": "—", "NOT_REQUIRED": "—", "AMBIGUOUS": "?", "CONFIRMATION_REQUIRED": "?", "MISSING": "○"}
        lines = []
        for key, label in labels.items():
            details = states.get(key, {"status": "MISSING"})
            status = str(details.get("status", "MISSING"))
            suffix = ""
            if status == "VALID":
                size = details.get("size")
                suffix = " — " + (format_bytes(size) + ", готово" if size else "готово")
            elif status == "PARTIAL":
                suffix = " — частичная загрузка будет продолжена"
            elif status == "INVALID":
                suffix = " — не прошёл проверку"
            elif status == "DISABLED":
                suffix = " — отключено"
            elif status == "NOT_REQUIRED":
                suffix = " — не требуется выбранным планом"
            elif status in {"AMBIGUOUS", "CONFIRMATION_REQUIRED"}:
                suffix = " — требуется выбор файла"
            else:
                suffix = " — отсутствует"
            lines.append(f"{symbols.get(status, '○')} {label}{suffix}")
        box = QMessageBox(self)
        box.setWindowTitle("Найден существующий проект")
        box.setIcon(QMessageBox.Information)
        box.setText(f"Найден существующий проект для этого видео:\n\n{path}\n\n" + "\n".join(lines))
        continue_button = box.addButton("Продолжить существующий проект", QMessageBox.AcceptRole)
        destination = self.current_destination()
        copy_path = self.container.projects.copy_path(destination, self.metadata) if destination else None
        copy_button = box.addButton(
            f"Создать новую копию — {copy_path.name}" if copy_path else "Создать новую копию",
            QMessageBox.ActionRole,
        )
        open_button = box.addButton("Открыть папку", QMessageBox.ActionRole)
        cancel_button = box.addButton("Отмена", QMessageBox.RejectRole)
        box.setDefaultButton(continue_button)
        box.exec()
        if box.clickedButton() == continue_button:
            return "continue", states
        if box.clickedButton() == copy_button:
            return "copy", states
        if box.clickedButton() == open_button:
            try:
                os.startfile(str(path))
            except OSError as exc:
                QMessageBox.warning(self, "Папка", str(exc))
            QTimer.singleShot(
                0,
                self._safe_ui_action(
                    "rediscover_existing_project",
                    lambda: self._detect_existing_project(self.metadata.video_id) if self.metadata else None,
                ),
            )
            return "cancel", states
        return "cancel", states

    def _apply_resumed_progress(self, states) -> None:
        if not self.progress_tracker:
            return
        role_stages = {
            "thumbnail": JobStage.DOWNLOAD_THUMBNAIL,
            "maximum": JobStage.DOWNLOAD_MAXIMUM,
            "proxy": JobStage.CREATE_PROXY,
            "audio": JobStage.DOWNLOAD_AUDIO,
            "instrumental": JobStage.SEPARATE_STEMS,
            "reaper": JobStage.CREATE_REAPER,
            "vegas": JobStage.CREATE_VEGAS,
        }
        for key, stage in role_stages.items():
            if states.get(key, {}).get("status") == "VALID":
                self.progress_tracker.mark_complete(stage)
                index = ORDERED_STAGES.index(stage)
                self.stage_list.item(index).setText("✓  " + self._stage_name(stage) + " — готово ранее")
        overall = int(self.progress_tracker.last_overall)
        self.overall_progress_bar.setValue(overall)
        self.overall_progress_caption.setText(f"Общий прогресс проекта: {overall}%")

    def cancel_job(self) -> None:
        if self.token:
            self._set_job_state(UiJobState.CANCELLING)
            self.token.cancel()
            self.container.runner.cancel_active()
            self.log.appendPlainText("Запрошена отмена…")

    def _progress(self, info: ProgressInfo) -> None:
        stage = next((value for value in ORDERED_STAGES if value.value == info.stage), None)
        overall = info.overall_percent
        stage_index = info.stage_index
        stage_count = info.stage_count
        if stage and self.progress_tracker:
            overall = self.progress_tracker.update(stage, info.percent)
            stage_index, stage_count = self.progress_tracker.position(stage)
            if info.percent == 100:
                overall = self.progress_tracker.mark_complete(stage)
        now = time.monotonic()
        stage_changed = info.stage != self._last_painted_stage
        if not stage_changed and info.percent not in (0, 100) and now - self._last_progress_paint < 0.1:
            return
        self._last_progress_paint = now
        self._last_painted_stage = info.stage
        prefix = f"Этап {stage_index} из {stage_count} — " if stage_index and stage_count else ""
        display_stage = self._stage_name(stage) if stage else info.stage
        self.progress_label.setText(prefix + display_stage + (f"\nПодэтап: {info.message}" if info.message else ""))
        if info.percent is None:
            self.stage_progress_bar.setRange(0, 0)
            self.stage_progress_caption.setText("Текущий этап: выполняется")
        else:
            value = max(0, min(100, int(info.percent)))
            self.stage_progress_bar.setRange(0, 100)
            self.stage_progress_bar.setValue(value)
            self.stage_progress_caption.setText(f"Текущий этап: {value}%")
        overall_value = max(0, min(100, int(overall or 0)))
        self.overall_progress_bar.setValue(overall_value)
        self.overall_progress_caption.setText(f"Общий прогресс проекта: {overall_value}%")
        downloaded = info.downloaded or format_bytes(info.downloaded_bytes) or "—"
        total = info.total or format_bytes(info.total_bytes)
        amount = f"{downloaded} из {total}" if total and "неизвест" not in total.casefold() else (total or downloaded)
        speed = info.speed or ((format_bytes(info.speed_bytes_per_second) + "/с") if info.speed_bytes_per_second else "—")
        eta = info.eta or format_duration(info.eta_seconds) or "рассчитывается"
        self.metrics_label.setText(f"Скорость: {speed}\nЗагружено: {amount}\nОсталось: {eta}")
        for index, stage in enumerate(ORDERED_STAGES):
            item = self.stage_list.item(index)
            if stage.value == info.stage:
                suffix = f" — {int(info.percent)}%" if info.percent is not None else ""
                item.setText("▶  " + self._stage_name(stage) + suffix)
                self.stage_list.setCurrentRow(index)
            elif self.progress_tracker and stage in self.progress_tracker.completed:
                item.setText("✓  " + self._stage_name(stage))
        self.log.appendPlainText(f"[{display_stage}] {info.message}")

    def _reset_progress(self, options: ProjectOptions) -> None:
        active = [
            JobStage.VALIDATE_URL,
            JobStage.FETCH_METADATA,
            JobStage.CHECK_DEPENDENCIES,
            JobStage.CHECK_DISK_SPACE,
            JobStage.CREATE_STRUCTURE,
        ]
        if self.metadata and self.metadata.best_thumbnail:
            active.append(JobStage.DOWNLOAD_THUMBNAIL)
        if options.download_maximum:
            active.append(JobStage.DOWNLOAD_MAXIMUM)
        if options.create_proxy:
            active.append(JobStage.CREATE_PROXY)
        if options.download_audio:
            active.append(JobStage.DOWNLOAD_AUDIO)
        if options.create_instrumental:
            active.append(JobStage.SEPARATE_STEMS)
        if options.create_reaper_project:
            active.append(JobStage.CREATE_REAPER)
        if options.create_vegas_project:
            active.append(JobStage.CREATE_VEGAS)
        active.extend((JobStage.FINAL_VALIDATION, JobStage.DONE))
        self.progress_tracker = WeightedProgressTracker(active)
        self.progress_tracker.mark_complete(JobStage.VALIDATE_URL)
        self.progress_tracker.mark_complete(JobStage.FETCH_METADATA)
        for index, stage in enumerate(ORDERED_STAGES):
            prefix = "○" if stage in active else "—"
            suffix = " — пропущено" if stage not in active else ""
            self.stage_list.item(index).setText(f"{prefix}  {self._stage_name(stage)}{suffix}")
        initial = int(self.progress_tracker.last_overall)
        self.stage_progress_bar.setRange(0, 100)
        self.stage_progress_bar.setValue(0)
        self.overall_progress_bar.setValue(initial)
        self.overall_progress_caption.setText(f"Общий прогресс проекта: {initial}%")
        self.stage_progress_caption.setText("Текущий этап: 0%")
        self._last_progress_paint = 0.0
        self._last_painted_stage = ""

    def _project_ready(self, result) -> None:
        self.token = None
        self._set_job_state(UiJobState.COMPLETED)
        self._set_busy(False)
        self.create_button.setEnabled(False)
        self.stage_progress_bar.setRange(0, 100)
        self.stage_progress_bar.setValue(100)
        self.overall_progress_bar.setValue(100)
        self.stage_progress_caption.setText("Текущий этап: 100%")
        self.overall_progress_caption.setText("Общий прогресс проекта: 100%")
        path = result.project_path
        self.current_project_path = Path(path) if path else None
        rpp_value = result.files.get("reaper") if getattr(result, "files", None) else None
        veg_value = result.files.get("vegas") if getattr(result, "files", None) else None
        self.current_rpp_path = Path(rpp_value) if rpp_value else None
        self.current_veg_path = Path(veg_value) if veg_value else None
        if self.current_project_path and not self.current_rpp_path:
            rpps = list(self.current_project_path.glob("*.rpp"))
            self.current_rpp_path = rpps[0] if len(rpps) == 1 else None
        if self.current_project_path and not self.current_veg_path:
            vegs = list(self.current_project_path.glob("*.veg"))
            self.current_veg_path = vegs[0] if len(vegs) == 1 else None
        self._sync_project_actions()
        self.log.appendPlainText("Проект успешно подготовлен: " + str(path))
        if path and self.container.settings.get("open_folder_after_completion", True):
            try:
                os.startfile(str(path))
            except OSError as exc:
                self.log.appendPlainText("Не удалось открыть Проводник: " + str(exc))
        if (
            self.current_veg_path
            and self.container.settings.get("auto_open_vegas_project", False)
            and self._auto_opened_vegas_path.casefold() != str(self.current_veg_path).casefold()
        ):
            self._auto_opened_vegas_path = str(self.current_veg_path)
            self.open_current_vegas()
        QMessageBox.information(self, "Creator Assistant", f"Проект готов:\n{path}")

    def _task_failed(self, message: str, details: str) -> None:
        cancelled = bool(self.token and self.token.is_cancelled)
        self.token = None
        self._set_job_state(UiJobState.CANCELLED if cancelled else UiJobState.FAILED)
        self.fetch_button.setEnabled(True)
        self._set_busy(False)
        self.stage_progress_bar.setRange(0, 100)
        current_row = self.stage_list.currentRow()
        if current_row >= 0:
            item = self.stage_list.item(current_row)
            clean = item.text().lstrip("▶✕⏸ ")
            item.setText(("⏸  " if cancelled else "✕  ") + clean)
        if cancelled:
            self.progress_label.setText("Операция отменена. Прогресс сохранён на последнем реальном значении.")
        self.log.appendPlainText("Ошибка: " + message)
        folder = self.container.projects.resumable_path(self.metadata) if self.metadata else None
        ErrorDialog(
            message,
            details,
            self,
            retry_callback=lambda: QTimer.singleShot(0, self._safe_ui_action("retry_project", self.create_project)),
            folder=folder,
        ).exec()

    def _media_resolution_required(self, request) -> None:
        self.token = None
        self._set_busy(False)
        self._set_job_state(UiJobState.WAITING_FOR_MEDIA_SELECTION)
        self._resume_states[str(request.role)] = {
            "status": "AMBIGUOUS",
            "candidates": list(request.candidates),
            "reason": str(request),
        }
        self.create_button.setEnabled(True)
        self.create_button.setText("Продолжить")
        self.progress_label.setText("Ожидание выбора материала. Проект не переведён в FAILED.")
        QMessageBox.information(
            self,
            "Требуется выбор материала",
            "Найдено несколько подходящих файлов. Нажмите «Продолжить», чтобы выбрать нужный файл.",
        )

    def _waiting_for_disk_space(self, error) -> None:
        self.token = None
        self._set_busy(False)
        self._set_job_state(UiJobState.WAITING_FOR_DISK_SPACE)
        folder = self.container.projects.resumable_path(self.metadata) if self.metadata else None
        self.current_project_path = Path(folder) if folder else self.current_project_path
        if folder and Path(folder).is_dir():
            self.selected_project_path = Path(folder)
            self.project_selection = "resume"
        self.create_button.setEnabled(True)
        self.create_button.setText("Продолжить проект")
        self.progress_label.setText("Задание приостановлено: освободите место и нажмите «Продолжить проект».")
        required = format_bytes(error.required_bytes + error.reserve_bytes)
        free = format_bytes(error.free_bytes)
        QMessageBox.warning(
            self,
            "Недостаточно места",
            f"Операция: {error.operation}\nПуть: {error.path}\n"
            f"Нужно с резервом: {required}\nСвободно: {free}\n\n"
            "Готовые файлы и временные данные текущего задания сохранены.",
        )
        self._sync_project_actions()

    def _resource_memory_error(self, error) -> None:
        self.token = None
        self._set_job_state(UiJobState.ABANDONED)
        self._set_busy(False)
        self.create_button.setEnabled(True)
        self.create_button.setText("Продолжить проект")
        QMessageBox.warning(self, "Недостаточно памяти", str(error))

    def _audio_output_missing(self, error) -> None:
        self.token = None
        self._set_job_state(UiJobState.ABANDONED)
        self._set_busy(False)
        self.create_button.setEnabled(True)
        self.create_button.setText("Продолжить проект")
        QMessageBox.warning(
            self,
            "Instrumental не найден",
            f"Audio Separator завершился без ожидаемого результата.\n"
            f"Временная папка: {error.temp_path}\nПапка результата: {error.output_path}",
        )

    def _task_cancelled(self) -> None:
        self.token = None
        self._set_job_state(UiJobState.CANCELLED)
        self.fetch_button.setEnabled(True)
        self._set_busy(False)
        self.stage_progress_bar.setRange(0, 100)
        current_row = self.stage_list.currentRow()
        stage_text = "Текущий этап"
        if current_row >= 0:
            item = self.stage_list.item(current_row)
            clean = item.text().lstrip("▶✕⏸ ").split(" — ", 1)[0]
            stage_text = clean
            item.setText("⏸  " + clean + " — отменено")
        self.progress_label.setText(f"Операция отменена пользователем.\nОтменено на этапе: {stage_text}")
        self.log.appendPlainText(f"Операция отменена. Готовые файлы сохранены. Этап: {stage_text}")
        self.cancel_button.setEnabled(False)
        self.create_button.setEnabled(True)
        self.create_button.setText("Продолжить проект")

        if self._reset_after_cancel:
            pending_url = self._pending_url_after_cancel
            QTimer.singleShot(0, self._safe_ui_action("reset_after_cancel", lambda: self.reset_for_new_project(pending_url)))

    def _manual_uvr_required(self, request) -> None:
        self.token = None
        self._set_job_state(UiJobState.ABANDONED)
        self._set_busy(False)
        self.stage_progress_bar.setRange(0, 100)
        self.progress_label.setText("Разделение аудио через UVR\nТребуется ручное действие; обработка не запущена автоматически.")
        index = ORDERED_STAGES.index(JobStage.SEPARATE_STEMS)
        self.stage_list.item(index).setText("⚠  " + JobStage.SEPARATE_STEMS.value + " — требуется ручное действие")
        self.stage_list.setCurrentRow(index)
        self.create_button.setText("Продолжить проект")

        def validate(path: Path) -> None:
            self.container.projects.validator.validate_expected_audio(
                path,
                CancellationToken(),
                duration=self.metadata.duration if self.metadata else None,
                require_flac=True,
            )

        dialog = ManualUvrDialog(request.source, request.expected_output, request.launcher, validate, self)
        if dialog.exec():
            QTimer.singleShot(0, self._safe_ui_action("resume_after_uvr", self.create_project))

    def _runtime_install_required(self, request) -> None:
        self.token = None
        self._set_job_state(UiJobState.ABANDONED)
        self._set_busy(False)
        answer = QMessageBox.question(
            self,
            "Установка Audio Separator Runtime",
            "Для автоматического разделения требуется установить локальный Audio Separator Runtime.\n\n"
            "Будут установлены Python 3.11, audio-separator и GPU-зависимости. Размер может составить несколько ГБ.\n\n"
            f"Путь: {request.runtime_path}\n\nУстановить сейчас?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            self.progress_label.setText("Установка Audio Separator Runtime отменена пользователем.")
            return
        self.token = CancellationToken()
        self._set_busy(True)
        manager = self.container.audio_separator_runtime

        def work(progress):
            return manager.install(lambda message: progress(ProgressInfo(JobStage.SEPARATE_STEMS.value, message)))

        def done(info):
            self.token = None
            self._set_busy(False)
            self.container.rebuild()
            self._refresh_backend_label()
            QMessageBox.information(self, "Audio Separator", "Runtime установлен и проверен.\n\n" + str(info.get("version", "")))
            QTimer.singleShot(0, self._safe_ui_action("resume_after_runtime_install", self.create_project))

        self._start_worker(work, done, self._task_failed, self._progress)

    def _refresh_backend_label(self) -> None:
        runtime = self.container.audio_separator_runtime
        backend = "Audio Separator — требуется установка"
        device = "не определено"
        if runtime.is_ready():
            backend = "Audio Separator CPU"
            try:
                marker = json.loads(runtime.marker_path.read_text(encoding="utf-8"))
                raw = marker.get("environment", {}).get("onnxruntime", "{}")
                environment = json.loads(raw)
                if environment.get("cuda_available") and "CUDAExecutionProvider" in environment.get("providers", []):
                    backend = "Audio Separator GPU"
                device = environment.get("gpu") or "CPU"
            except (OSError, ValueError, TypeError):
                pass
        self.backend_label.setText(
            "Разделение аудио: UVR-MDX-NET Inst HQ 3\n"
            f"Backend: {backend}\n"
            f"Устройство: {device}"
        )

    def _ready_to_start(self) -> bool:
        if self.active_thread or self.metadata_thread or (self._discovery_thread and self._discovery_thread.isRunning()):
            self.progress_label.setText("Предыдущая операция ещё завершается. Дождитесь её окончания.")
            return False
        if not self.metadata:
            QMessageBox.warning(self, "Creator Assistant", "Сначала получите информацию о видео.")
            return False
        destination = self.current_destination()
        if not destination or not destination.is_dir():
            QMessageBox.warning(self, "Creator Assistant", "Выберите существующую папку назначения.")
            return False
        return True

    def shutdown_workers(self, timeout_ms: int = 5000) -> bool:
        """Cancel and join owned threads before their parent widgets are destroyed."""
        crash_event(f"Worker shutdown requested; active_threads={len(self._threads)}")
        if self.token:
            self.token.cancel()
        try:
            self.container.runner.cancel_active()
        except Exception:
            pass
        threads = list(self._threads)
        for thread in threads:
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
        if not threads:
            return True
        per_thread = max(100, timeout_ms // len(threads))
        complete = True
        for thread in threads:
            if thread.isRunning() and not thread.wait(per_thread):
                complete = False
                crash_event(f"QThread still running during shutdown: {thread.objectName()}")
        return complete

    def _set_busy(self, busy: bool) -> None:
        self.create_button.setEnabled(not busy and self.metadata is not None)
        self.dry_run_button.setEnabled(not busy and self.metadata is not None)
        try:
            valid_url = bool(youtube_video_id(self.url_edit.text()))
        except Exception:
            valid_url = False
        self.fetch_button.setEnabled(not busy and not self.metadata_request_in_progress and valid_url)
        self.cancel_button.setEnabled(busy)
        for checkbox in (
            self.max_check,
            self.proxy_check,
            self.audio_check,
            self.instrumental_check,
            self.reaper_check,
            self.vegas_check,
        ):
            checkbox.setEnabled(not busy)

    def _start_worker(self, function, finished, failed, progress=None) -> None:
        thread = QThread(self)
        worker = FunctionWorker(function)
        generation = self._job_generation
        job_id = self.active_job_id
        label = (job_id or "operation")[:8]
        thread.setObjectName(f"project-thread-{label}")
        worker.setObjectName(f"project-worker-{label}")
        terminal = {"kind": "", "args": ()}

        def queue_terminal(kind: str, *args) -> None:
            terminal["kind"] = kind
            terminal["args"] = args
            crash_event(f"Project worker terminal signal queued: {kind}")

        def cleanup():
            crash_event(f"Project QThread finished job_id={job_id} generation={generation}")
            if thread in self._threads:
                self._threads.remove(thread)
            if generation == self._job_generation and job_id == self.active_job_id:
                self.active_worker = None
                self.active_thread = None
                update_context(project_worker="absent")
            bridge.deleteLater()
            if generation != self._job_generation or job_id != self.active_job_id:
                return
            kind = terminal["kind"]
            args = terminal["args"]
            terminal_callbacks = {
                "finished": finished,
                "failed": failed,
                "cancelled": self._task_cancelled,
                "manual_action_required": self._manual_uvr_required,
                "runtime_install_required": self._runtime_install_required,
                "authentication_required": self._project_authentication_required,
                "cookies_unavailable": self._project_cookies_unavailable,
                "media_forbidden": self._media_forbidden,
                "waiting_for_disk_space": self._waiting_for_disk_space,
                "gpu_memory_required": self._resource_memory_error,
                "system_memory_required": self._resource_memory_error,
                "audio_output_missing": self._audio_output_missing,
                "media_resolution_required": self._media_resolution_required,
            }
            callback = terminal_callbacks.get(kind)
            if callback:
                self._safe_ui_action(f"project_terminal:{kind}", lambda: callback(*args))()

        callbacks = {
            "finished": lambda value: queue_terminal("finished", value),
            "failed": lambda message, details: queue_terminal("failed", message, details),
            "cancelled": lambda: queue_terminal("cancelled"),
            "manual_action_required": lambda value: queue_terminal("manual_action_required", value),
            "runtime_install_required": lambda value: queue_terminal("runtime_install_required", value),
            "authentication_required": lambda value: queue_terminal("authentication_required", value),
            "cookies_unavailable": lambda value: queue_terminal("cookies_unavailable", value),
            "media_forbidden": lambda value: queue_terminal("media_forbidden", value),
            "waiting_for_disk_space": lambda value: queue_terminal("waiting_for_disk_space", value),
            "gpu_memory_required": lambda value: queue_terminal("gpu_memory_required", value),
            "system_memory_required": lambda value: queue_terminal("system_memory_required", value),
            "audio_output_missing": lambda value: queue_terminal("audio_output_missing", value),
            "media_resolution_required": lambda value: queue_terminal("media_resolution_required", value),
            "thread_finished": cleanup,
        }
        if progress:
            callbacks["progress"] = progress
        bridge = UiWorkerBridge(
            callbacks,
            predicate=lambda: generation == self._job_generation and job_id == self.active_job_id,
            on_error=self._ui_action_failed,
            parent=self,
        )
        thread.worker = worker
        thread.bridge = bridge
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(bridge.finished)
        worker.failed.connect(bridge.failed)
        worker.cancelled.connect(bridge.cancelled)
        worker.manual_action_required.connect(bridge.manual_action_required)
        worker.runtime_install_required.connect(bridge.runtime_install_required)
        worker.authentication_required.connect(bridge.authentication_required)
        worker.cookies_unavailable.connect(bridge.cookies_unavailable)
        worker.media_forbidden.connect(bridge.media_forbidden)
        worker.waiting_for_disk_space.connect(bridge.waiting_for_disk_space)
        worker.gpu_memory_required.connect(bridge.gpu_memory_required)
        worker.system_memory_required.connect(bridge.system_memory_required)
        worker.audio_output_missing.connect(bridge.audio_output_missing)
        worker.media_resolution_required.connect(bridge.media_resolution_required)
        if progress:
            worker.progress.connect(bridge.progress)
        for signal in (
            worker.finished, worker.failed, worker.cancelled, worker.manual_action_required,
            worker.runtime_install_required, worker.authentication_required,
            worker.cookies_unavailable, worker.media_forbidden,
            worker.waiting_for_disk_space, worker.gpu_memory_required,
            worker.system_memory_required, worker.audio_output_missing, worker.media_resolution_required,
        ):
            signal.connect(worker.deleteLater)
            signal.connect(thread.quit)
        thread.finished.connect(bridge.thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        self.active_worker = worker
        self.active_thread = thread
        update_context(project_worker=f"running:{worker.objectName()}")
        crash_event(f"Project QThread starting job_id={job_id} generation={generation}")
        if self.job_state == UiJobState.PREPARING:
            self._set_job_state(UiJobState.RUNNING)
        thread.start()
