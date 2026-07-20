from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.automation.models import (
    AutomationIssue, AutomationJob, AutomationShort, AutomationShortStatus, RenderArtifact,
)
from creator_assistant.domain.shorts.models import Candidate, SourceInfo, SubtitleCue
from creator_assistant.services.automation.quality_control import AutomationQualityControl
from creator_assistant.services.automation.selection import AutomaticCandidateSelector, unique_automation_shorts
from creator_assistant.services.shorts.candidate_ranking import assign_candidate_ranks
from creator_assistant.services.shorts.candidate_generator import CandidateSettings
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore
from creator_assistant.services.shorts.manifest import ShortsManifestStore
from creator_assistant.services.shorts.semantic_cache import SemanticCache
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectStore
from creator_assistant.services.shorts.source_service import ShortsSourceService
from creator_assistant.services.shorts.subtitle_service import SubtitleService
from creator_assistant.services.shorts.title_assets import ShortTitleAssetService
from creator_assistant.services.shorts.render_settings import VerticalRenderSettingsResolver
from creator_assistant.services.shorts.render_state import RenderStateStore
from creator_assistant.services.shorts.project_template import (
    ProjectShortsTemplate, TRANSLATED_SOURCE_TITLE, composition_snapshot_hash, render_identity,
)
from creator_assistant.services.shorts.transcription_service import TranscriptionService


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
        job.resume_data["selection_details"] = []
        summaries = []
        for source in job.sources:
            values = job.resume_data.get("analysis_candidates", {}).get(source.source_id, [])
            candidates = [Candidate(**item) for item in values]
            selected, summary, details = self.selector.evaluate(candidates, job.selection_settings)
            source.shorts_selected = len(selected)
            summaries.append(f"{Path(source.path).name}: {summary}")
            for detail in details:
                detail["source_id"] = source.source_id
            job.resume_data.setdefault("selection_details", []).extend(details)
            for candidate in selected:
                job.shorts.append(AutomationShort(
                    short_id=f"{source.source_id}-{candidate.id}", source_id=source.source_id,
                    candidate_id=candidate.id, candidate_rank=candidate.candidate_rank,
                    start=candidate.start, end=candidate.end,
                    score=candidate.final_score or candidate.score,
                    candidate_data=asdict(candidate),
                ))
        job.shorts = unique_automation_shorts(job.shorts)
        for source in job.sources:
            source.shorts_selected = sum(item.source_id == source.source_id for item in job.shorts)
        job.resume_data["render_queue"] = self._render_queue_details(job.shorts)
        job.result.summary = "\n".join(summaries)

    def prepare_titles(self, job: AutomationJob) -> None:
        for short in job.shorts:
            source = self._source(job, short.source_id)
            project = Path(source.shorts_project_path)
            manifest = ShortsManifestStore(project / "shorts_manifest.json").load()
            assets = manifest.title_assets if manifest else {}
            candidate = Candidate(**short.candidate_data)
            translated = str(candidate.branding_settings.get("translated_video_title") or assets.get("translated_title") or "").strip()
            original = str(candidate.branding_settings.get("original_video_title") or assets.get("original_title") or "").strip()
            short.title = translated or original
            candidate.branding_settings["final_title_text"] = short.title
            candidate.branding_settings["title_mode"] = TRANSLATED_SOURCE_TITLE
            candidate.branding_settings["show_title"] = bool(short.title)
            short.candidate_data = asdict(candidate)
            short.status = AutomationShortStatus.PREPARING.value

    def prepare_composition(self, job: AutomationJob) -> None:
        job.shorts = unique_automation_shorts(job.shorts)
        resolver = VerticalRenderSettingsResolver(self.container.settings, self.assets)
        template = self._job_template(job, resolver)
        for short in job.shorts:
            source = self._source(job, short.source_id)
            candidate = Candidate(**short.candidate_data)
            author_values = [source.source_author, source.folder_author, *[parent.name for parent in list(Path(source.path).parents)[:4]]]
            resolved = resolver.resolve(
                candidate,
                channel_id=source.channel_id,
                source_author=source.source_author,
                aliases=[value for value in author_values if value],
                selected_profile_id=job.profile.channel_profile_id,
                selected_subtitle_preset=job.profile.subtitle_preset,
                profile_layout=job.profile.composition_preset,
                job_layout=job.composition_preset,
                project_template=template,
            )
            short.subtitle_settings = resolved.subtitle
            short.layout_settings = resolved.layout
            short.branding_settings = resolved.branding
            short.profile_id = resolved.profile_id
            short.composition_snapshot_hash = job.composition_snapshot_hash
            if candidate.settings_override:
                short.composition_snapshot_hash = composition_snapshot_hash({
                    "job_snapshot": job.composition_snapshot_hash,
                    "subtitle": {key: value for key, value in resolved.subtitle.items() if key != "cues"},
                    "layout": resolved.layout,
                    "branding": {
                        key: value for key, value in resolved.branding.items()
                        if key not in {"final_title_text", "channel_banner_path"}
                    },
                })
            self._rebuild_subtitles(source, candidate, short)
            short.render_key = render_identity(
                self._source_fingerprint(source), short.candidate_id, short.start, short.end,
                short.composition_snapshot_hash,
            )
            short.candidate_data = asdict(candidate)
            if short.status != AutomationShortStatus.NEEDS_REVIEW.value:
                short.status = AutomationShortStatus.READY.value
        job.resume_data["render_queue"] = self._render_queue_details(job.shorts)

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
        job.shorts = unique_automation_shorts(job.shorts)
        job.resume_data["render_queue"] = self._render_queue_details(job.shorts)
        save()
        rendered_keys: set[str] = set()
        for short in job.shorts:
            if short.status not in {
                AutomationShortStatus.READY.value,
                AutomationShortStatus.RENDERING.value,
                AutomationShortStatus.RENDERED.value,
                AutomationShortStatus.APPROVED.value,
                AutomationShortStatus.SCHEDULED.value,
            }:
                continue
            if short.render_key and short.render_key in rendered_keys:
                continue
            if short.render_key:
                rendered_keys.add(short.render_key)
            if short.artifact and short.artifact.validated and short.status in {
                AutomationShortStatus.RENDERED.value, AutomationShortStatus.APPROVED.value,
                AutomationShortStatus.SCHEDULED.value,
            }:
                continue
            source_job = self._source(job, short.source_id)
            project = Path(source_job.shorts_project_path)
            paths = ShortsProjectStore.paths(project)
            state_store = RenderStateStore(project)
            state_store.migrate_legacy()
            source = SourceInfo.from_dict(json.loads((paths.analysis / "source_info.json").read_text(encoding="utf-8")))
            candidate = Candidate(**short.candidate_data)
            candidate.subtitle_settings = dict(short.subtitle_settings)
            candidate.branding_settings = dict(short.branding_settings)
            candidate.layout_settings = dict(short.layout_settings)
            candidate.title = short.title
            cues = [SubtitleCue(float(item["start"]), float(item["end"]), str(item["text"])) for item in short.subtitle_settings.get("cues", [])]
            ass = paths.cache / f"{candidate.id}.autopilot.ass"
            SubtitleService().write(cues, paths.cache / f"{candidate.id}.autopilot.srt", ass, candidate.subtitle_settings, candidate.branding_settings)
            rank = short.candidate_rank or len(job.shorts)
            interval = f"{short.start:.3f}-{short.end:.3f}"
            target = paths.renders / f"[{rank:02d}] {candidate.id} [{interval}] [autopilot].mp4"
            if target.exists():
                previous = state_store.load(short.render_key)
                if (
                    previous.get("render_key") == short.render_key
                    and Path(str(previous.get("output_path") or target)) == target
                ):
                    short.artifact = RenderArtifact(candidate.id, candidate.candidate_rank, str(target))
                    artifact, issues = self.qc.probe_render(
                        self.container.runner, self.container.paths.get("ffprobe", "ffprobe.exe"),
                        short, source.fps,
                    )
                    short.artifact = artifact
                    short.issues.extend(issues)
                    short.status = AutomationShortStatus.RENDERED.value if artifact.validated else AutomationShortStatus.NEEDS_REVIEW.value
                    self._render_platform_variants(job, short, source, candidate, ass, paths, state_store)
                    save()
                    continue
                target = paths.renders / f"[{rank:02d}] {candidate.id} [{interval}] [autopilot {job.job_id[-6:]}].mp4"
            short.status = AutomationShortStatus.RENDERING.value
            save()
            self.container.shorts_render.render(source, candidate, ass, target, _NeverCancelled())
            short.artifact = RenderArtifact(candidate.id, candidate.candidate_rank, str(target))
            artifact, issues = self.qc.probe_render(self.container.runner, self.container.paths.get("ffprobe", "ffprobe.exe"), short, source.fps)
            short.artifact = artifact
            state_store.save(short.render_key, {
                "render_key": short.render_key,
                "job_id": job.job_id,
                "output_path": str(target),
                "composition_snapshot_hash": short.composition_snapshot_hash,
                "candidate_id": short.candidate_id,
                "start": short.start,
                "end": short.end,
            })
            if artifact.validated:
                issues.extend(self.qc.inspect_frames(
                    self.container.runner, self.container.paths.get("ffmpeg", "ffmpeg.exe"), short,
                ))
                artifact.validated = not any(item.critical for item in issues)
            short.issues.extend(issues)
            job.issues.extend(issues)
            short.status = AutomationShortStatus.NEEDS_REVIEW.value if any(item.critical for item in issues) else AutomationShortStatus.RENDERED.value
            self._render_platform_variants(job, short, source, candidate, ass, paths, state_store)
            save()

    def _render_platform_variants(self, job, short, source, candidate, ass, paths, state_store) -> None:
        if not short.artifact or not short.artifact.validated:
            return
        base = short.artifact.output_path
        if "youtube" in job.platforms:
            short.platform_artifacts["youtube"] = base
        if "tiktok" not in job.platforms:
            return
        if not bool(short.branding_settings.get("show_channel_card", False)):
            short.platform_artifacts["tiktok"] = base
            return
        variant = Candidate(**short.candidate_data)
        variant.subtitle_settings = dict(short.subtitle_settings)
        variant.layout_settings = dict(short.layout_settings)
        variant.branding_settings = dict(short.branding_settings)
        variant.branding_settings.update({"show_channel_card": False, "channel_profile_id": "", "channel_banner_path": ""})
        variant.title = short.title
        target = paths.renders / f"{candidate.id}_tiktok.mp4"
        state_key = f"{short.render_key}-tiktok"
        previous = state_store.load(state_key)
        if not (target.is_file() and previous.get("render_key") == state_key):
            self.container.shorts_render.render(source, variant, ass, target, _NeverCancelled())
            state_store.save(state_key, {
                "render_key": state_key, "job_id": job.job_id, "output_path": str(target),
                "platform": "tiktok", "candidate_id": short.candidate_id,
            })
        probe = deepcopy(short)
        probe.artifact = RenderArtifact(candidate.id, candidate.candidate_rank, str(target))
        artifact, issues = self.qc.probe_render(self.container.runner, self.container.paths.get("ffprobe", "ffprobe.exe"), probe, source.fps)
        short.issues.extend(issues)
        job.issues.extend(issues)
        if artifact.validated:
            short.platform_artifacts["tiktok"] = str(target)
        else:
            short.status = AutomationShortStatus.NEEDS_REVIEW.value

    @staticmethod
    def _render_queue_details(shorts) -> list[dict]:
        return [
            {
                "rank": item.candidate_rank,
                "short_id": item.short_id,
                "candidate_id": item.candidate_id,
                "source_id": item.source_id,
                "start": item.start,
                "end": item.end,
                "output_filename": (
                    Path(item.artifact.output_path).name
                    if item.artifact else f"[{(item.candidate_rank or index):02d}] {item.candidate_id} [{item.start:.3f}-{item.end:.3f}] [autopilot].mp4"
                ),
                "composition_snapshot_hash": item.composition_snapshot_hash,
                "render_key": item.render_key,
                "subtitle_status": item.subtitle_status,
            }
            for index, item in enumerate(shorts, 1)
        ]

    def _job_template(self, job: AutomationJob, resolver: VerticalRenderSettingsResolver) -> ProjectShortsTemplate:
        if job.composition_snapshot:
            stored = ProjectShortsTemplate.from_dict(job.composition_snapshot.get("template"))
            if stored:
                if not job.composition_snapshot_hash:
                    job.composition_snapshot_hash = composition_snapshot_hash(job.composition_snapshot)
                return stored
        if not job.shorts:
            template = ProjectShortsTemplate()
            job.composition_snapshot = {"schema_version": 1, "template": template.to_dict(), "selected_profile_id": job.profile.channel_profile_id}
            job.composition_snapshot_hash = composition_snapshot_hash(job.composition_snapshot)
            return template
        first_short = job.shorts[0]
        first_source = self._source(job, first_short.source_id)
        manifest = ShortsManifestStore(Path(first_source.shorts_project_path) / "shorts_manifest.json").load()
        stored = ProjectShortsTemplate.from_dict(manifest.shorts_template if manifest else None)
        if stored:
            template = stored
        else:
            candidate = Candidate(**first_short.candidate_data)
            resolved = resolver.resolve(
                candidate,
                channel_id=first_source.channel_id,
                source_author=first_source.source_author,
                aliases=[first_source.source_author, first_source.folder_author],
                selected_profile_id=job.profile.channel_profile_id,
                selected_subtitle_preset=job.profile.subtitle_preset,
                profile_layout=job.profile.composition_preset,
                job_layout=job.composition_preset,
                use_candidate_override=False,
            )
            candidate.subtitle_settings = resolved.subtitle
            candidate.layout_settings = resolved.layout
            candidate.branding_settings = resolved.branding
            template = ProjectShortsTemplate.from_candidate(candidate)
        snapshot = {
            "schema_version": 1,
            "template": template.to_dict(),
            "selected_profile_id": job.profile.channel_profile_id,
        }
        job.composition_snapshot = snapshot
        job.composition_snapshot_hash = composition_snapshot_hash(snapshot)
        return template

    def _rebuild_subtitles(self, source, candidate: Candidate, short: AutomationShort) -> None:
        transcript_path = Path(source.shorts_project_path) / "Analysis" / "transcript.json"
        if not transcript_path.is_file():
            short.subtitle_status = "missing"
            return
        try:
            transcript = TranscriptionService.load(transcript_path)
        except (OSError, ValueError, TypeError, KeyError):
            short.subtitle_status = "missing"
            return
        service = SubtitleService()
        adjusted_end = service.suggested_candidate_end(transcript, candidate)
        if adjusted_end > candidate.end + 0.001:
            candidate.end = adjusted_end
            short.end = adjusted_end
        track = service.track(transcript, candidate)
        candidate.last_aligned_word_end = track.last_word_end
        candidate.boundary_tail_padding_ms = max(
            0, round((candidate.end - track.last_word_end) * 1000)
        ) if track.last_word_end is not None else 0
        cues = service.generate(
            transcript, candidate,
            int(short.subtitle_settings.get("maximum", 36)),
            int(short.subtitle_settings.get("lines", 2)),
        )
        problems = service.validate(cues, candidate.duration, short.subtitle_settings)
        if problems:
            cues = service.generate(
                transcript, candidate,
                int(short.subtitle_settings.get("maximum", 36)),
                int(short.subtitle_settings.get("lines", 2)),
            )
            problems = service.validate(cues, candidate.duration, short.subtitle_settings)
        short.subtitle_settings["cues"] = [asdict(cue) for cue in cues]
        if problems:
            short.subtitle_status = "invalid"
            short.status = AutomationShortStatus.NEEDS_REVIEW.value
            short.issues.append(AutomationIssue(
                "invalid_subtitles", "; ".join(problems), True, "prepare_composition", short.short_id,
            ))
        else:
            short.subtitle_status = "ready"

    @staticmethod
    def _source_fingerprint(source) -> str:
        path = Path(source.shorts_project_path) / "Analysis" / "source_info.json"
        if path.is_file():
            try:
                return str(json.loads(path.read_text(encoding="utf-8")).get("fingerprint") or source.source_id)
            except (OSError, ValueError, TypeError):
                pass
        return source.source_id

    @staticmethod
    def _source(job: AutomationJob, source_id: str):
        return next(item for item in job.sources if item.source_id == source_id)


class _NeverCancelled:
    is_cancelled = False

    @staticmethod
    def raise_if_cancelled() -> None:
        return None
