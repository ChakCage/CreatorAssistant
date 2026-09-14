from __future__ import annotations

from typing import Annotated, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator

NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")]
Text = Annotated[str, Field(min_length=1, max_length=20000)]
Role = Literal["hook", "setup", "development", "climax", "payoff"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True, allow_inf_nan=False)


class TimeRange(Model):
    """Seconds, half-open [start, end), relative to the referenced source."""
    start: NonNegative
    end: Positive

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class SourceAsset(Model):
    id: Identifier
    kind: Literal["video", "audio", "image", "broll", "meme", "sfx", "music", "overlay"]
    uri: Text
    duration: Optional[Positive] = None
    title: str = ""
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    rights: str = "unknown; user must verify before retrieval/export"


class SemanticSegment(Model):
    id: Identifier
    source_asset_id: Identifier
    range: TimeRange
    transcript: str
    speaker: Optional[str] = None
    topic: Optional[str] = None
    story_id: Optional[Identifier] = None
    semantic_summary: Optional[str] = None
    narrative_role: Optional[Role] = None
    emotion: Optional[str] = None
    energy: Optional[Probability] = None
    punchline_likelihood: Optional[Probability] = None
    importance: Optional[Probability] = None
    scene: Optional[str] = None
    visual_description: Optional[str] = None
    detected_text: Optional[List[str]] = None
    objects: Optional[List[str]] = None
    faces: Optional[List[str]] = None
    silence: Optional[bool] = None
    shot_change: Optional[bool] = None
    annotation_origin: Literal["transcript_only", "human", "synthetic", "model"] = "transcript_only"


class SemanticTimeline(Model):
    schema_version: Literal["1.0"] = "1.0"
    id: Identifier
    sources: List[SourceAsset] = Field(min_length=1)
    segments: List[SemanticSegment] = Field(min_length=1)

    @model_validator(mode="after")
    def references(self):
        assets = {a.id: a for a in self.sources}
        if len(assets) != len(self.sources):
            raise ValueError("duplicate source ID")
        if len({s.id for s in self.segments}) != len(self.segments):
            raise ValueError("duplicate segment ID")
        for s in self.segments:
            a = assets.get(s.source_asset_id)
            if a is None or a.duration is None:
                raise ValueError(f"segment {s.id}: timed source with duration required")
            if s.range.end > a.duration:
                raise ValueError(f"segment {s.id}: outside source duration")
        return self


class AssetIndex(Model):
    schema_version: Literal["1.0"] = "1.0"
    assets: List[SourceAsset] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        if len({a.id for a in self.assets}) != len(self.assets):
            raise ValueError("duplicate asset ID")
        return self


class StyleProfile(Model):
    schema_version: Literal["1.0"] = "1.0"
    id: Identifier
    name: Text
    pacing: Literal["slow", "balanced", "dynamic"] = "balanced"
    average_shot_duration: Positive = 4
    visual_changes_per_minute: NonNegative = 15
    broll_density: Probability = 0.2
    meme_density: Probability = 0
    zoom_density: Probability = 0.1
    sfx_density: Probability = 0.1
    subtitle_style: str = "clean"
    transition_style: str = "cut"
    music_usage: Literal["none", "background", "featured"] = "none"
    cold_open: bool = True
    dead_air_removal_aggressiveness: Probability = 0.5
    origin: Literal["user", "preset", "reference_analysis"] = "preset"


class UserCommand(Model):
    source: Literal["text", "voice"] = "text"
    text: Text
    transcript_confidence: Optional[Probability] = None


class TargetFormat(Model):
    width: Annotated[int, Field(gt=0)] = 1080
    height: Annotated[int, Field(gt=0)] = 1920
    fps_numerator: Annotated[int, Field(gt=0)] = 30000
    fps_denominator: Annotated[int, Field(gt=0)] = 1001


class TimelineState(Model):
    id: Identifier
    revision: Identifier
    duration: NonNegative
    clip_ids: List[Identifier] = Field(default_factory=list)
    description: str = ""


class PlannerInput(Model):
    timeline: SemanticTimeline
    assets: AssetIndex = Field(default_factory=AssetIndex)
    style: StyleProfile
    command: UserCommand
    target_duration: Positive = 60
    minimum_duration: NonNegative = 0
    target_format: TargetFormat = Field(default_factory=TargetFormat)
    current_timeline: Optional[TimelineState] = None

    @model_validator(mode="after")
    def constraints(self):
        if self.minimum_duration > self.target_duration:
            raise ValueError("minimum duration exceeds target duration")
        ids = [a.id for a in self.timeline.sources + self.assets.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("source and supplementary asset IDs must be unique")
        return self
