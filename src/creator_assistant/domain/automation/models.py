from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AutomationMode(str, Enum):
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    FULL_AUTOPILOT = "FULL_AUTOPILOT"


class AutomationStatus(str, Enum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    ANALYZING = "ANALYZING"
    SELECTING = "SELECTING"
    PREPARING_TITLES = "PREPARING_TITLES"
    PREPARING_COMPOSITION = "PREPARING_COMPOSITION"
    QUALITY_CHECK = "QUALITY_CHECK"
    RENDERING = "RENDERING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    READY_TO_SCHEDULE = "READY_TO_SCHEDULE"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AutomationShortStatus(str, Enum):
    SELECTED = "SELECTED"
    PREPARING = "PREPARING"
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    RENDERING = "RENDERING"
    RENDERED = "RENDERED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


@dataclass
class AutomationJobSource:
    path: str
    source_id: str = ""
    shorts_project_path: str = ""
    channel_id: str = ""
    source_author: str = ""
    folder_author: str = ""
    status: str = "pending"
    candidates_found: int = 0
    shorts_selected: int = 0
    error: str = ""


@dataclass
class AutomationProfile:
    channel_profile_id: str = ""
    auto_detect: bool = True
    banner_required: bool = False
    # Empty means "use the preset currently saved in Vertical Editor".
    subtitle_preset: str = ""
    composition_preset: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AutomationIssue:
    code: str
    message: str
    critical: bool = False
    stage: str = ""
    short_id: str = ""


@dataclass
class RenderArtifact:
    candidate_id: str
    candidate_rank: Optional[int]
    output_path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    size: int = 0
    validated: bool = False


@dataclass
class AutomationShort:
    short_id: str
    source_id: str
    candidate_id: str
    candidate_rank: Optional[int]
    start: float
    end: float
    score: float
    status: str = AutomationShortStatus.SELECTED.value
    title: str = ""
    candidate_data: Dict[str, Any] = field(default_factory=dict)
    subtitle_settings: Dict[str, Any] = field(default_factory=dict)
    branding_settings: Dict[str, Any] = field(default_factory=dict)
    layout_settings: Dict[str, Any] = field(default_factory=dict)
    profile_id: str = ""
    issues: List[AutomationIssue] = field(default_factory=list)
    artifact: Optional[RenderArtifact] = None
    composition_snapshot_hash: str = ""
    render_key: str = ""
    subtitle_status: str = "pending"
    platform_artifacts: Dict[str, str] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class PublishingSlot:
    short_id: str
    scheduled_at: str
    platforms: List[str] = field(default_factory=list)
    status: str = "planned"


@dataclass
class PublishingPlan:
    timezone: str
    created_at: str
    slots: List[PublishingSlot] = field(default_factory=list)


@dataclass
class AutomationResult:
    selected_count: int = 0
    rendered_count: int = 0
    needs_review_count: int = 0
    summary: str = ""
    publishing_plan: Optional[PublishingPlan] = None


@dataclass
class AutomationJob:
    job_id: str
    sources: List[AutomationJobSource]
    mode: str = AutomationMode.APPROVAL_REQUIRED.value
    profile: AutomationProfile = field(default_factory=AutomationProfile)
    analysis_settings: Dict[str, Any] = field(default_factory=dict)
    selection_settings: Dict[str, Any] = field(default_factory=lambda: {
        "candidate_count_mode": "AUTO", "minimum_score": 80.0,
        "maximum_per_source": 10, "minimum_temporal_distance": 30.0,
        "minimum_duration": 25.0, "desired_duration": 45.0, "maximum_duration": 75.0,
    })
    composition_preset: Dict[str, Any] = field(default_factory=dict)
    composition_snapshot: Dict[str, Any] = field(default_factory=dict)
    composition_snapshot_hash: str = ""
    schedule_settings: Dict[str, Any] = field(default_factory=dict)
    platforms: List[str] = field(default_factory=lambda: ["youtube"])
    shorts: List[AutomationShort] = field(default_factory=list)
    issues: List[AutomationIssue] = field(default_factory=list)
    result: AutomationResult = field(default_factory=AutomationResult)
    progress: float = 0.0
    status: str = AutomationStatus.CREATED.value
    created_at: str = ""
    started_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    error: str = ""
    resume_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AutomationJob":
        data = dict(raw)
        data["sources"] = [AutomationJobSource(**item) for item in data.get("sources", [])]
        data["profile"] = AutomationProfile(**data.get("profile", {}))
        shorts: List[AutomationShort] = []
        for item in data.get("shorts", []):
            value = dict(item)
            value["issues"] = [AutomationIssue(**issue) for issue in value.get("issues", [])]
            if value.get("artifact"):
                value["artifact"] = RenderArtifact(**value["artifact"])
            shorts.append(AutomationShort(**value))
        data["shorts"] = shorts
        data["issues"] = [AutomationIssue(**item) for item in data.get("issues", [])]
        result = dict(data.get("result", {}))
        if result.get("publishing_plan"):
            plan = dict(result["publishing_plan"])
            plan["slots"] = [PublishingSlot(**item) for item in plan.get("slots", [])]
            result["publishing_plan"] = PublishingPlan(**plan)
        data["result"] = AutomationResult(**result)
        return cls(**data)
