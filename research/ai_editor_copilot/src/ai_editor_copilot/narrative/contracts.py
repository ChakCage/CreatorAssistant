from datetime import datetime
from typing import List, Literal, Optional
from pydantic import Field, model_validator
from ai_editor_copilot.domain.models import Model, Identifier, Text, Probability, Role
from ai_editor_copilot.narrative.evidence import Annotation, EvidenceRef

StoryRole = Literal["hook", "setup", "problem", "development", "reveal", "climax", "payoff", "callback"]
Relation = Literal["requires_context", "explains", "causes", "resolves", "contradicts", "follows"]
ReasonCode = Literal["MISSING_SETUP", "MISSING_PAYOFF", "DURATION_IMPOSSIBLE", "WEAK_EVIDENCE",
    "DAMAGED_TRANSCRIPT", "INCOMPLETE_COVERAGE", "CONTEXT_BUDGET", "INVALID_MODEL_OUTPUT"]


class LocalCandidate(Model):
    start_segment_id: Identifier
    end_segment_id: Identifier
    role: StoryRole
    summary: str = Field(min_length=1, max_length=400)
    entities: List[str] = Field(max_length=8)
    topics: List[str] = Field(max_length=8)
    confidence: Probability


class BlockResponse(Model):
    candidates: List[LocalCandidate] = Field(max_length=8)
    no_candidate_reason: str = ""

    @model_validator(mode="after")
    def empty_reason(self):
        if not self.candidates and not self.no_candidate_reason:
            raise ValueError("empty chunk result needs explanation")
        return self


class IndexShortlist(Model):
    candidate_ids: List[Identifier] = Field(max_length=8)
    reason: Text


class Candidate(Model):
    id: Identifier
    evidence: EvidenceRef
    annotation: Annotation
    role: StoryRole
    entities: List[str]
    topics: List[str]
    analyzed_chunk_ids: List[Identifier]


class Chosen(Model):
    candidate_id: Identifier
    role: Role
    reason: str = Field(min_length=1, max_length=600)


class StoryEdge(Model):
    from_candidate: Identifier
    to_candidate: Identifier
    relation: Relation
    evidence_segment_ids: List[Identifier] = Field(min_length=1, max_length=20)
    rationale: str = Field(min_length=1, max_length=600)
    confidence: Probability
    status: Literal["INFERRED"] = "INFERRED"


class Decision(Model):
    status: Literal["PLAN", "NO_COHERENT_STORY"]
    story: str = Field(max_length=800)
    selected: List[Chosen] = Field(max_length=8)
    edges: List[StoryEdge] = Field(max_length=12)
    reason_codes: List[ReasonCode] = Field(max_length=3)
    explanation: str = Field(max_length=800)

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("duplicate refusal reason codes")
        if self.status == "NO_COHERENT_STORY":
            if self.selected or self.edges or not self.reason_codes or not self.explanation:
                raise ValueError("refusal needs reasons and no plan")
        elif not self.selected or not self.story or self.reason_codes:
            raise ValueError("plan needs story/selection and no refusal codes")
        return self


class Refusal(Model):
    status: Literal["NO_COHERENT_STORY"] = "NO_COHERENT_STORY"
    reason_codes: List[ReasonCode] = Field(min_length=1)
    explanation: Text


def validate_graph(decision, candidates):
    by_id = {c.id: c for c in candidates}
    selected = [s.candidate_id for s in decision.selected]
    if len(set(selected)) != len(selected) or not set(selected) <= set(by_id):
        raise ValueError("duplicate or unknown selected candidate")
    pairs = set()
    for edge in decision.edges:
        if edge.from_candidate not in selected or edge.to_candidate not in selected or edge.from_candidate == edge.to_candidate:
            raise ValueError("story edge references unselected/self candidate")
        allowed = set(by_id[edge.from_candidate].evidence.source_segment_ids + by_id[edge.to_candidate].evidence.source_segment_ids)
        if not set(edge.evidence_segment_ids) <= allowed:
            raise ValueError("story rationale has unsupported evidence references")
        if edge.relation == "requires_context" and selected.index(edge.to_candidate) >= selected.index(edge.from_candidate):
            raise ValueError("required context must precede dependent candidate")
        if edge.relation == "contradicts":
            raise ValueError("unresolved contradiction in selected story")
        pairs.add((edge.from_candidate, edge.to_candidate))
    if any((a, b) not in pairs for a, b in zip(selected, selected[1:])):
        raise ValueError("every adjacent cut needs evidence-linked rationale")


def deduplicate(candidates):
    """Exact raw span identity only. Shared topic is NEVER a duplicate criterion."""
    result, removed = {}, []
    for candidate in candidates:
        key = tuple(candidate.evidence.source_segment_ids)
        old = result.get(key)
        if old is None:
            result[key] = candidate
        else:
            winner, loser = (candidate, old) if candidate.annotation.confidence > old.annotation.confidence else (old, candidate)
            winner = winner.model_copy(update={"analyzed_chunk_ids": sorted(set(old.analyzed_chunk_ids + candidate.analyzed_chunk_ids))})
            result[key] = winner
            removed.append({"id": loser.id, "reason": "identical raw evidence span"})
    return sorted(result.values(), key=lambda c: (c.evidence.evidence_start, c.id)), removed
