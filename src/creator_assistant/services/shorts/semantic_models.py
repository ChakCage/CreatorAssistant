from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


MomentType = Literal[
    "funny", "tense", "unexpected", "useful", "emotional", "achievement",
    "failure", "transformation", "explanation", "other",
]


class SemanticCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    start: float
    end: float
    duration: float
    transcript: str
    context_before: str = ""
    context_after: str = ""
    content_type: str = "gaming"
    heuristic_score: float = Field(ge=0, le=100)
    speech_density: float = Field(ge=0)
    scene_activity: float = Field(ge=0)
    audio_activity: float = Field(ge=0)
    pause_count: int = Field(ge=0)
    assumed_hook: str = ""
    assumed_payoff: str = ""


class SemanticCandidateScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    semantic_score: float = Field(ge=0, le=100)
    hook_score: float = Field(ge=0, le=100)
    context_independence: float = Field(ge=0, le=100)
    conflict_score: float = Field(ge=0, le=100)
    development_score: float = Field(ge=0, le=100)
    payoff_score: float = Field(ge=0, le=100)
    emotion_score: float = Field(ge=0, le=100)
    entertainment_score: float = Field(ge=0, le=100)
    usefulness_score: float = Field(ge=0, le=100)
    retention_score: float = Field(ge=0, le=100)
    completeness_score: float = Field(ge=0, le=100)
    moment_type: MomentType = "other"
    verdict: str = Field(min_length=1, max_length=240)
    reason: str = Field(min_length=1, max_length=600)
    weaknesses: List[str] = Field(default_factory=list, max_length=6)
    suggested_start: Optional[float] = None
    suggested_end: Optional[float] = None


class SemanticBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: List[SemanticCandidateScore] = Field(min_length=1)


class GlobalSelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_ids: List[str] = Field(min_length=1)
    reasons: Dict[str, str] = Field(default_factory=dict)

