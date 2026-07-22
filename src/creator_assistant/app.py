from __future__ import annotations

from copy import deepcopy
import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from creator_assistant.infrastructure.dependency_detector import (
    SOURCE_SAVED,
    SETTING_KEYS,
    DependencyDetector,
)
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.infrastructure.audio_separator_runtime import AudioSeparatorRuntimeManager
from creator_assistant.infrastructure.logging_setup import configure_logging
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.infrastructure.project_index import ProjectIndex
from creator_assistant.infrastructure.settings_store import SettingsStore
from creator_assistant.infrastructure.updater import YtDlpUpdater
from creator_assistant.infrastructure.windows_paths import NamingTemplates
from creator_assistant.domain.youtube_auth import YtDlpAuthContext
from creator_assistant.services.ffmpeg_service import FfmpegService
from creator_assistant.services.media_validation_service import MediaValidationService
from creator_assistant.services.metadata_service import MetadataService
from creator_assistant.services.metadata_request_controller import MetadataRequestController
from creator_assistant.services.project_service import ProjectService
from creator_assistant.services.reaper_service import ReaperService
from creator_assistant.services.vegas_service import VegasService
from creator_assistant.services.stem_separation.audio_separator_backend import AudioSeparatorBackend
from creator_assistant.services.stem_separation.uvr_manual_fallback import UvrManualFallbackBackend
from creator_assistant.services.thumbnail_service import ThumbnailService
from creator_assistant.services.yt_dlp_service import YtDlpService
from creator_assistant.services.storage_service import StoragePolicy, StorageService, default_temp_root
from creator_assistant.services.settings_service import SettingsService
from creator_assistant.services.shorts.audio_extract_service import TranscriptionAudioService
from creator_assistant.services.shorts.audio_activity_service import AudioActivityService
from creator_assistant.services.shorts.candidate_generator import CandidateGenerator
from creator_assistant.services.shorts.candidate_scorer import HeuristicCandidateScorer
from creator_assistant.services.shorts.duplicate_filter import DuplicateFilter
from creator_assistant.services.shorts.hybrid_analyzer import HybridCandidateAnalyzer
from creator_assistant.services.shorts.proxy_service import AnalysisProxyService
from creator_assistant.services.shorts.render_service import ShortsRenderService
from creator_assistant.services.shorts.semantic_backend import DisabledSemanticScorer, OllamaSemanticScorer
from creator_assistant.services.shorts.scene_detection_service import SceneDetectionService
from creator_assistant.services.shorts.transcription.disabled import DisabledTranscriptionBackend
from creator_assistant.services.shorts.transcription.existing_whisper import ExistingWhisperBackend
from creator_assistant.services.shorts.transcription.faster_whisper import FasterWhisperBackend
from creator_assistant.services.shorts.transcription.managed_whisper import ManagedWhisperBackend
from creator_assistant.services.shorts.transcription.runtime_manager import WhisperRuntimeManager
from creator_assistant.services.shorts.transcription_service import TranscriptionService
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.global_template_library import GlobalShortsTemplateLibrary
from creator_assistant.product import AppEdition, DEVELOPER_AI_MODEL, Feature, FeatureRegistry, current_edition


