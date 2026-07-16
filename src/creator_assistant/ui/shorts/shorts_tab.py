from __future__ import annotations

from copy import deepcopy
import json
import hashlib
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
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.services.shorts.subtitle_layout import subtitle_preset_value
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectPaths, ShortsProjectStore
from creator_assistant.services.shorts.source_service import ShortsSourceService
from creator_assistant.services.shorts.title_assets import ShortTitleAssetService
from creator_assistant.services.shorts.render_settings import VerticalRenderSettingsResolver
from creator_assistant.services.shorts.project_template import ProjectShortsTemplate
from creator_assistant.services.shorts.title_service import ShortTitleService
from creator_assistant.ui.shorts.analysis_progress_panel import AnalysisProgressPanel
from creator_assistant.ui.shorts.analysis_settings_panel import AnalysisSettingsPanel
from creator_assistant.ui.shorts.candidate_editor import CandidateEditor
from creator_assistant.ui.shorts.candidate_list import CandidateList
from creator_assistant.ui.shorts.source_panel import SourcePanel
from creator_assistant.ui.shorts.subtitle_editor import SubtitleEditor
from creator_assistant.ui.shorts.render_queue import RenderQueue
from creator_assistant.ui.widgets.error_dialog import ErrorDialog


_ACTIVE_ANALYSIS_PROJECTS: set[str] = set()


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


