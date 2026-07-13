from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from creator_assistant.app import ServiceContainer
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import SourceInfo
from creator_assistant.services.shorts.cache import ShortsCache
from creator_assistant.services.shorts.candidate_generator import CandidateSettings
from creator_assistant.services.shorts.manifest import ShortsManifestStore
from creator_assistant.services.shorts.review_service import CandidateReviewService
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectPaths, ShortsProjectStore
from creator_assistant.services.shorts.source_service import ShortsSourceService
from creator_assistant.ui.shorts.analysis_progress_panel import AnalysisProgressPanel
from creator_assistant.ui.shorts.analysis_settings_panel import AnalysisSettingsPanel
from creator_assistant.ui.shorts.candidate_editor import CandidateEditor
from creator_assistant.ui.shorts.candidate_list import CandidateList
from creator_assistant.ui.shorts.source_panel import SourcePanel
from creator_assistant.ui.widgets.error_dialog import ErrorDialog


class _ProbeWorker(QObject):
    finished = Signal(object)
    failed = Signal(str, str)

    def __init__(self, service: ShortsSourceService, path: Path) -> None:
        super().__init__()
        self.service = service
        self.path = path

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self.service.probe(self.path))
        except Exception as exc:
            self.failed.emit(str(exc), repr(exc))


