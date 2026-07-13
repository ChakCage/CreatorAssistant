from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .stages import JobStage


STAGE_WEIGHTS: Dict[JobStage, float] = {
    JobStage.VALIDATE_URL: 1.0,
    JobStage.FETCH_METADATA: 3.0,
    JobStage.CHECK_DEPENDENCIES: 2.0,
    JobStage.CHECK_DISK_SPACE: 1.0,
    JobStage.CREATE_STRUCTURE: 1.0,
    JobStage.DOWNLOAD_THUMBNAIL: 2.0,
    JobStage.DOWNLOAD_MAXIMUM: 30.0,
    JobStage.CREATE_PROXY: 20.0,
    JobStage.DOWNLOAD_AUDIO: 10.0,
    JobStage.SEPARATE_STEMS: 25.0,
    JobStage.CREATE_REAPER: 3.0,
    JobStage.CREATE_VEGAS: 3.0,
    JobStage.FINAL_VALIDATION: 2.0,
    JobStage.DONE: 1.0,
}


class WeightedProgressTracker:
    """Monotonic weighted progress for only the stages selected by the user."""

    def __init__(self, active_stages: Iterable[JobStage]) -> None:
        self.active: List[JobStage] = list(dict.fromkeys(active_stages))
        self.completed: List[JobStage] = []
        self.current: Optional[JobStage] = None
        self.current_percent: Optional[float] = 0.0
        self.last_overall = 0.0
        self.total_weight = sum(STAGE_WEIGHTS.get(stage, 1.0) for stage in self.active) or 1.0

    def mark_complete(self, stage: JobStage) -> float:
        if stage in self.active and stage not in self.completed:
            self.completed.append(stage)
        if self.current == stage:
            self.current_percent = 100.0
        return self._calculate()

    def update(self, stage: JobStage, percent: Optional[float]) -> float:
        if stage not in self.active:
            return self.last_overall
        if self.current and self.current != stage and self.current not in self.completed:
            self.completed.append(self.current)
        self.current = stage
        self.current_percent = None if percent is None else max(0.0, min(100.0, float(percent)))
        return self._calculate()

    def position(self, stage: JobStage) -> tuple[int, int]:
        try:
            return self.active.index(stage) + 1, len(self.active)
        except ValueError:
            return 0, len(self.active)

    def _calculate(self) -> float:
        completed_weight = sum(STAGE_WEIGHTS.get(stage, 1.0) for stage in self.completed if stage in self.active)
        current_weight = 0.0
        if self.current in self.active and self.current not in self.completed and self.current_percent is not None:
            current_weight = STAGE_WEIGHTS.get(self.current, 1.0) * self.current_percent / 100.0
        calculated = min(100.0, (completed_weight + current_weight) / self.total_weight * 100.0)
        self.last_overall = max(self.last_overall, calculated)
        if set(self.completed) >= set(self.active):
            self.last_overall = 100.0
        return self.last_overall


def format_bytes(value: Optional[float]) -> str:
    if value is None:
        return ""
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if abs(amount) < 1024.0 or unit == units[-1]:
            break
        amount /= 1024.0
    digits = 0 if unit == "Б" else 1
    return f"{amount:.{digits}f} {unit}".replace(".", ",")


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs} с"
    return f"{secs} с"
