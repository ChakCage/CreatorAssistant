from __future__ import annotations

import json
from dataclasses import asdict, replace
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
from creator_assistant.domain.shorts.models import RenderJob, SourceInfo, SubtitleCue
from creator_assistant.services.shorts.cache import ShortsCache
from creator_assistant.services.shorts.candidate_generator import CandidateSettings
from creator_assistant.services.shorts.hybrid_analyzer import HybridAnalysisResult
from creator_assistant.services.shorts.manifest import ShortsManifestStore, utc_now
from creator_assistant.services.shorts.review_service import CandidateReviewService
from creator_assistant.services.shorts.render_service import unique_output_path
from creator_assistant.services.shorts.semantic_cache import SemanticCache
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectPaths, ShortsProjectStore
from creator_assistant.services.shorts.source_service import ShortsSourceService
from creator_assistant.ui.shorts.analysis_progress_panel import AnalysisProgressPanel
from creator_assistant.ui.shorts.analysis_settings_panel import AnalysisSettingsPanel
from creator_assistant.ui.shorts.candidate_editor import CandidateEditor
from creator_assistant.ui.shorts.candidate_list import CandidateList
from creator_assistant.ui.shorts.source_panel import SourcePanel
from creator_assistant.ui.shorts.subtitle_editor import SubtitleEditor
from creator_assistant.ui.shorts.render_queue import RenderQueue
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
                estimated_proxy = max(256 * 1024**2, int(self.source.size * min(1.0, 720 / max(1, self.source.height))))
                self.container.projects.storage.require("Analysis proxy Shorts", self.paths.cache, estimated_proxy)
                self.container.shorts_proxy.create(Path(self.source.path), proxy, self.token)
                cache.mark_complete("proxy", proxy_settings)
                store.save(manifest)
            audio = self.paths.cache / "transcription_audio.wav"
            audio_settings = {"channels": 1, "sample_rate": 16000, "codec": "pcm_s16le"}
            if not cache.stage_valid("audio", audio, audio_settings):
                self.progress.emit("3. Извлечение аудио", "Создаётся mono PCM 16 kHz без изменения таймингов.", 24)
                self.container.projects.storage.require("Аудио Whisper Shorts", self.paths.cache, int(self.source.duration * 16000 * 2 + 64 * 1024**2))
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
                self.container.projects.storage.require("Транскрипция Shorts", self.paths.analysis, 256 * 1024**2)
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
                if scenes and not any(scene.thumbnail and Path(scene.thumbnail).is_file() for scene in scenes):
                    scenes = self.container.shorts_scenes.generate_thumbnails(proxy, scenes, self.paths.thumbnails, self.token)
                    self.container.shorts_scenes.save(scenes_path, scenes)
            else:
                scenes = self.container.shorts_scenes.detect(proxy, self.source.duration, scenes_path, self.token)
                scenes = self.container.shorts_scenes.generate_thumbnails(proxy, scenes, self.paths.thumbnails, self.token)
                self.container.shorts_scenes.save(scenes_path, scenes)
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
            self.progress.emit("8–10. Кандидаты", "Эвристический отбор, кластеризация дублей и локальная смысловая оценка.", 82)
            candidates_path = self.paths.analysis / "candidates.json"
            ai_settings = dict(self.container.settings.get("shorts_ai", {}))
            candidate_config = {**asdict(self.candidate_settings), "shorts_ai": ai_settings}
            if cache.stage_valid("candidates", candidates_path, candidate_config):
                from creator_assistant.domain.shorts.models import Candidate
                candidates = [Candidate(**item) for item in json.loads(candidates_path.read_text(encoding="utf-8"))]
                restored_ai = any(item.selection_source == "hybrid_ai" for item in candidates)
                analysis_result = HybridAnalysisResult(
                    candidates=candidates,
                    used_ai=restored_ai,
                    cache_hit=restored_ai,
                    backend=str(ai_settings.get("backend", "disabled")),
                    model=str(ai_settings.get("model", "")),
                    model_digest=str(manifest.ai_analysis.get("model_digest", "")) if manifest else "",
                    quantization=str(manifest.ai_analysis.get("quantization", "")) if manifest else "",
                    analysis_mode=str(manifest.ai_analysis.get("mode", ai_settings.get("mode", "balanced"))),
                    cache_key=str(manifest.ai_analysis.get("cache_key", "")) if manifest else "",
                )
            else:
                raw_candidates = self.container.shorts_candidate_generator.generate(transcript, scenes, audio_features, self.candidate_settings)
                scored = [self.container.shorts_candidate_scorer.score(item, scenes, audio_features) for item in raw_candidates]
                if ai_settings.get("enabled", False):
                    self.progress.emit(
                        "10. Локальный AI-анализ",
                        f"{ai_settings.get('model', 'qwen3:14b')} сравнивает смысловые моменты; исходник остаётся на компьютере.",
                        88,
                    )
                analysis_result = self.container.shorts_hybrid_analyzer.analyse(
                    scored, transcript, scenes, audio_features,
                    self.container.shorts_semantic_backend, ai_settings,
                    SemanticCache(self.paths.analysis / "semantic_cache.json"), self.token,
                    content_type=self.candidate_settings.content_type,
                    requested_count=(
                        int(ai_settings.get("final_count", 5))
                        if ai_settings.get("enabled", False) else self.candidate_settings.count
                    ),
                )
                candidates = analysis_result.candidates
                candidates_path.write_text(json.dumps([asdict(item) for item in candidates], ensure_ascii=False, indent=2), encoding="utf-8")
                cache.mark_complete("candidates", candidate_config)
                manifest.candidates = [asdict(item) for item in candidates]
                manifest.analysis_settings = candidate_config
                manifest.ai_analysis = {
                    "backend": analysis_result.backend,
                    "model": analysis_result.model,
                    "model_digest": analysis_result.model_digest,
                    "quantization": analysis_result.quantization,
                    "prompt_version": "shorts-semantic-v1",
                    "mode": analysis_result.analysis_mode,
                    "timestamp": utc_now(),
                    "cache_key": analysis_result.cache_key,
                    "cache_hit": analysis_result.cache_hit,
                    "used_ai": analysis_result.used_ai,
                    "fallback_reason": analysis_result.fallback_reason,
                    "metrics": analysis_result.metrics,
                }
                store.save(manifest)
            if analysis_result and analysis_result.fallback_reason:
                summary = f"AI недоступен — применён эвристический fallback: {analysis_result.fallback_reason}"
            elif analysis_result and analysis_result.used_ai:
                source = "semantic cache" if analysis_result.cache_hit else analysis_result.model
                summary = f"Гибридная оценка завершена: {source}."
            else:
                summary = "Использована эвристическая оценка."
            self.progress.emit("11. Ожидание пользователя", f"Подготовлено {len(candidates)} непохожих кандидатов. {summary}", 92)
            self.finished.emit({
                "transcript": transcript, "candidates": candidates, "scenes": scenes,
                "audio_features": audio_features, "analysis_result": analysis_result,
            })
        except Exception as exc:
            from creator_assistant.domain.errors import JobCancelledError
            if isinstance(exc, JobCancelledError) or self.token.is_cancelled:
                self.cancelled.emit()
            else:
                import traceback
                self.failed.emit(_friendly_error(exc), traceback.format_exc())