class _AnalysisWorker(QObject):
    finished = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()
    progress = Signal(str, str, int)

    def __init__(self, container: ServiceContainer, source: SourceInfo, paths: ShortsProjectPaths, token: CancellationToken, candidate_settings: CandidateSettings) -> None:
        super().__init__()
        self.container, self.source, self.paths, self.token = container, source, paths, token
        self.candidate_settings = candidate_settings

    @Slot()
    def run(self) -> None:
        try:
            store = ShortsManifestStore(self.paths.manifest)
            manifest = store.load()
            if manifest is None:
                raise RuntimeError("Manifest проекта Shorts не найден.")
            cache = ShortsCache(manifest)
            proxy = self.paths.cache / "analysis_proxy.mp4"
            proxy_settings = {"height": 720, "codec": "h264"}
            if not cache.stage_valid("proxy", proxy, proxy_settings):
                self.progress.emit("2. Создание proxy", "FFmpeg готовит H.264 720p для быстрого предпросмотра.", 12)
                self.container.shorts_proxy.create(Path(self.source.path), proxy, self.token)
                cache.mark_complete("proxy", proxy_settings)
                store.save(manifest)
            audio = self.paths.cache / "transcription_audio.wav"
            audio_settings = {"channels": 1, "sample_rate": 16000, "codec": "pcm_s16le"}
            if not cache.stage_valid("audio", audio, audio_settings):
                self.progress.emit("3. Извлечение аудио", "Создаётся mono PCM 16 kHz без изменения таймингов.", 24)
                self.container.shorts_audio.extract(Path(self.source.path), audio, self.token)
                cache.mark_complete("audio", audio_settings)
                store.save(manifest)
            transcript_path = self.paths.analysis / "transcript.json"
            transcription_settings = {
                key: self.container.settings.get(key)
                for key in (
                    "whisper_backend", "whisper_model", "whisper_language", "whisper_device",
                    "whisper_profile", "whisper_use_gpu", "whisper_fp16", "whisper_word_timestamps",
                    "whisper_use_dictionary", "whisper_dictionary",
                )
            }
            if cache.stage_valid("transcription", transcript_path, transcription_settings):
                transcript = self.container.shorts_transcription.load(transcript_path)
                self.progress.emit("Транскрипция из cache", "Исходник и настройки не изменились — Whisper не запускается повторно.", 55)
            else:
                capabilities = self.container.shorts_transcription_backend.capabilities()
                self.progress.emit("4–5. Whisper", f"{capabilities.name}; модель {self.container.settings.get('whisper_model')}; прогресс backend не сообщает.", 32)
                transcript = self.container.shorts_transcription.transcribe(audio, self.paths.analysis, self.token)
                manifest.transcription_backend = transcript.backend
                manifest.whisper_model = transcript.model
                cache.mark_complete("transcription", transcription_settings)
                store.save(manifest)
            self.progress.emit("6. Анализ сцен", "FFmpeg определяет реальные смены сцен без тяжёлой CV-модели.", 62)
            scenes_path = self.paths.analysis / "scenes.json"
            scene_settings = {"threshold": self.container.shorts_scenes.threshold}
            if cache.stage_valid("scenes", scenes_path, scene_settings):
                scenes = self.container.shorts_scenes.load(scenes_path)
            else:
                scenes = self.container.shorts_scenes.detect(proxy, self.source.duration, scenes_path, self.token)
                cache.mark_complete("scenes", scene_settings)
                store.save(manifest)
            self.progress.emit("7. Анализ звука", "Определяются речь, тишина и средняя громкость.", 72)
            audio_features_path = self.paths.analysis / "audio_features.json"
            activity_settings = {"silence_db": -35, "minimum_pause": 0.7}
            if cache.stage_valid("audio_activity", audio_features_path, activity_settings):
                audio_features = self.container.shorts_audio_activity.load(audio_features_path)
            else:
                audio_features = self.container.shorts_audio_activity.analyse(audio, self.source.duration, audio_features_path, self.token)
                cache.mark_complete("audio_activity", activity_settings)
                store.save(manifest)
            self.progress.emit("8–10. Кандидаты", "Границы по фразам, паузам и сценам; затем эвристическая оценка и дедупликация.", 84)
            candidates_path = self.paths.analysis / "candidates.json"
            candidate_config = asdict(self.candidate_settings)
            if cache.stage_valid("candidates", candidates_path, candidate_config):
                from creator_assistant.domain.shorts.models import Candidate
                candidates = [Candidate(**item) for item in json.loads(candidates_path.read_text(encoding="utf-8"))]
            else:
                raw_candidates = self.container.shorts_candidate_generator.generate(transcript, scenes, audio_features, self.candidate_settings)
                scored = [self.container.shorts_candidate_scorer.score(item, scenes, audio_features) for item in raw_candidates]
                candidates = self.container.shorts_duplicate_filter.filter(scored, self.candidate_settings.count)
                candidates_path.write_text(json.dumps([asdict(item) for item in candidates], ensure_ascii=False, indent=2), encoding="utf-8")
                cache.mark_complete("candidates", candidate_config)
                manifest.candidates = [asdict(item) for item in candidates]
                store.save(manifest)
            self.progress.emit("11. Ожидание пользователя", f"Подготовлено {len(candidates)} непохожих кандидатов для ручной проверки.", 90)
            self.finished.emit({"transcript": transcript, "candidates": candidates, "scenes": scenes, "audio_features": audio_features})
        except Exception as exc:
            from creator_assistant.domain.errors import JobCancelledError
            if isinstance(exc, JobCancelledError) or self.token.is_cancelled:
                self.cancelled.emit()
            else:
                import traceback
                self.failed.emit(str(exc), traceback.format_exc())