class _ConfigurationSaveWorker(QObject):
    finished = Signal()
    failed = Signal(str, str)

    def __init__(self, service: CandidateReviewService, candidates) -> None:
        super().__init__()
        self.service = service
        self.candidates = candidates

    @Slot()
    def run(self) -> None:
        try:
            self.service.save(self.candidates)
            self.finished.emit()
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
        lock_key = str(self.paths.root.resolve()).casefold()
        if lock_key in _ACTIVE_ANALYSIS_PROJECTS:
            self.failed.emit("Анализ этого проекта уже выполняется.", "")
            return
        _ACTIVE_ANALYSIS_PROJECTS.add(lock_key)
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
            stage_fingerprints = dict(manifest.analysis_settings.get("stage_fingerprints", {}))
            transcript_hash = hashlib.sha256(transcript.text.encode("utf-8")).hexdigest()
            candidate_config = {
                **asdict(self.candidate_settings),
                "shorts_ai": ai_settings,
                "source_fingerprint": self.source.fingerprint,
                "transcript_hash": transcript_hash,
                "heuristic_version": "shorts-heuristic-v2-content-profile",
                "prompt_version": "shorts-semantic-v1",
                "clustering": {"overlap": 0.62},
            }
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
                scored = [
                    self.container.shorts_candidate_scorer.score(item, scenes, audio_features, self.candidate_settings.content_type)
                    for item in raw_candidates
                ]
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
                from creator_assistant.services.shorts.candidate_ranking import assign_candidate_ranks
                assign_candidate_ranks(candidates)
                candidates_path.write_text(json.dumps([asdict(item) for item in candidates], ensure_ascii=False, indent=2), encoding="utf-8")
                cache.mark_complete("candidates", candidate_config)
                stage_fingerprints = dict(manifest.analysis_settings.get("stage_fingerprints", {}))
                manifest.candidates = [asdict(item) for item in candidates]
                manifest.analysis_settings = {**candidate_config, "stage_fingerprints": stage_fingerprints}
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
            from creator_assistant.services.shorts.candidate_ranking import assign_candidate_ranks
            assign_candidate_ranks(candidates)
            title_assets = ShortTitleAssetService()
            title_result = title_assets.prepare(
                source=self.source,
                paths=self.paths,
                manifest=manifest,
                candidates=candidates,
                transcript=transcript,
                backend=self.container.shorts_semantic_backend,
                ai_settings=ai_settings,
                cache=SemanticCache(self.paths.analysis / "title_cache.json"),
                cancellation=self.token,
                content_type=self.candidate_settings.content_type,
                batch_size=int(ai_settings.get("title_batch_size", 4) or 4),
                progress=self.progress.emit,
            )
            manifest.candidates = [asdict(item) for item in candidates]
            candidates_path.write_text(json.dumps(manifest.candidates, ensure_ascii=False, indent=2), encoding="utf-8")
            store.save(manifest)
            if analysis_result and analysis_result.fallback_reason:
                summary = f"AI недоступен — применён эвристический fallback: {analysis_result.fallback_reason}"
            elif analysis_result and analysis_result.used_ai:
                source = "semantic cache" if analysis_result.cache_hit else analysis_result.model
                summary = f"Гибридная оценка завершена: {source}."
            else:
                summary = "Использована эвристическая оценка."
            self.progress.emit("11. Ожидание пользователя", f"Подготовлено {len(candidates)} непохожих кандидатов. {summary}", 92)
            title_summary = (
                f"AI-заголовки: перевод {'готов' if title_result.translated_ready else 'недоступен'}, "
                f"hooks {title_result.hook_ready}/{len(candidates)}, cache hits {title_result.cache_hits}."
            )
            self.progress.emit("11. Title assets", title_summary, 92)
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
        finally:
            _ACTIVE_ANALYSIS_PROJECTS.discard(lock_key)


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
                job = RenderJob(
                    id=f"render_{candidate.id}", candidate_id=candidate.id,
                    output_path=str(target), status="rendering", progress=0.0,
                    candidate_rank=candidate.candidate_rank,
                )
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
                branding = candidate.branding_settings or {}
                subtitle_service.write(cues, self.paths.cache / f"{candidate.id}.render.srt", ass, settings, branding)
                render_ass = ass if bool(branding.get("show_subtitles", True)) else self.paths.cache / f"{candidate.id}.no_subtitles.ass"

                def update(value: float, current=job):
                    current.progress = round(value, 1)
                    current.speed = self.container.shorts_render.current_speed
                    self.job_updated.emit(current)

                try:
                    self.container.shorts_render.render(self.source, candidate, render_ass, target, self.token, update)
                    job.status, job.progress = "done", 100.0
                    job.speed = self.container.shorts_render.last_speed
                    from creator_assistant.domain.shorts.models import RenderArtifact
                    job.artifact = asdict(RenderArtifact(
                        candidate_id=candidate.id,
                        candidate_rank=candidate.candidate_rank,
                        output_path=str(target),
                    ))
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
            branding = self.candidate.branding_settings or {}
            SubtitleService().write(cues, self.paths.cache / f"{self.candidate.id}.preview.srt", ass, settings, branding)
            render_ass = ass if bool(branding.get("show_subtitles", True)) else self.paths.cache / f"{self.candidate.id}.no_subtitles.ass"
            target = self.paths.renders / f"{self.candidate.id} [preview 5s].mp4"
            self.container.shorts_render.render(self.source, preview, render_ass, target, self.token)
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
        self.title_service = ShortTitleService()
        self.source: Optional[SourceInfo] = None
        self.paths: Optional[ShortsProjectPaths] = None
        self.candidates = []
        self.review_service: Optional[CandidateReviewService] = None
        self.transcript = None
        self._pending_root: Optional[Path] = None
        self._thread: Optional[QThread] = None
        self._configuration_save_thread: Optional[QThread] = None
        self._configuration_save_pending = False
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
        self.subtitle_editor = SubtitleEditor(
            self.container.shorts_semantic_backend,
            subtitle_presets=self.container.settings.get("shorts_subtitle_presets", {}),
        )
        self.render_queue = RenderQueue()
        self.workspace.addTab(self.candidate_list, "Кандидаты")
        self.workspace.addTab(self.candidate_editor, "Редактор")
        self.workspace.addTab(self.subtitle_editor, "Вертикальный редактор")
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
        self.candidate_editor.active_boundary_changed.connect(self._active_boundary_changed)
        self.candidate_editor.boundaries_saved.connect(self._save_boundaries)
        self.subtitle_editor.saved.connect(self._subtitle_saved)
        self.subtitle_editor.configuration_changed.connect(self._subtitle_configuration_changed)
        self.subtitle_editor.test_render_requested.connect(self._start_test_render)
        self.subtitle_editor.defaults_requested.connect(self._save_subtitle_defaults)
        self.subtitle_editor.subtitle_preset_changed.connect(self._subtitle_preset_changed)
        self.subtitle_editor.template_save_requested.connect(self._save_project_template)
        self.subtitle_editor.template_apply_all_requested.connect(self._apply_project_template_all)
        self.subtitle_editor.template_reset_requested.connect(self._reset_candidate_to_template)
        self.render_queue.render_requested.connect(self._start_render)
        self.render_queue.retry_requested.connect(self._start_render)
        self.render_queue.cancel_requested.connect(self.cancel_analysis)
        self.render_queue.selection_changed.connect(self._subtitle_configuration_changed)

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
            self,
            "Выберите готовое видео",
            str(self._initial_video_folder()),
            "Видео (*.mp4 *.mkv *.mov *.m4v *.webm *.avi);;Все файлы (*)",
        )
        if not selected:
            return
        source = Path(selected)
        self._remember_video_folder(source.parent)
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

    def _initial_video_folder(self) -> Path:
        candidates = [
            self.container.settings.get("shorts_default_video_folder", ""),
            self.container.settings.get("shorts_last_video_folder", ""),
            r"E:\YouTube",
            str(Path.home() / "Videos"),
        ]
        for value in candidates:
            path = Path(str(value))
            if path.is_dir():
                return path
        return Path.home()

    def _remember_video_folder(self, folder: Path) -> None:
        if not folder.is_dir():
            return
        settings = dict(self.container.settings)
        settings["shorts_last_video_folder"] = str(folder)
        self.container.save_settings(settings)

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
        self.candidate_editor.release_media()
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
        self._apply_subtitle_defaults(candidate)
        self._apply_branding_defaults(candidate)
        self.candidate_editor.set_candidate(candidate, self.paths.cache / "analysis_proxy.mp4", self.transcript)
        if self.transcript:
            proxy_path = self.paths.cache / "analysis_proxy.mp4"
            proxy_info = None
            if proxy_path.is_file():
                try:
                    proxy_info = self.source_service.probe(proxy_path)
                except Exception:
                    proxy_info = None
            encoder = self.container.shorts_render.last_encoder or ("H.264 NVENC" if self.container.shorts_render.prefer_nvenc else "H.264 libx264")
            if encoder == "h264_nvenc":
                encoder = "H.264 NVENC"
            elif encoder == "libx264":
                encoder = "H.264 libx264"
            self.subtitle_editor.set_technical_context(
                source_info=self.source,
                proxy_info=proxy_info,
                proxy_path=proxy_path,
                render_encoder=encoder,
                ffmpeg_path=self.container.shorts_render.ffmpeg_path,
            )
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

    def _apply_branding_defaults(self, candidate) -> None:
        original = self.title_service.resolve_original_title(self.source, self.paths) if self.source else None
        branding = dict(candidate.branding_settings or {})
        if original and not self.title_service.is_good_title(str(branding.get("original_video_title", ""))):
            branding["original_video_title"] = original.title
            branding["original_video_title_source"] = original.source
        elif original and not str(branding.get("original_video_title_source", "")).strip():
            branding["original_video_title_source"] = original.source
        candidate.branding_settings = branding
        source_author = self._source_author_hint()
        aliases = [source_author, Path(self.source.name).stem if self.source else ""]
        resolved = VerticalRenderSettingsResolver(self.container.settings, ChannelAssetStore()).resolve(
            candidate, source_author=source_author, aliases=aliases,
            project_template=self._project_template(),
        )
        candidate.branding_settings = resolved.branding

    def _source_author_hint(self) -> str:
        if not self.source:
            return ""
        path = Path(self.source.path)
        parts = [part for part in path.parts]
        for marker in ("Beppo", "Myles", "Чак", "Chak", "Kazak"):
            if any(marker.casefold() in part.casefold() for part in parts):
                return marker
        return path.parent.name

    @Slot(object)
    def _start_render(self, candidates) -> None:
        if not self.source or not self.paths or not self.transcript or (self._thread and self._thread.isRunning()):
            return
        for candidate in candidates:
            self._apply_subtitle_defaults(candidate)
            self._apply_branding_defaults(candidate)
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
        if self._configuration_save_thread and self._configuration_save_thread.isRunning():
            self._configuration_save_pending = True
            return
        self._start_configuration_save()

    def _start_configuration_save(self) -> None:
        if not self.review_service:
            return
        self._configuration_save_pending = False
        thread = QThread(self)
        worker = _ConfigurationSaveWorker(self.review_service, deepcopy(self.candidates))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(self._configuration_save_succeeded)
        worker.failed.connect(self._configuration_save_failed)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(lambda t=thread: self._configuration_save_thread_finished(t))
        self._configuration_save_thread = thread
        thread.start()

    @Slot()
    def _configuration_save_succeeded(self) -> None:
        if not self._configuration_save_pending:
            self.subtitle_editor.mark_saved()

    @Slot(str, str)
    def _configuration_save_failed(self, message: str, details: str) -> None:
        ErrorDialog(message, details, self).exec()

    def _configuration_save_thread_finished(self, thread: QThread) -> None:
        if thread is not self._configuration_save_thread:
            thread.deleteLater()
            return
        self._configuration_save_thread = None
        thread.deleteLater()
        if self._configuration_save_pending:
            self._start_configuration_save()

    def _apply_subtitle_defaults(self, candidate) -> None:
        author_resolver = getattr(self, "_source_author_hint", None)
        source_author = author_resolver() if callable(author_resolver) else ""
        source = getattr(self, "source", None)
        template_resolver = getattr(self, "_project_template", None)
        project_template = template_resolver() if callable(template_resolver) else None
        resolved = VerticalRenderSettingsResolver(self.container.settings, ChannelAssetStore()).resolve(
            candidate,
            source_author=source_author,
            aliases=[source_author, Path(source.name).stem if source else ""],
            project_template=project_template,
        )
        candidate.subtitle_settings = resolved.subtitle
        candidate.layout_settings = resolved.layout

    def _project_template(self) -> ProjectShortsTemplate | None:
        if not self.paths:
            return None
        manifest = ShortsManifestStore(self.paths.manifest).load()
        return ProjectShortsTemplate.from_dict(manifest.shorts_template if manifest else None)

    @Slot(object)
    def _save_project_template(self, candidate) -> None:
        if not self.paths or not self.review_service:
            return
        template = ProjectShortsTemplate.from_candidate(candidate, {
            "width": 1080, "height": 1920, "fps_policy": "source",
            "encoder": "h264_nvenc" if self.container.shorts_render.prefer_nvenc else "libx264",
            "audio_codec": "aac",
        })
        manifest_store = ShortsManifestStore(self.paths.manifest)
        manifest = manifest_store.load()
        if not manifest:
            return
        manifest.shorts_template = template.to_dict()
        manifest_store.save(manifest)
        candidate.settings_override = False
        self._apply_template_to_candidate(candidate, template, reset_override=True)
        self.review_service.save(self.candidates)
        self.subtitle_editor.set_context(candidate, self.transcript, self.paths)
        self.progress_panel.update_state("Шаблон Shorts сохранён", "Общий шаблон проекта применяется ко всем кандидатам без ручного override.", 94)

    @Slot()
    def _apply_project_template_all(self) -> None:
        template = self._project_template()
        if not template or not self.review_service:
            QMessageBox.information(self, "Шаблон Shorts", "Сначала сохраните шаблон проекта.")
            return
        has_overrides = any(candidate.settings_override for candidate in self.candidates)
        overwrite = False
        if has_overrides:
            answer = QMessageBox.question(
                self, "Применить шаблон",
                "Перезаписать существующие ручные overrides?\n\nДа — перезаписать. Нет — сохранить ручные настройки.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.No,
            )
            if answer == QMessageBox.Cancel:
                return
            overwrite = answer == QMessageBox.Yes
        for candidate in self.candidates:
            if candidate.settings_override and not overwrite:
                continue
            self._apply_template_to_candidate(candidate, template, reset_override=True)
        self.review_service.save(self.candidates)
        current = self.subtitle_editor.candidate
        if current:
            self.subtitle_editor.set_context(current, self.transcript, self.paths)
        self.progress_panel.update_state("Шаблон применён", f"Обновлено кандидатов: {sum(not item.settings_override for item in self.candidates)}.", 94)

    @Slot(object)
    def _reset_candidate_to_template(self, candidate) -> None:
        template = self._project_template()
        if not template or not self.review_service:
            QMessageBox.information(self, "Шаблон Shorts", "Шаблон проекта ещё не сохранён.")
            return
        self._apply_template_to_candidate(candidate, template, reset_override=True)
        self.review_service.save(self.candidates)
        self.subtitle_editor.set_context(candidate, self.transcript, self.paths)

    def _apply_template_to_candidate(self, candidate, template: ProjectShortsTemplate, *, reset_override: bool) -> None:
        cues = list((candidate.subtitle_settings or {}).get("cues") or [])
        dynamic = {
            key: value for key, value in (candidate.branding_settings or {}).items()
            if key in {
                "source_author", "original_video_title", "original_video_title_source",
                "translated_video_title", "short_hook_title", "short_hook_suggestions", "title_suggestions",
            }
        }
        if reset_override:
            candidate.settings_override = False
        source_author = self._source_author_hint()
        resolved = VerticalRenderSettingsResolver(self.container.settings, ChannelAssetStore()).resolve(
            candidate,
            source_author=source_author,
            aliases=[source_author, Path(self.source.name).stem if self.source else ""],
            project_template=template,
            use_candidate_override=False,
        )
        candidate.subtitle_settings = {**resolved.subtitle, "cues": cues}
        candidate.layout_settings = resolved.layout
        candidate.branding_settings = {**resolved.branding, **dynamic}

    @Slot(str, object)
    def _subtitle_preset_changed(self, name: str, preset) -> None:
        presets = self.container.settings.setdefault("shorts_subtitle_presets", {})
        presets[str(name)] = dict(preset or {})
        defaults = self.container.settings.setdefault("shorts_subtitle_defaults", {})
        if str(defaults.get("style") or "clean") == str(name):
            defaults.update(dict(preset or {}))
        # SettingsStore.save uses a sibling temporary file and os.replace, so
        # the explicit preset action is persistent and atomic without rebuilding
        # the FFmpeg/Ollama service graph.
        self.container.settings_store.save(self.container.settings)
        self.progress_panel.update_state(
            "Пресет субтитров сохранён",
            f"Пользовательские настройки пресета «{name}» будут использоваться новыми Shorts и автопилотом.",
            94,
        )

    @Slot(object)
    def _save_subtitle_defaults(self, candidate) -> None:
        if not self.container:
            return
        settings = dict(self.container.settings)
        subtitle = candidate.subtitle_settings or {}
        layout = candidate.layout_settings or {}
        branding = candidate.branding_settings or {}
        settings["shorts_subtitle_defaults"] = {
            "style": subtitle.get("style", "clean"),
            "position": subtitle.get("position", "lower"),
            "alignment": subtitle.get("alignment", "center"),
            "horizontal_offset": int(subtitle.get("horizontal_offset", 0)),
            "font_family": subtitle.get("font_family", "Segoe UI"),
            "vertical_offset": int(subtitle.get("vertical_offset", 0)),
            "size": int(subtitle.get("size", 58)),
            "maximum": int(subtitle.get("maximum", 36)),
            "lines": int(subtitle.get("lines", 2)),
            "outline": int(subtitle.get("outline", 3)),
            "shadow": int(subtitle.get("shadow", 1)),
            "background": bool(subtitle.get("background", False)),
            "safe_margin": int(subtitle.get("safe_margin", 120)),
            "auto_above_banner": bool(subtitle.get("auto_above_banner", True)),
            "line_anchor_mode": str(subtitle.get("line_anchor_mode", "first_line_fixed")),
            "banner_gap": int(subtitle.get("banner_gap", 15)),
            "layout_mode": layout.get("mode", "center_crop"),
            "foreground_scale": int(layout.get("foreground_scale", 100)),
            "crop_center": int(layout.get("crop_center", 50)),
            "background_color": layout.get("background_color", "black"),
        }
        settings["shorts_branding_defaults"] = {
            key: value
            for key, value in {
                key: branding.get(key)
                for key in (
                    "preset", "show_subtitles", "show_title", "final_title_text", "title_style", "title_size", "title_bold",
                    "title_color", "title_outline", "title_shadow", "title_background", "title_y", "title_max_lines",
                    "title_alignment", "title_offset_x", "title_font_family", "use_subtitle_font_for_title",
                    "show_channel_card", "channel_profile_id", "banner_scale", "banner_anchor",
                    "banner_fit_mode", "banner_offset_x", "banner_offset_y", "banner_opacity", "safe_margin",
                )
            }.items()
            if value is not None
        }
        self.container.save_settings(settings)
        self.progress_panel.update_state("Настройки Shorts по умолчанию сохранены", "Новые кандидаты будут использовать текущий стиль, позицию и кадр.", 94)

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
        self._apply_candidate_boundaries(candidate, start, end, candidate.selected_boundary_variant_id or "main", "Границы сохранены")

    @Slot(object, float, float, str)
    def _active_boundary_changed(self, candidate, start: float, end: float, variant_id: str) -> None:
        self._apply_candidate_boundaries(candidate, start, end, variant_id, "Выбран вариант границ")

    def _apply_candidate_boundaries(self, candidate, start: float, end: float, variant_id: str, title: str) -> None:
        if not self.review_service:
            return
        try:
            self.review_service.update_boundaries(candidate, start, end)
            candidate.selected_boundary_variant_id = variant_id
            if self.transcript:
                candidate.text = self._transcript_for_range(start, end)
                ShortTitleAssetService().mark_stale(candidate, self.transcript)
            rebuilt = []
            if self.transcript:
                rebuilt = self.subtitle_editor.rebuild_for_boundaries(candidate, self.transcript)
            self.review_service.save(self.candidates)
            self.candidate_list.refresh()
            if self.paths:
                self.render_queue.set_context(self.candidates, self.paths.renders)
                self.candidate_editor.set_candidate(candidate, self.paths.cache / "analysis_proxy.mp4", self.transcript)
            if self.transcript and self.paths:
                self.subtitle_editor.set_context(candidate, self.transcript, self.paths)
            detail = f"; локальные субтитры пересобраны ({len(rebuilt)} строк)" if rebuilt else ""
            self.progress_panel.update_state(
                title,
                f"{candidate.id}: {start:.3f}–{end:.3f} сек{detail}.",
                92,
            )
        except Exception as exc:
            ErrorDialog(str(exc), repr(exc), self).exec()

    def _transcript_for_range(self, start: float, end: float) -> str:
        if not self.transcript:
            return ""
        parts = [
            segment.text.strip()
            for segment in self.transcript.segments
            if segment.end > start and segment.start < end and segment.text.strip()
        ]
        return " ".join(parts)

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
                from creator_assistant.services.shorts.candidate_ranking import assign_candidate_ranks
                ranks_changed = assign_candidate_ranks(self.candidates)
                if ranks_changed:
                    candidates_path.write_text(
                        json.dumps([asdict(item) for item in self.candidates], ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                self.candidate_list.set_candidates(self.candidates)
                manifest = ShortsManifestStore(self.paths.manifest).load()
                if manifest and ranks_changed:
                    manifest.candidates = [asdict(item) for item in self.candidates]
                    ShortsManifestStore(self.paths.manifest).save(manifest)
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