class _RenderWorker(QObject):
    finished = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()
    job_updated = Signal(object)

    def __init__(self, container: ServiceContainer, source: SourceInfo, paths: ShortsProjectPaths, transcript, candidates, token: CancellationToken) -> None:
        super().__init__()
        self.container, self.source, self.paths = container, source, paths
        self.transcript, self.candidates, self.token = transcript, candidates, token

    def _persist(self, jobs: list[RenderJob]) -> None:
        manifest_store = ShortsManifestStore(self.paths.manifest)
        manifest = manifest_store.load()
        if manifest:
            merged = {str(item.get("candidate_id")): item for item in manifest.render_jobs}
            merged.update({job.candidate_id: asdict(job) for job in jobs})
            manifest.render_jobs = list(merged.values())
            manifest_store.save(manifest)

    @Slot()
    def run(self) -> None:
        jobs: list[RenderJob] = []
        subtitle_service = SubtitleService()
        try:
            for candidate in self.candidates:
                self.token.raise_if_cancelled()
                try:
                    index = int(candidate.id.rsplit("_", 1)[-1])
                except ValueError:
                    index = len(jobs) + 1
                target = unique_output_path(self.paths.renders, Path(self.source.name).stem, index, candidate.title)
                job = RenderJob(f"render_{candidate.id}", candidate.id, str(target), "rendering", 0.0)
                jobs.append(job)
                self.job_updated.emit(job)
                settings = candidate.subtitle_settings or {"style": "clean", "position": "lower", "size": 58}
                stored_cues = settings.get("cues") or []
                cues = (
                    [SubtitleCue(float(item["start"]), float(item["end"]), str(item["text"])) for item in stored_cues]
                    if stored_cues else
                    subtitle_service.generate(self.transcript, candidate, int(settings.get("maximum", 36)), int(settings.get("lines", 2)))
                )
                ass = self.paths.cache / f"{candidate.id}.render.ass"
                subtitle_service.write(cues, self.paths.cache / f"{candidate.id}.render.srt", ass, settings)

                def update(value: float, current=job):
                    current.progress = round(value, 1)
                    current.speed = self.container.shorts_render.current_speed
                    self.job_updated.emit(current)

                try:
                    self.container.shorts_render.render(self.source, candidate, ass, target, self.token, update)
                    job.status, job.progress = "done", 100.0
                    job.speed = self.container.shorts_render.last_speed
                except Exception as exc:
                    if self.token.is_cancelled:
                        job.status, job.error = "cancelled", "Операция отменена пользователем."
                        self.job_updated.emit(job)
                        self._persist(jobs)
                        self.cancelled.emit()
                        return
                    job.status, job.error = "error", _friendly_error(exc)
                self.job_updated.emit(job)
                self._persist(jobs)
            self.finished.emit(jobs)
        except Exception as exc:
            import traceback
            self.failed.emit(_friendly_error(exc), traceback.format_exc())