class ShortsTab(QWidget):
    """Independent Shorts workspace; later stages plug into its inner tabs."""

    def __init__(self, container: ServiceContainer, parent=None) -> None:
        super().__init__(parent)
        self.container = container
        self.source_service = ShortsSourceService(container.runner, container.paths.get("ffprobe", ""))
        self.project_store = ShortsProjectStore()
        self.source: Optional[SourceInfo] = None
        self.paths: Optional[ShortsProjectPaths] = None
        self.candidates = []
        self.review_service: Optional[CandidateReviewService] = None
        self._pending_root: Optional[Path] = None
        self._thread: Optional[QThread] = None
        self._token: Optional[CancellationToken] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        header = QLabel("Shorts · локальный анализ и вертикальный рендер")
        header.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(header)
        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.source_panel = SourcePanel()
        self.progress_panel = AnalysisProgressPanel()
        self.analysis_settings = AnalysisSettingsPanel()
        self.start_button = QPushButton("Запустить анализ")
        self.start_button.setEnabled(False)
        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.setEnabled(False)
        action_row = QHBoxLayout()
        action_row.addWidget(self.start_button)
        action_row.addWidget(self.cancel_button)
        left_layout.addWidget(self.source_panel)
        left_layout.addWidget(self.progress_panel)
        left_layout.addWidget(self.analysis_settings)
        left_layout.addLayout(action_row)
        left_layout.addStretch(1)
        self.workspace = QTabWidget()
        self.candidate_list = CandidateList()
        self.candidate_editor = CandidateEditor()
        self.workspace.addTab(self.candidate_list, "Кандидаты")
        self.workspace.addTab(self.candidate_editor, "Редактор")
        self.workspace.addTab(self._placeholder("Субтитры создаются отдельно для каждого Short."), "Субтитры")
        self.workspace.addTab(self._placeholder("Одобренные фрагменты попадут в последовательную очередь."), "Рендер")
        splitter.addWidget(left)
        splitter.addWidget(self.workspace)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)
        self.source_panel.choose_file_requested.connect(self.choose_file)
        self.source_panel.choose_project_requested.connect(self.choose_project)
        self.start_button.clicked.connect(self.start_analysis)
        self.cancel_button.clicked.connect(self.cancel_analysis)
        self.candidate_list.selected.connect(self._edit_candidate)
        self.candidate_list.status_changed.connect(self._set_candidate_status)
        self.candidate_editor.status_changed.connect(self._set_candidate_status)
        self.candidate_editor.boundaries_saved.connect(self._save_boundaries)

    @staticmethod
    def _placeholder(text: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch(1)
        return widget

    @Slot()
    def choose_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Выберите готовое видео", "", "Видео (*.mp4 *.mkv *.mov *.m4v *.webm *.avi);;Все файлы (*)"
        )
        if not selected:
            return
        output = QFileDialog.getExistingDirectory(self, "Выберите папку, в которой создать проект Shorts", str(Path(selected).parent))
        if output:
            self._begin_probe(Path(selected), Path(output) / f"{Path(selected).stem} Shorts")

    @Slot()
    def choose_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Выберите папку проекта Creator Assistant")
        if not selected:
            return
        folder = Path(selected)
        candidates = self.source_service.project_video_candidates(folder)
        if not candidates:
            QMessageBox.warning(self, "Shorts", "В папке проекта не найден подходящий финальный видеофайл.")
            return
        if len(candidates) > 1:
            picker = QFileDialog(self, "Выберите финальный рендер", str(folder))
            picker.setNameFilter("Видео (*.mp4 *.mkv *.mov *.m4v *.webm *.avi)")
            picker.setFileMode(QFileDialog.ExistingFile)
            if not picker.exec() or not picker.selectedFiles():
                return
            source = Path(picker.selectedFiles()[0])
        else:
            source = candidates[0]
        self._begin_probe(source, folder / "Shorts")

    def _begin_probe(self, source: Path, root: Path) -> None:
        if self._thread and self._thread.isRunning():
            return
        self._pending_root = root
        self.source_panel.set_busy(True)
        self.progress_panel.update_state("1. Проверка исходника", "FFprobe читает только метаданные; исходник не изменяется.", 2)
        thread = QThread(self)
        worker = _ProbeWorker(self.source_service, source)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._probe_finished)
        worker.failed.connect(self._probe_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._probe_thread_finished)
        self._thread = thread
        self._thread.worker = worker
        thread.start()

    @Slot()
    def start_analysis(self) -> None:
        if not self.source or not self.paths or (self._thread and self._thread.isRunning()):
            return
        self._token = CancellationToken()
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        thread = QThread(self)
        worker = _AnalysisWorker(self.container, self.source, self.paths, self._token, self.analysis_settings.value())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.progress_panel.update_state)
        worker.finished.connect(self._analysis_finished)
        worker.failed.connect(self._probe_failed)
        worker.cancelled.connect(self._analysis_cancelled)
        for signal in (worker.finished, worker.failed, worker.cancelled):
            signal.connect(thread.quit)
            signal.connect(worker.deleteLater)
        thread.finished.connect(self._analysis_thread_finished)
        self._thread = thread
        self._thread.worker = worker
        thread.start()

    @Slot()
    def cancel_analysis(self) -> None:
        if self._token:
            self._token.cancel()
        self.container.runner.cancel_active()
        self.progress_panel.update_state("Отмена", "Останавливаю текущий внешний процесс; готовый cache сохранится.", self.progress_panel.progress.value())

    @Slot(object)
    def _analysis_finished(self, payload) -> None:
        candidates = payload["candidates"]
        self.candidates = candidates
        self.candidate_list.set_candidates(candidates)
        self.progress_panel.update_state("Анализ готов", f"Найдено {len(candidates)} кандидатов. Эвристическая оценка требует проверки человеком.", 90)

    @Slot(object)
    def _edit_candidate(self, candidate) -> None:
        if not self.paths:
            return
        self.candidate_editor.set_candidate(candidate, self.paths.cache / "analysis_proxy.mp4")
        self.workspace.setCurrentWidget(self.candidate_editor)

    @Slot(object, str)
    def _set_candidate_status(self, candidate, status: str) -> None:
        if not self.review_service:
            return
        candidate.status = status
        try:
            self.review_service.save(self.candidates)
            self.candidate_list.refresh()
            label = "одобрен" if status == "approved" else "отклонён"
            self.progress_panel.update_state("Проверка кандидатов", f"{candidate.id}: {label}. Решение сохранено.", 92)
        except Exception as exc:
            ErrorDialog(str(exc), repr(exc), self).exec()

    @Slot(object, float, float)
    def _save_boundaries(self, candidate, start: float, end: float) -> None:
        if not self.review_service:
            return
        try:
            self.review_service.update_boundaries(candidate, start, end)
            self.review_service.save(self.candidates)
            self.candidate_list.refresh()
            self.progress_panel.update_state("Границы сохранены", f"{candidate.id}: {start:.3f}–{end:.3f} сек.", 92)
        except Exception as exc:
            ErrorDialog(str(exc), repr(exc), self).exec()

    @Slot()
    def _analysis_cancelled(self) -> None:
        self.progress_panel.update_state("Анализ отменён", "Готовые этапы сохранены. Можно нажать «Запустить анализ» и продолжить.", self.progress_panel.progress.value())

    @Slot()
    def _analysis_thread_finished(self) -> None:
        thread = self._thread
        self._thread = None
        self._token = None
        self.start_button.setEnabled(bool(self.source))
        self.cancel_button.setEnabled(False)
        if thread:
            thread.deleteLater()

    @Slot(object)
    def _probe_finished(self, source: SourceInfo) -> None:
        assert self._pending_root is not None
        self.source = source
        self.paths = self.project_store.open_or_create(self._pending_root, source)
        self.review_service = CandidateReviewService(self.paths, source.duration)
        source_json = self.paths.analysis / "source_info.json"
        source_json.write_text(json.dumps(source.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        self.source_panel.show_source(source, self.paths.root)
        self.progress_panel.update_state("Источник готов", "Manifest и структура проекта сохранены атомарно.", 8)
        self.start_button.setEnabled(True)
        candidates_path = self.paths.analysis / "candidates.json"
        if candidates_path.is_file():
            from creator_assistant.domain.shorts.models import Candidate
            try:
                self.candidates = [Candidate(**item) for item in json.loads(candidates_path.read_text(encoding="utf-8"))]
                self.candidate_list.set_candidates(self.candidates)
                if self.candidates:
                    self.progress_panel.update_state("Проект восстановлен", f"Загружено {len(self.candidates)} кандидатов; завершённые этапы будут взяты из cache.", 90)
            except (OSError, ValueError, TypeError):
                self.candidates = []

    @Slot(str, str)
    def _probe_failed(self, message: str, details: str) -> None:
        self.progress_panel.update_state("Ошибка проверки", message, 0)
        ErrorDialog(message, details, self).exec()

    @Slot()
    def _probe_thread_finished(self) -> None:
        self.source_panel.set_busy(False)
        thread = self._thread
        self._thread = None
        if thread:
            thread.deleteLater()

    def shutdown_workers(self) -> bool:
        if not self._thread or not self._thread.isRunning():
            return True
        self.container.runner.cancel_active()
        if self._token:
            self._token.cancel()
        self._thread.quit()
        return self._thread.wait(5000)
