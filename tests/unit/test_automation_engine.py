from pathlib import Path

from creator_assistant.domain.automation.models import (
    AutomationMode, AutomationShort, AutomationShortStatus, AutomationStatus, RenderArtifact,
)
from creator_assistant.infrastructure.automation_job_store import AutomationJobStore
from creator_assistant.services.automation.engine import AutomationEngine


class FakePipeline:
    def __init__(self, critical_first=False, fail_once_at=""):
        self.calls = []
        self.critical_first = critical_first
        self.fail_once_at = fail_once_at

    def _call(self, name):
        self.calls.append(name)
        if self.fail_once_at == name:
            self.fail_once_at = ""
            raise RuntimeError(f"failed {name}")

    def validate(self, job): self._call("validate")
    def analyze(self, job): self._call("analyze")

    def select(self, job):
        self._call("select")
        job.shorts = [
            AutomationShort("one", "source", "candidate_1", 1, 0, 40, 95),
            AutomationShort("two", "source", "candidate_2", 2, 80, 125, 90),
        ]

    def prepare_titles(self, job): self._call("titles")
    def prepare_composition(self, job): self._call("composition")

    def quality_check(self, job):
        self._call("qc")
        if self.critical_first:
            job.shorts[0].status = AutomationShortStatus.NEEDS_REVIEW.value

    def render(self, job, save):
        self._call("render")
        for short in job.shorts:
            if short.status == AutomationShortStatus.NEEDS_REVIEW.value:
                continue
            short.artifact = RenderArtifact(short.candidate_id, short.candidate_rank, f"{short.short_id}.mp4", validated=True)
            short.status = AutomationShortStatus.RENDERED.value
            save()


def engine(tmp_path, pipeline=None):
    return AutomationEngine(pipeline or FakePipeline(), AutomationJobStore(tmp_path / "jobs"))


def test_job_for_one_and_multiple_sources_is_persisted(tmp_path):
    service = engine(tmp_path)
    one = service.create_job(["one.mp4"])
    many = service.create_job(["one.mp4", "two.mp4"])
    assert len(service.store.load(one.job_id).sources) == 1
    assert len(service.store.load(many.job_id).sources) == 2
    assert {item.job_id for item in service.store.list()} == {one.job_id, many.job_id}


def test_approval_required_stops_before_schedule(tmp_path):
    service = engine(tmp_path)
    job = service.create_job(["one.mp4"], mode=AutomationMode.APPROVAL_REQUIRED.value)
    result = service.run(job)
    assert result.status == AutomationStatus.WAITING_FOR_APPROVAL.value
    assert result.result.rendered_count == 2
    assert result.result.publishing_plan is None
    planned = service.approve_and_schedule(job.job_id)
    assert planned.status == AutomationStatus.SCHEDULED.value
    assert len(planned.result.publishing_plan.slots) == 2


def test_full_autopilot_schedules_without_approval_and_continues_after_critical_short(tmp_path):
    service = engine(tmp_path, FakePipeline(critical_first=True))
    job = service.create_job(["one.mp4"], mode=AutomationMode.FULL_AUTOPILOT.value)
    result = service.run(job)
    assert result.status == AutomationStatus.SCHEDULED.value
    assert result.shorts[0].status == AutomationShortStatus.NEEDS_REVIEW.value
    assert result.shorts[1].status == AutomationShortStatus.SCHEDULED.value
    assert result.result.needs_review_count == 1
    assert len(result.result.publishing_plan.slots) == 1


def test_failed_job_can_resume_from_failed_stage(tmp_path):
    pipeline = FakePipeline(fail_once_at="titles")
    service = engine(tmp_path, pipeline)
    job = service.create_job(["one.mp4"])
    failed = service.run(job)
    assert failed.status == AutomationStatus.FAILED.value
    resumed = service.resume(job.job_id)
    assert resumed.status == AutomationStatus.WAITING_FOR_APPROVAL.value
    assert pipeline.calls.count("validate") == 1
    assert pipeline.calls.count("titles") == 2


def test_pause_cancel_and_unfinished_discovery(tmp_path):
    service = engine(tmp_path)
    job = service.create_job(["one.mp4"])
    paused = service.pause(job.job_id)
    assert paused.status == AutomationStatus.PAUSED.value
    assert service.store.unfinished()[0].job_id == job.job_id
    cancelled = service.cancel(job.job_id)
    assert cancelled.status == AutomationStatus.CANCELLED.value
    assert service.store.unfinished() == []


def test_atomic_store_leaves_no_temporary_file(tmp_path):
    service = engine(tmp_path)
    job = service.create_job(["one.mp4"])
    assert service.store.path_for(job.job_id).is_file()
    assert not list(Path(tmp_path).rglob("*.tmp"))
