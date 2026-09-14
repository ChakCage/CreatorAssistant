from typing import List, Literal
from pydantic import Field
from ai_editor_copilot.domain.models import (Model, Identifier, Text, NonNegative, Positive,
    Probability, TimeRange, Role, SourceAsset, StyleProfile, UserCommand, TargetFormat)
from ai_editor_copilot.tools.registry import EditAction, Provenance, SearchReference


class Selection(Model):
    segment_id: Identifier
    source_range: TimeRange
    narrative_role: Role
    reason: Text
    confidence: Probability


class PlannerProposal(Model):
    """Small LLM wire contract; compiler owns IDs, offsets and canonical asset references."""
    story: Text
    clips: List[Selection] = Field(min_length=1, max_length=100)
    asset_requests: List[SearchReference] = Field(default_factory=list, max_length=50)
    notes: str = ""


class PlannedClip(Model):
    id: Identifier
    segment_id: Identifier
    source_asset_id: Identifier
    source_range: TimeRange
    timeline_start: NonNegative
    narrative_role: Role
    reason: Text
    confidence: Probability


class NarrativeBeat(Model):
    role: Role
    clip_id: Identifier
    reason: Text


class EditPlan(Model):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: Identifier
    user_request: UserCommand
    target_format: TargetFormat
    target_duration: Positive
    minimum_duration: NonNegative
    source_references: List[SourceAsset] = Field(min_length=1)
    style_profile: StyleProfile
    story: Text
    sequence: List[PlannedClip] = Field(min_length=1)
    narrative_structure: List[NarrativeBeat] = Field(min_length=1)
    actions: List[EditAction] = Field(min_length=1)
    warnings: List[str] = Field(default_factory=list)
    unresolved_requirements: List[str] = Field(default_factory=list)
    asset_requests: List[SearchReference] = Field(default_factory=list)
    planner_notes: str
    generated_by: Provenance
