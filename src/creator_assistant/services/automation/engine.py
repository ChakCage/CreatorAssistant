from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Iterable, Protocol

from creator_assistant.domain.automation.models import (
    AutomationJob, AutomationJobSource, AutomationMode, AutomationProfile,
    AutomationShortStatus, AutomationStatus,
)
from creator_assistant.infrastructure.automation_job_store import AutomationJobStore
from creator_assistant.services.automation.schedule import SchedulePlanner
from creator_assistant.services.shorts.render_state import RenderStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutomationPipeline(Protocol):
    def validate(self, job: AutomationJob) -> None: ...
    def analyze(self, job: AutomationJob) -> None: ...
    def select(self, job: AutomationJob) -> None: ...
    def prepare_titles(self, job: AutomationJob) -> None: ...
    def prepare_composition(self, job: AutomationJob) -> None: ...
    def quality_check(self, job: AutomationJob) -> None: ...
    def render(self, job: AutomationJob, save: Callable[[], None]) -> None: ...


class AutomationEngine:
    """Resumable state machine shared by UI and headless entry points."""

    def __init__(self, pipeline: AutomationPipeline, store: AutomationJobStore | None = None, planner: SchedulePlanner | None = None) -> None:
        self.pipeline = pipeline
        self.store = store or AutomationJobStore()
        self.planner = planner or SchedulePlanner()
        self._pause_requested: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._lock = RLock()

    def create_job(
        self, sources: Iterable[str], *, mode: str = AutomationMode.APPROVAL_REQUIRED.value,
        profile: AutomationProfile | None = None, analysis_settings: dict | None = None,
        selection_settings: dict | None = None, composition_preset: dict | None = None,
        schedule_settings: dict | None = None, platforms: list[str] | None = None,
    ) -> AutomationJob:
        now = _now()
        job = AutomationJob(
            job_id=f"autopilot-{uuid.uuid4().hex[:12]}",
            sources=[AutomationJobSource(str(Path(value))) for value in sources],
            mode=mode, profile=profile or AutomationProfile(),
            analysis_settings=dict(analysis_settings or {}),
            composition_preset=dict(composition_preset or {}),
            schedule_settings=dict(schedule_settings or {}),
            platforms=list(platforms or ["youtube"]),
            created_at=now, updated_at=now,
        )
        if selection_settings:
            job.selection_settings.update(selection_settings)
        self._save(job)
        return job

    def run(self, job_or_id: AutomationJob | str) -> AutomationJob:
        with self._lock:
            job = job_or_id if isinstance(job_or_id, AutomationJob) else self._required(job_or_id)
            if job.status in {AutomationStatus.COMPLETED.value, AutomationStatus.CANCELLED.value, AutomationStatus.FAILED.value, AutomationStatus.WAITING_FOR_APPROVAL.value, AutomationStatus.SCHEDULED.value}:
                return job
            if job.status == AutomationStatus.PAUSED.value:
                job.status = str(job.resume_data.pop("paused_from", AutomationStatus.VALIDATING.value))
            if not job.started_at:
                job.started_at = _now()
            try:
                stages = [
                    (AutomationStatus.VALIDATING, self.pipeline.validate, 8),
                    (AutomationStatus.ANALYZING, self.pipeline.analyze, 35),
                    (AutomationStatus.SELECTING, self.pipeline.select, 48),
                    (AutomationStatus.PREPARING_TITLES, self.pipeline.prepare_titles, 58),
                    (AutomationStatus.PREPARING_COMPOSITION, self.pipeline.prepare_composition, 68),
                    (AutomationStatus.QUALITY_CHECK, self.pipeline.quality_check, 74),
                ]
                start = self._stage_index(job.status, stages)
                for index in range(start, len(stages)):
                    status, operation, progress = stages[index]
                    if self._stop_if_requested(job, status.value):
                        return job
                    job.status = status.value
                    self._save(job)
                    operation(job)
                    job.progress = progress
                    job.resume_data["completed_stage"] = status.value
                    self._save(job)
                if self._stop_if_requested(job, AutomationStatus.RENDERING.value):
                    return job
                job.status = AutomationStatus.RENDERING.value
                self._save(job)
                self.pipeline.render(job, lambda: self._save(job))
                job.progress = 90
                self._save(job)
                if job.mode == AutomationMode.APPROVAL_REQUIRED.value:
                    job.status = AutomationStatus.WAITING_FOR_APPROVAL.value
                else:
                    self._schedule(job)
                self._save(job)
                return job
            except Exception as exc:
                job.resume_data["failed_stage"] = job.status
                job.status = AutomationStatus.FAILED.value
                job.error = str(exc)
                job.finished_at = _now()
                self._save(job)
                return job

    def approve_and_schedule(self, job_id: str) -> AutomationJob:
        job = self._required(job_id)
        if job.status != AutomationStatus.WAITING_FOR_APPROVAL.value:
            raise ValueError("Job is not waiting for approval")
        for short in job.shorts:
            if short.status == AutomationShortStatus.RENDERED.value:
                short.status = AutomationShortStatus.APPROVED.value
        self._schedule(job)
        self._save(job)
        return job

    def pause(self, job_id: str) -> AutomationJob:
        self._pause_requested.add(job_id)
        job = self._required(job_id)
        if job.status not in {AutomationStatus.PAUSED.value, AutomationStatus.CANCELLED.value}:
            job.resume_data["paused_from"] = job.status
            job.status = AutomationStatus.PAUSED.value
            self._save(job)
        return job

    def resume(self, job_id: str) -> AutomationJob:
        self._pause_requested.discard(job_id)
        job = self._required(job_id)
        if job.status == AutomationStatus.FAILED.value:
            job.status = str(job.resume_data.pop("failed_stage", AutomationStatus.VALIDATING.value))
            job.error = ""
            job.finished_at = ""
            self._save(job)
        return self.run(job_id)

    def cancel(self, job_id: str) -> AutomationJob:
        self._cancel_requested.add(job_id)
        job = self._required(job_id)
        job.status = AutomationStatus.CANCELLED.value
        job.finished_at = _now()
        self._save(job)
        return job

    def delete_job(self, job_id: str) -> bool:
        job = self._required(job_id)
        allowed = {
            AutomationStatus.CREATED.value, AutomationStatus.CANCELLED.value,
            AutomationStatus.FAILED.value, AutomationStatus.COMPLETED.value,
            AutomationStatus.WAITING_FOR_APPROVAL.value, AutomationStatus.SCHEDULED.value,
        }
        if job.status not in allowed:
            raise ValueError("Сначала отмените активное задание")
        for source in job.sources:
            if source.shorts_project_path:
                RenderStateStore(Path(source.shorts_project_path)).delete_job(job_id)
        return self.store.delete(job_id)

    def _schedule(self, job: AutomationJob) -> None:
        eligible = [item for item in job.shorts if item.status in {AutomationShortStatus.RENDERED.value, AutomationShortStatus.APPROVED.value} and item.artifact and item.artifact.validated]
        order = str(job.schedule_settings.get("order", "rank"))
        if order == "score":
            eligible.sort(key=lambda item: (-item.score, item.candidate_rank or 10**9))
        elif order == "source":
            eligible.sort(key=lambda item: (item.source_id, item.candidate_rank or 10**9))
        else:
            eligible.sort(key=lambda item: (item.candidate_rank or 10**9, -item.score))
        occupied = {str(value) for value in job.schedule_settings.get("occupied_slots", [])}
        plan = self.planner.build([item.short_id for item in eligible], job.schedule_settings, job.platforms, occupied)
        job.result.publishing_plan = plan
        for short in eligible:
            short.status = AutomationShortStatus.SCHEDULED.value
        job.status = AutomationStatus.SCHEDULED.value
        job.progress = 100
        job.finished_at = _now()

    @staticmethod
    def _stage_index(status: str, stages: list) -> int:
        if status in {AutomationStatus.CREATED.value, AutomationStatus.PAUSED.value}:
            return 0
        values = [item[0].value for item in stages]
        if status in values:
            return values.index(status)
        if status == AutomationStatus.RENDERING.value:
            return len(stages)
        return 0

    def _stop_if_requested(self, job: AutomationJob, next_status: str) -> bool:
        if job.job_id in self._cancel_requested:
            job.status = AutomationStatus.CANCELLED.value
            job.finished_at = _now()
            self._save(job)
            return True
        if job.job_id in self._pause_requested:
            job.resume_data["paused_from"] = next_status
            job.status = AutomationStatus.PAUSED.value
            self._save(job)
            return True
        return False

    def _required(self, job_id: str) -> AutomationJob:
        job = self.store.load(job_id)
        if not job:
            raise KeyError(f"Automation job not found: {job_id}")
        return job

    def _save(self, job: AutomationJob) -> None:
        job.updated_at = _now()
        job.result.selected_count = len(job.shorts)
        job.result.rendered_count = sum(
            item.artifact is not None
            and (item.artifact.validated or Path(item.artifact.output_path).is_file())
            for item in job.shorts
        )
        job.result.needs_review_count = sum(item.status == AutomationShortStatus.NEEDS_REVIEW.value for item in job.shorts)
        self.store.save(job)