class _PreviewRenderWorker(QObject):
    finished = Signal(object)
    failed = Signal(str, str)

    def __init__(self, container, source, paths, transcript, candidate, token) -> None:
        super().__init__()
        self.container, self.source, self.paths, self.transcript = container, source, paths, transcript
        self.candidate, self.token = candidate, token

    @Slot()
    def run(self) -> None:
        try:
            settings = self.candidate.subtitle_settings or {"style": "clean", "position": "lower", "size": 58}
            stored = settings.get("cues") or []
            cues = (
                [SubtitleCue(float(item["start"]), min(5.0, float(item["end"])), str(item["text"])) for item in stored if float(item["start"]) < 5.0]
                if stored else SubtitleService().generate(self.transcript, self.candidate, int(settings.get("maximum", 36)), 2)
            )
            preview = replace(self.candidate, end=min(self.candidate.end, self.candidate.start + 5.0))
            ass = self.paths.cache / f"{self.candidate.id}.preview.ass"
            SubtitleService().write(cues, self.paths.cache / f"{self.candidate.id}.preview.srt", ass, settings)
            target = self.paths.renders / f"{self.candidate.id} [preview 5s].mp4"
            self.container.shorts_render.render(self.source, preview, ass, target, self.token)
            self.finished.emit(target)
        except Exception as exc:
            import traceback
            self.failed.emit(_friendly_error(exc), traceback.format_exc())


