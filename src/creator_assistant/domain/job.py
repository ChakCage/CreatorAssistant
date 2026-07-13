from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import List

from .errors import JobCancelledError, ValidationError
from .stages import JobStage, ORDERED_STAGES


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise JobCancelledError("Операция отменена пользователем.")

    def wait(self, seconds: float) -> None:
        if self._event.wait(max(0.0, seconds)):
            self.raise_if_cancelled()


@dataclass
class JobState:
    status: JobStatus = JobStatus.PENDING
    current_stage: JobStage = JobStage.VALIDATE_URL
    completed_stages: List[JobStage] = field(default_factory=list)

    def start(self) -> None:
        if self.status not in (JobStatus.PENDING, JobStatus.FAILED, JobStatus.CANCELLED):
            raise ValidationError("Это задание уже запущено или завершено.")
        self.status = JobStatus.RUNNING

    def advance(self, stage: JobStage) -> None:
        if self.status != JobStatus.RUNNING:
            raise ValidationError("Нельзя изменить этап неактивного задания.")
        if self.completed_stages:
            previous = ORDERED_STAGES.index(self.completed_stages[-1])
            current = ORDERED_STAGES.index(stage)
            if current < previous:
                raise ValidationError("Некорректный переход этапов задания.")
        self.current_stage = stage

    def complete_stage(self, stage: JobStage) -> None:
        self.advance(stage)
        if stage not in self.completed_stages:
            self.completed_stages.append(stage)
        if stage == JobStage.DONE:
            self.status = JobStatus.COMPLETED

    def cancel(self) -> None:
        if self.status == JobStatus.RUNNING:
            self.status = JobStatus.CANCELLED

    def fail(self) -> None:
        if self.status == JobStatus.RUNNING:
            self.status = JobStatus.FAILED
