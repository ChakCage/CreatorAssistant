from __future__ import annotations

import json
import hashlib
from dataclasses import asdict
from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.automation.models import (
    AutomationIssue, AutomationJob, AutomationShort, AutomationShortStatus, RenderArtifact,
)
from creator_assistant.domain.shorts.models import Candidate, SourceInfo, SubtitleCue
from creator_assistant.services.automation.quality_control import AutomationQualityControl
from creator_assistant.services.automation.selection import AutomaticCandidateSelector
from creator_assistant.services.shorts.candidate_ranking import assign_candidate_ranks
from creator_assistant.services.shorts.candidate_generator import CandidateSettings
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.manifest import ShortsManifestStore
from creator_assistant.services.shorts.semantic_cache import SemanticCache
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore
from creator_assistant.services.shorts.source_service import ShortsSourceService
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.services.shorts.title_assets import ShortTitleAssetService


class ExistingShortsAutomationPipeline:
    """Automation adapter around the same manifest, subtitle and render services used by Shorts UI."""

    def __init__(self, container) -> None:
        self.container = container
        self.selector = AutomaticCandidateSelector()
        self.qc = AutomationQualityControl()
        self.assets = ChannelAssetStore()

    def validate(self, job: AutomationJob) -> None:
        if not job.sources:
            raise ValueError("Не добавлены исходные видео")
        for source in job.sources:
            path = Path(source.path)
            if not path.is_file():
                raise FileNotFoundError(path)
            project = Path(source.shorts_project_path) if source.shorts_project_path else ShortsProjectStore.suggested_root(path)
            source.shorts_project_path = str(project)
            digest = hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:10]
            source.source_id = source.source_id or f"source-{digest}"
            source.status = "validated"

    def analyze(self, job: AutomationJob) -> None:
        cache = {}
        total = 0
        for source in job.sources:
            project = Path(source.shorts_project_path)
            candidates_path = project / "Analysis" / "candidates.json"
            if not candidates_path.is_file():
                self._analyze_new_source(job, source)
            candidates = [Candidate(**item) for item in json.loads(candidates_path.read_text(encoding="utf-8"))]
            ranks_changed = assign_candidate_ranks(candidates)
            if ranks_changed:
                candidates_path.write_text(json.dumps([asdict(item) for item in candidates], ensure_ascii=False, indent=2), encoding="utf-8")
                manifest_store = ShortsManifestStore(project / "shorts_manifest.json")
                manifest = manifest_store.load()
                if manifest:
                    manifest.candidates = [asdict(item) for item in candidates]
                    manifest_store.save(manifest)
            source.candidates_found = len(candidates)
            source.status = "analyzed"
            total += len(candidates)
            cache[source.source_id] = [asdict(item) for item in candidates]
        job.resume_data["analysis_candidates"] = cache
        job.resume_data["candidates_found"] = total

    def _analyze_new_source(self, job: AutomationJob, source_job) -> None:
        token = CancellationToken()
        source_path = Path(source_job.path)
        source = ShortsSourceService(self.container.runner, self.container.paths.get("ffprobe", "")).probe(source_path)
        store = ShortsProjectStore()
        paths = store.open_or_create(Path(source_job.shorts_project_path), source)
        source_job.shorts_project_path = str(paths.root)
        (paths.analysis / "source_info.json").write_text(json.dumps(asdict(source), ensure_ascii=False, indent=2), encoding="utf-8")
        proxy = self.container.shorts_proxy.create(source_path, paths.analysis / "analysis_proxy.mp4", token)
        audio = self.container.shorts_audio.extract(source_path, paths.analysis / "transcription_audio.flac", token)
        transcript = self.container.shorts_transcription.transcribe(audio, paths.analysis, token)
        scenes_path = paths.analysis / "scenes.json"
        scenes = self.container.shorts_scenes.detect(proxy, source.duration, scenes_path, token)
        scenes = self.container.shorts_scenes.generate_thumbnails(proxy, scenes, paths.thumbnails, token)
        self.container.shorts_scenes.save(scenes_path, scenes)
        audio_features = self.container.shorts_audio_activity.analyse(
            audio, source.duration, paths.analysis / "audio_features.json", token,
        )
        maximum_per_source = max(1, int(job.selection_settings.get("maximum_per_source", 10)))
        settings = CandidateSettings(
            minimum=float(job.selection_settings.get("minimum_duration", 25)),
            maximum=float(job.selection_settings.get("maximum_duration", 75)),
            desired=float(job.selection_settings.get("desired_duration", 45)),
            count=max(15, maximum_per_source * 4),
            content_type=str(job.analysis_settings.get("content_type", "gaming")),
        )
        generated = self.container.shorts_candidate_generator.generate(transcript, scenes, audio_features, settings)
        scored = [
            self.container.shorts_candidate_scorer.score(item, scenes, audio_features, settings.content_type)
            for item in generated
        ]
        ai_settings = dict(self.container.settings.get("shorts_ai", {}))
        result = self.container.shorts_hybrid_analyzer.analyse(
            scored, transcript, scenes, audio_features, self.container.shorts_semantic_backend,
            ai_settings, SemanticCache(paths.analysis / "semantic_cache.json"), token,
            content_type=settings.content_type, requested_count=max(15, maximum_per_source * 3),
        )
        candidates = result.candidates
        assign_candidate_ranks(candidates)
        manifest_store = ShortsManifestStore(paths.manifest)
        manifest = manifest_store.load()
        if not manifest:
            raise RuntimeError("Не удалось создать Shorts manifest")
        ShortTitleAssetService().prepare(
            source=source, paths=paths, manifest=manifest, candidates=candidates,
            transcript=transcript, backend=self.container.shorts_semantic_backend,
            ai_settings=ai_settings, cache=SemanticCache(paths.analysis / "title_cache.json"),
            cancellation=token, content_type=settings.content_type,
        )
        manifest.candidates = [asdict(item) for item in candidates]
        manifest.analysis_settings = asdict(settings)
        manifest.transcription_backend = transcript.backend
        manifest.whisper_model = transcript.model
        manifest_store.save(manifest)
        (paths.analysis / "candidates.json").write_text(
            json.dumps(manifest.candidates, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def select(self, job: AutomationJob) -> None:
        job.shorts.clear()
        summaries = []
        for source in job.sources:
            values = job.resume_data.get("analysis_candidates", {}).get(source.source_id, [])
            candidates = [Candidate(**item) for item in values]
            selected, summary = self.selector.select(candidates, job.selection_settings)
            source.shorts_selected = len(selected)
            summaries.append(f"{Path(source.path).name}: {summary}")
            for candidate in selected:
                job.shorts.append(AutomationShort(
                    short_id=f"{source.source_id}-{candidate.id}", source_id=source.source_id,
                    candidate_id=candidate.id, candidate_rank=candidate.candidate_rank,
                    start=candidate.start, end=candidate.end,
                    score=candidate.final_score or candidate.score,
                    candidate_data=asdict(candidate),
                ))
        job.result.summary = "\n".join(summaries)

    def prepare_titles(self, job: AutomationJob) -> None:
        for short in job.shorts:
            source = self._source(job, short.source_id)
            project = Path(source.shorts_project_path)
            manifest = ShortsManifestStore(project / "shorts_manifest.json").load()
            assets = manifest.title_assets if manifest else {}
            candidate = Candidate(**short.candidate_data)
            translated = str(candidate.branding_settings.get("translated_video_title") or assets.get("translated_title") or "").strip()
            hook = str(candidate.branding_settings.get("short_hook_title") or candidate.title or "").strip()
            short.title = hook or translated
            candidate.branding_settings["final_title_text"] = translated or short.title
            candidate.branding_settings["show_title"] = bool(translated or short.title)
            short.candidate_data = asdict(candidate)
            short.status = AutomationShortStatus.PREPARING.value

    def prepare_composition(self, job: AutomationJob) -> None:
        presets = self.container.settings.get("shorts_subtitle_presets", {})
        subtitle_defaults = self.container.settings.get("shorts_subtitle_defaults", {})
        branding_defaults = self.container.settings.get("shorts_branding_defaults", {})
        links = self.container.settings.get("shorts_channel_profile_links", {})
        for short in job.shorts:
            source = self._source(job, short.source_id)
            candidate = Candidate(**short.candidate_data)
            style = str(candidate.subtitle_settings.get("style") or job.profile.subtitle_preset or "clean")
            preset = dict(presets.get(style, {})) if isinstance(presets, dict) else {}
            short.subtitle_settings = {**subtitle_defaults, **preset, **candidate.subtitle_settings}
            short.layout_settings = {
                "mode": "blur_background", "foreground_scale": int(subtitle_defaults.get("foreground_scale", 100)),
                "crop_center": int(subtitle_defaults.get("crop_center", 50)),
                **job.profile.composition_preset, **job.composition_preset, **candidate.layout_settings,
            }
            author_values = [source.source_author, source.folder_author, *[parent.name for parent in list(Path(source.path).parents)[:4]]]
            saved_id = job.profile.channel_profile_id
            if not saved_id and isinstance(links, dict):
                saved_id = next((str(links.get(value.casefold(), "")) for value in author_values if value and links.get(value.casefold())), "")
            profile = self.assets.resolve(
                channel_id=source.channel_id, saved_profile_id=saved_id,
                source_author=source.source_author, aliases=[value for value in author_values if value],
            ) if job.profile.auto_detect or saved_id else None
            banner = self.assets.banner_path(profile)
            profile_branding = {
                "channel_profile_id": profile.id if profile else "",
                "channel_banner_path": str(banner or ""),
                "show_channel_card": bool(banner),
                "banner_scale": profile.default_banner_scale if profile else 100,
                "banner_offset_x": profile.default_banner_offset_x if profile else 0,
                "banner_offset_y": profile.default_banner_offset_y if profile else 0,
                "banner_opacity": profile.default_banner_opacity if profile else 100,
                "banner_anchor": profile.default_banner_anchor if profile else "bottom_center",
                "banner_fit_mode": profile.default_banner_fit_mode if profile else "contain",
            }
            short.branding_settings = {**branding_defaults, **profile_branding, **candidate.branding_settings}
            short.profile_id = str(short.branding_settings.get("channel_profile_id", ""))
            short.candidate_data = asdict(candidate)
            short.status = AutomationShortStatus.READY.value

    def quality_check(self, job: AutomationJob) -> None:
        for short in job.shorts:
            source = self._source(job, short.source_id)
            transcript_exists = (Path(source.shorts_project_path) / "Analysis" / "transcript.json").is_file()
            short.issues = self.qc.pre_render(short, transcript_exists=transcript_exists, profile_required=job.profile.banner_required)
            source_info_path = Path(source.shorts_project_path) / "Analysis" / "source_info.json"
            if source_info_path.is_file():
                source_info = SourceInfo.from_dict(json.loads(source_info_path.read_text(encoding="utf-8")))
                if not source_info.video_codec or not source_info.audio_codec:
                    short.issues.append(AutomationIssue(
                        "missing_source_stream", "В исходнике отсутствует видео- или аудиопоток",
                        True, "pre_render", short.short_id,
                    ))
            job.issues.extend(short.issues)
            if any(issue.critical for issue in short.issues):
                short.status = AutomationShortStatus.NEEDS_REVIEW.value

    def render(self, job: AutomationJob, save) -> None:
        for short in job.shorts:
            if short.status == AutomationShortStatus.NEEDS_REVIEW.value:
                continue
            if short.artifact and short.artifact.validated and short.status in {
                AutomationShortStatus.RENDERED.value, AutomationShortStatus.APPROVED.value,
                AutomationShortStatus.SCHEDULED.value,
            }:
                continue
            source_job = self._source(job, short.source_id)
            project = Path(source_job.shorts_project_path)
            paths = ShortsProjectStore.paths(project)
            source = SourceInfo.from_dict(json.loads((paths.analysis / "source_info.json").read_text(encoding="utf-8")))
            candidate = Candidate(**short.candidate_data)
            candidate.subtitle_settings = dict(short.subtitle_settings)
            candidate.branding_settings = dict(short.branding_settings)
            candidate.layout_settings = dict(short.layout_settings)
            candidate.title = short.title
            cues = [SubtitleCue(float(item["start"]), float(item["end"]), str(item["text"])) for item in short.subtitle_settings.get("cues", [])]
            ass = paths.cache / f"{candidate.id}.autopilot.ass"
            SubtitleService().write(cues, paths.cache / f"{candidate.id}.autopilot.srt", ass, candidate.subtitle_settings, candidate.branding_settings)
            target = paths.renders / f"{candidate.id} [autopilot].mp4"
            if target.exists():
                target = paths.renders / f"{candidate.id} [autopilot {job.job_id[-6:]}].mp4"
            short.status = AutomationShortStatus.RENDERING.value
            save()
            self.container.shorts_render.render(source, candidate, ass, target, _NeverCancelled())
            short.artifact = RenderArtifact(candidate.id, candidate.candidate_rank, str(target))
            artifact, issues = self.qc.probe_render(self.container.runner, self.container.paths.get("ffprobe", "ffprobe.exe"), short, source.fps)
            short.artifact = artifact
            if artifact.validated:
                issues.extend(self.qc.inspect_frames(
                    self.container.runner, self.container.paths.get("ffmpeg", "ffmpeg.exe"), short,
                ))
                artifact.validated = not any(item.critical for item in issues)
            short.issues.extend(issues)
            job.issues.extend(issues)
            short.status = AutomationShortStatus.NEEDS_REVIEW.value if any(item.critical for item in issues) else AutomationShortStatus.RENDERED.value
            save()

    @staticmethod
    def _source(job: AutomationJob, source_id: str):
        return next(item for item in job.sources if item.source_id == source_id)


class _NeverCancelled:
    is_cancelled = False

    @staticmethod
    def raise_if_cancelled() -> None:
        return None