class ServiceContainer:
    def __init__(self, settings_store: SettingsStore = None) -> None:
        self.edition = current_edition()
        self.features = FeatureRegistry
        self.settings_store = settings_store or SettingsStore()
        self.first_run = not self.settings_store.path.exists()
        initial_settings: Dict[str, Any] = self.settings_store.load()
        if self.edition is AppEdition.DEVELOPER:
            ai = initial_settings.setdefault("shorts_ai", {})
            ai.update({"enabled": True, "backend": "ollama", "model": DEVELOPER_AI_MODEL, "strict_model": True, "fallback": False})
        if not initial_settings.get("temp_root"):
            initial_settings["temp_root"] = str(default_temp_root(Path(initial_settings.get("youtube_root", r"E:\YouTube"))))
        self.logger = configure_logging()
        self.settings_service = SettingsService(self.settings_store, initial_settings, self.logger)
        if FeatureRegistry.is_available(Feature.YOUTUBE_PUBLISHING, self.edition):
            store_module = importlib.import_module("creator_assistant.infrastructure." + "publishing_store")
            credentials_module = importlib.import_module("creator_assistant.infrastructure." + "credential_store")
            accounts_module = importlib.import_module("creator_assistant.services." + "publishing.accounts")
            manager_module = importlib.import_module("creator_assistant.services." + "publishing.manager")
            oauth_module = importlib.import_module("creator_assistant.services." + "publishing.oauth")
            self.publishing_store = store_module.PublishingStore()
            self.publishing_credentials = credentials_module.WindowsCredentialStore()
            self.publishing_oauth = oauth_module.OAuthService(self.publishing_credentials)
            self.publishing_accounts = accounts_module.PublishingAccountService(self.publishing_store, self.publishing_credentials, self.publishing_oauth)
            self.publishing_manager = manager_module.PublishingManager(self.publishing_store, self.publishing_credentials)
            self.publishing_agent = manager_module.BackgroundPublishingAgent(self.publishing_manager)
        if FeatureRegistry.is_available(Feature.LICENSING, self.edition):
            licensing_module = importlib.import_module("creator_assistant.services." + "licensing")
            self.licensing = licensing_module.LicenseService()
        self.global_shorts_templates = GlobalShortsTemplateLibrary()
        self.youtube_auth = YtDlpAuthContext.from_settings(self.settings)
        self.runner = ProcessRunner(self.logger)
        self.detector = DependencyDetector(self.runner)
        self.updater = YtDlpUpdater(self.runner)
        self.dependency_resolutions = self.detector.discover(self.settings)
        self.detector.apply_to_settings(self.settings, self.dependency_resolutions)
        if self.edition is AppEdition.COMMERCIAL:
            setup_module = importlib.import_module("creator_assistant.services." + "commercial_setup")
            self.commercial_setup = setup_module.CommercialSetupService(self.settings, self.dependency_resolutions)
            selected = self.commercial_setup.selected_profile()
            ai = self.settings.setdefault("shorts_ai", {})
            if str(ai.get("model", "")) not in {"qwen3:14b", "qwen3.6:35b-a3b"}:
                self.commercial_setup.apply_profile(selected.id)
            else:
                # Commercial model selection is strict and never silently falls back.
                ai.update({"enabled": True, "backend": "ollama", "strict_model": True, "fallback": False})
                profile = self.commercial_setup.profile_for_model(str(ai["model"]))
                ai["model_profile"] = profile.id
                self.settings.setdefault("commercial_setup", {})["model_profile"] = profile.id
            self.first_run = not bool(self.settings.get("commercial_setup", {}).get("completed", False))
        # Persist safe schema migrations (notably stale, non-permanent YouTube auth state).
        self.settings_store.save(self.settings)
        self.settings_service.mark_current_as_persisted()
        self.rebuild()
        if hasattr(self, "publishing_agent") and os.environ.get("CREATOR_ASSISTANT_AGENT_MODE") != "1":
            self.publishing_agent.start()

    @property
    def settings(self) -> Dict[str, Any]:
        return self.settings_service.settings

    def rebuild(self) -> None:
        self.youtube_auth = YtDlpAuthContext.from_settings(self.settings)
        self.dependency_resolutions = self.detector.discover(self.settings)
        if self.edition is AppEdition.COMMERCIAL and hasattr(self, "commercial_setup"):
            self.commercial_setup.resolutions = self.dependency_resolutions
        self.paths = {key: item.path for key, item in self.dependency_resolutions.items()}
        naming_data = self.settings.get("naming", {})
        naming = NamingTemplates(
            maximum=naming_data.get("maximum", "{title} [MAX {height}p]"),
            proxy=naming_data.get("proxy", "{title} [{proxy_height}p]"),
            audio=naming_data.get("audio", "{title} [Audio]"),
            instrumental=naming_data.get("instrumental", "{title} [Instrumental]"),
            preview=naming_data.get("preview", "Preview"),
        )
        yt_path = self.paths.get("yt_dlp", "")
        ffmpeg_path = self.paths.get("ffmpeg", "")
        ffprobe_path = self.paths.get("ffprobe", "")
        self.metadata = MetadataService(self.runner, yt_path, self.youtube_auth)
        yt_version = getattr(self.dependency_resolutions.get("yt_dlp"), "version", "")
        self.metadata_controller = MetadataRequestController(self.metadata, logger=self.logger, yt_dlp_version=yt_version)
        yt_service = YtDlpService(self.runner, yt_path, ffmpeg_path, auth=self.youtube_auth)
        ffmpeg_service = FfmpegService(
            self.runner,
            ffmpeg_path,
            bool(self.settings.get("prefer_nvenc", True)) and self.detector.nvenc_available(ffmpeg_path),
            ffprobe_path,
        )
        validator = MediaValidationService(self.runner, ffprobe_path)
        model_path = self.detector.uvr_model_path(self.paths.get("uvr", ""))
        self.audio_separator_runtime = AudioSeparatorRuntimeManager(self.runner, model_path)
        direct = AudioSeparatorBackend(
            self.runner,
            self.audio_separator_runtime,
            ffprobe_path,
            ffmpeg_path,
            bool(self.settings.get("use_gpu", True)),
        )
        manual = UvrManualFallbackBackend(Path(self.paths.get("uvr", "")))
        self.projects = ProjectService(
            yt_service,
            ffmpeg_service,
            validator,
            ThumbnailService(),
            ReaperService(),
            direct,
            manual,
            JobStore(),
            naming,
            self.settings.get("reaper_initial_audio", "original"),
            VegasService(self.paths.get("vegas", "")),
            StorageService(StoragePolicy(
                reserve_bytes=int(float(self.settings.get("disk_reserve_gb", 5)) * 1024**3),
                separator_safety_factor=float(self.settings.get("separator_temp_safety_factor", 4.0)),
            )),
            project_index=ProjectIndex(),
            project_roots=self.settings.get("author_presets", []),
        )
        self.whisper_runtime = WhisperRuntimeManager(self.runner)
        backend_choice = str(self.settings.get("whisper_backend", "auto"))
        existing_backend = ExistingWhisperBackend(self.runner, self.settings)
        if backend_choice == "disabled":
            self.shorts_transcription_backend = DisabledTranscriptionBackend()
        elif backend_choice == "managed":
            self.shorts_transcription_backend = ManagedWhisperBackend(
                self.runner, self.settings, self.whisper_runtime
            )
        elif backend_choice == "faster":
            self.shorts_transcription_backend = FasterWhisperBackend(self.runner, self.settings)
        elif backend_choice == "auto":
            if existing_backend.capabilities().available:
                self.shorts_transcription_backend = existing_backend
            elif self.whisper_runtime.is_ready():
                self.shorts_transcription_backend = ManagedWhisperBackend(
                    self.runner, self.settings, self.whisper_runtime
                )
            else:
                self.shorts_transcription_backend = DisabledTranscriptionBackend()
        else:
            self.shorts_transcription_backend = existing_backend
        self.shorts_transcription = TranscriptionService(self.shorts_transcription_backend)
        self.shorts_proxy = AnalysisProxyService(
            self.runner,
            ffmpeg_path,
            bool(self.settings.get("prefer_nvenc", True))
            and self.detector.nvenc_available(ffmpeg_path),
        )
        self.shorts_audio = TranscriptionAudioService(self.runner, ffmpeg_path)
        self.shorts_scenes = SceneDetectionService(self.runner, ffmpeg_path)
        self.shorts_audio_activity = AudioActivityService(self.runner, ffmpeg_path)
        self.shorts_candidate_generator = CandidateGenerator()
        self.shorts_candidate_scorer = HeuristicCandidateScorer()
        ai_settings = self.settings.get("shorts_ai", {})
        ai_context = int(ai_settings.get("context_length", 32768))
        if str(ai_settings.get("context_mode", "auto")) == "auto":
            ai_context = max(32768, ai_context)
        if ai_settings.get("enabled", False) and ai_settings.get("backend", "ollama") == "ollama":
            self.shorts_semantic_backend = OllamaSemanticScorer(
                endpoint=str(ai_settings.get("endpoint", "http://127.0.0.1:11434")),
                model=str(ai_settings.get("model", "qwen3.6:35b-a3b")),
                timeout=float(ai_settings.get("timeout", 1800)),
                keep_alive=str(ai_settings.get("keep_alive", "60m")),
                connection_timeout=float(ai_settings.get("connection_timeout", 15)),
                warmup_timeout=float(ai_settings.get("warmup_timeout", 900)),
                context_length=ai_context,
            )
        else:
            self.shorts_semantic_backend = DisabledSemanticScorer()
        self.shorts_duplicate_filter = DuplicateFilter()
        self.shorts_hybrid_analyzer = HybridCandidateAnalyzer(self.shorts_duplicate_filter)
        self.shorts_render = ShortsRenderService(
            self.runner, ffmpeg_path, ffprobe_path,
            bool(self.settings.get("prefer_nvenc", True)) and self.detector.nvenc_available(ffmpeg_path),
            int(float(self.settings.get("disk_reserve_gb", 5)) * 1024**3),
        )
        self.channel_assets = ChannelAssetStore(
            ffprobe_path=ffprobe_path,
            ffmpeg_path=ffmpeg_path,
            project_roots=[Path(self.settings.get("youtube_root", r"E:\YouTube"))],
        )
        if FeatureRegistry.is_available(Feature.AUTOPILOT, self.edition):
            store_module = importlib.import_module("creator_assistant.infrastructure." + "automation_job_store")
            engine_module = importlib.import_module("creator_assistant.services." + "automation.engine")
            pipeline_module = importlib.import_module("creator_assistant.services." + "automation.shorts_pipeline")
            self.automation_engine = engine_module.AutomationEngine(
                pipeline_module.ExistingShortsAutomationPipeline(self), store_module.AutomationJobStore()
            )

    def save_settings(self, settings: Dict[str, Any], started_at: float | None = None) -> None:
        candidate = deepcopy(settings)
        changed_keys = self.settings_service.changed_top_level_keys(candidate)
        previous = self.settings_service.persisted_snapshot()
        sources = candidate.setdefault("dependency_sources", {})
        for key, setting_key in SETTING_KEYS.items():
            if str(candidate.get(setting_key, "")) != str(previous.get(setting_key, "")):
                if candidate.get(setting_key):
                    sources[key] = SOURCE_SAVED
                else:
                    sources.pop(key, None)
        self.settings_service.update(candidate, started_at=started_at)

        # Proxy quality is a live UI preference. It must never trigger dependency
        # discovery, FFmpeg/NVENC probes, a worker, or a service graph rebuild.
        if changed_keys <= {"reaper_proxy_height"}:
            self.logger.info(
                "settings saved through fast path: changed=%s ui_latency_ms=%s",
                sorted(changed_keys),
                self.settings_service.last_timing.get("ui_latency_ms"),
            )
            return

        self.auto_detect_dependencies(rebuild=False)
        self.rebuild()

    def auto_detect_dependencies(self, rebuild: bool = True):
        resolutions = self.detector.discover(self.settings)
        changed = self.detector.apply_to_settings(self.settings, resolutions)
        self.dependency_resolutions = resolutions
        if changed:
            self.settings_store.save(self.settings)
            self.settings_service.mark_current_as_persisted()
        if rebuild:
            self.rebuild()
        return resolutions

    def enable_youtube_auth(
        self,
        mode: str,
        browser: str = "",
        profile: str = "",
        cookies_file: str = "",
        persist: bool = False,
    ) -> None:
        """Enable auth for the current interaction; persist only after an explicit permanent choice."""
        self.youtube_auth.enable_one_time(mode, browser, profile, cookies_file)
        if not persist:
            return
        access = self.settings.setdefault("youtube_access", {})
        access.update({
            "mode": mode,
            "browser": browser,
            "browser_profile": profile.strip(),
            "cookies_file": cookies_file.strip(),
            "always_use": True,
            "schema_version": 2,
        })
        self.settings_store.save(self.settings)

    def reset_youtube_access(self) -> None:
        self.settings["youtube_access"] = {
            "mode": "automatic",
            "browser": "",
            "browser_profile": "",
            "cookies_file": "",
            "always_use": False,
            "schema_version": 2,
        }
        self.settings_store.save(self.settings)
        self.youtube_auth = YtDlpAuthContext.from_settings(self.settings)
        self.rebuild()