def _friendly_error(exc: Exception) -> str:
    from creator_assistant.domain.errors import DiskSpaceError
    if isinstance(exc, DiskSpaceError):
        gib = 1024**3
        return (
            f"Недостаточно места для операции «{exc.operation}» на {exc.path}. "
            f"Требуется {exc.required_bytes / gib:.2f} ГиБ + резерв {exc.reserve_bytes / gib:.2f} ГиБ; "
            f"свободно {exc.free_bytes / gib:.2f} ГиБ."
        )
    return str(exc)


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
        self.transcript = None
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
        self.subtitle_editor = SubtitleEditor()
        self.render_queue = RenderQueue()
        self.workspace.addTab(self.candidate_list, "Кандидаты")
        self.workspace.addTab(self.candidate_editor, "Редактор")
        self.workspace.addTab(self.subtitle_editor, "Субтитры и кадр")
        self.workspace.addTab(self.render_queue, "Рендер")
        splitter.addWidget(left)
        splitter.addWidget(self.workspace)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)
        self.source_panel.choose_file_requested.connect(self.choose_file)
        self.source_panel.choose_project_requested.connect(self.choose_project)
        self.source_panel.choose_output_requested.connect(self.choose_output)
        self.start_button.clicked.connect(self.start_analysis)
        self.cancel_button.clicked.connect(self.cancel_analysis)
        self.candidate_list.selected.connect(self._edit_candidate)
        self.candidate_list.status_changed.connect(self._set_candidate_status)
        self.candidate_editor.status_changed.connect(self._set_candidate_status)
        self.candidate_editor.boundaries_saved.connect(self._save_boundaries)
        self.subtitle_editor.saved.connect(self._subtitle_saved)
        self.subtitle_editor.configuration_changed.connect(self._subtitle_configuration_changed)
        self.subtitle_editor.test_render_requested.connect(self._start_test_render)
        self.render_queue.render_requested.connect(self._start_render)
        self.render_queue.retry_requested.connect(self._start_render)
        self.render_queue.cancel_requested.connect(self.cancel_analysis)

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
        source = Path(selected)
        suggested = self.project_store.suggested_root(source)
        if bool(self.container.settings.get("auto_shorts_project_folder", True)):
            self._begin_probe(source, suggested)
            return
        output = QFileDialog.getExistingDirectory(
            self, "Выберите папку проекта Shorts", str(suggested)
        )
        if output:
            self._begin_probe(source, Path(output))

    @Slot()
    def choose_output(self) -> None:
        if not self.source:
            return
        current = self.paths.root if self.paths else self.project_store.suggested_root(Path(self.source.path))
        output = QFileDialog.getExistingDirectory(self, "Выберите папку проекта Shorts", str(current))
        if output:
            # An explicit choice wins for this source until another source is selected.
            self._begin_probe(Path(self.source.path), Path(output))

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
        capabilities = self.container.shorts_transcription_backend.capabilities()
        self.progress_panel.start_operation(
            capabilities.name,
            str(self.container.settings.get("whisper_model", "—")),
            "GPU/CUDA" if capabilities.cuda else "CPU",
        )
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
        self.transcript = payload["transcript"]
        self.candidates = candidates
        self.candidate_list.set_candidates(candidates)
        if self.paths:
            self.render_queue.set_context(candidates, self.paths.renders)
        result = payload.get("analysis_result")
        if result and result.used_ai:
            detail = f"гибридный локальный AI ({'cache' if result.cache_hit else result.model})"
        elif result and result.fallback_reason:
            detail = f"эвристический fallback: {result.fallback_reason}"
        else:
            detail = "эвристическая оценка"
        self.progress_panel.update_state("Анализ готов", f"Найдено {len(candidates)} кандидатов · {detail}.", 92)

    @Slot(object)
    def _edit_candidate(self, candidate) -> None:
        if not self.paths:
            return
        self.candidate_editor.set_candidate(candidate, self.paths.cache / "analysis_proxy.mp4")
        if self.transcript:
            self.subtitle_editor.set_context(candidate, self.transcript, self.paths)
        self.workspace.setCurrentWidget(self.candidate_editor)

    @Slot(object, str)
    def _set_candidate_status(self, candidate, status: str) -> None:
        if not self.review_service:
            return
        candidate.status = status
        try:
            self.review_service.save(self.candidates)
            self.candidate_list.refresh()
            if self.paths:
                self.render_queue.set_context(self.candidates, self.paths.renders)
            label = "одобрен" if status == "approved" else "отклонён"
            self.progress_panel.update_state("Проверка кандидатов", f"{candidate.id}: {label}. Решение сохранено.", 92)
        except Exception as exc:
            ErrorDialog(str(exc), repr(exc), self).exec()

    @Slot(object)
    def _start_render(self, candidates) -> None:
        if not self.source or not self.paths or not self.transcript or (self._thread and self._thread.isRunning()):
            return
        self._token = CancellationToken()
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_panel.update_state("12. Рендер", f"В очереди {len(candidates)} Short; обработка последовательная.", 95)
        self.progress_panel.start_operation(
            "FFmpeg", "H.264", "NVENC" if self.container.shorts_render.prefer_nvenc else "CPU/libx264"
        )
        thread = QThread(self)
        worker = _RenderWorker(self.container, self.source, self.paths, self.transcript, candidates, self._token)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.job_updated.connect(self.render_queue.update_job)
        worker.finished.connect(self._render_finished)
        worker.failed.connect(self._probe_failed)
        worker.cancelled.connect(self._analysis_cancelled)
        for signal in (worker.finished, worker.failed, worker.cancelled):
            signal.connect(thread.quit)
            signal.connect(worker.deleteLater)
        thread.finished.connect(self._analysis_thread_finished)
        self._thread = thread
        self._thread.worker = worker
        thread.start()

    @Slot(object)
    def _render_finished(self, jobs) -> None:
        done = sum(job.status == "done" for job in jobs)
        failed = sum(job.status == "error" for job in jobs)
        self.progress_panel.update_state("Рендер завершён", f"Готово: {done}; ошибок: {failed}. Encoder последнего файла: {self.container.shorts_render.last_encoder}.", 100 if not failed else 98)

    @Slot(object)
    def _subtitle_saved(self, candidate) -> None:
        if not self.review_service:
            return
        try:
            self.review_service.save(self.candidates)
            self.progress_panel.update_state("Субтитры сохранены", f"Созданы UTF-8 SRT/ASS и настройки вертикального кадра для {candidate.id}.", 94)
        except Exception as exc:
            ErrorDialog(str(exc), repr(exc), self).exec()

    @Slot(object)
    def _subtitle_configuration_changed(self, candidate) -> None:
        if not self.review_service:
            return
        try:
            self.review_service.save(self.candidates)
            self.subtitle_editor.mark_saved()
        except Exception as exc:
            ErrorDialog(str(exc), repr(exc), self).exec()

    @Slot(object)
    def _start_test_render(self, candidate) -> None:
        if not self.source or not self.paths or not self.transcript or (self._thread and self._thread.isRunning()):
            return
        self._token = CancellationToken()
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_panel.update_state("Тестовый рендер", "Рендерятся первые 5 секунд с текущими настройками.", 95)
        thread = QThread(self)
        worker = _PreviewRenderWorker(self.container, self.source, self.paths, self.transcript, candidate, self._token)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._test_render_finished)
        worker.failed.connect(self._probe_failed)
        for signal in (worker.finished, worker.failed):
            signal.connect(thread.quit)
            signal.connect(worker.deleteLater)
        thread.finished.connect(self._analysis_thread_finished)
        self._thread = thread
        self._thread.worker = worker
        thread.start()

    @Slot(object)
    def _test_render_finished(self, target) -> None:
        speed = self.container.shorts_render.last_speed or "—"
        elapsed = self.container.shorts_render.last_elapsed
        self.progress_panel.update_state("Тестовый рендер готов", f"{target} · {elapsed:.1f} с · скорость {speed}", 100)

    @Slot(object, float, float)
    def _save_boundaries(self, candidate, start: float, end: float) -> None:
        if not self.review_service:
            return
        try:
            self.review_service.update_boundaries(candidate, start, end)
            rebuilt = []
            if self.transcript:
                rebuilt = self.subtitle_editor.rebuild_for_boundaries(candidate, self.transcript)
            self.review_service.save(self.candidates)
            self.candidate_list.refresh()
            detail = f"; локальные субтитры пересобраны ({len(rebuilt)} строк)" if rebuilt else ""
            self.progress_panel.update_state(
                "Границы сохранены",
                f"{candidate.id}: {start:.3f}–{end:.3f} сек{detail}.",
                92,
            )
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
        self.progress_panel.finish_operation()
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
        transcript_path = self.paths.analysis / "transcript.json"
        if transcript_path.is_file():
            try:
                self.transcript = self.container.shorts_transcription.load(transcript_path)
            except (OSError, ValueError, TypeError):
                self.transcript = None
        candidates_path = self.paths.analysis / "candidates.json"
        if candidates_path.is_file():
            from creator_assistant.domain.shorts.models import Candidate
            try:
                self.candidates = [Candidate(**item) for item in json.loads(candidates_path.read_text(encoding="utf-8"))]
                self.candidate_list.set_candidates(self.candidates)
                manifest = ShortsManifestStore(self.paths.manifest).load()
                jobs = [RenderJob(**item) for item in (manifest.render_jobs if manifest else [])]
                self.render_queue.set_context(self.candidates, self.paths.renders, jobs)
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
