"""Mechanical metrics and blind TEXT selection evaluation. No human quality claims."""
import hashlib
import json
import random
import re
from pathlib import Path
from statistics import mean, pstdev
from typing import List, Literal
from pydantic import Field
from ai_editor_copilot.domain.models import Model, Text, Probability
from ai_editor_copilot.narrative.contracts import Refusal
from ai_editor_copilot.narrative.chunks import union_length


class Task(Model):
    id: str
    source_id: str
    split: Literal["development", "held_out"]
    scenario: str
    request: Text
    target_duration: float = Field(gt=0, le=180)
    minimum_duration: float = Field(ge=0)


def load_tasks(path, split="development", allow_held_out=False):
    if split == "held_out" and not allow_held_out:
        raise ValueError("held-out release requires explicit final-evaluation flag")
    tasks = [Task.model_validate(t) for t in json.loads(Path(path).read_text(encoding="utf-8"))]
    if len({t.id for t in tasks}) != len(tasks):
        raise ValueError("duplicate task ID")
    if any(t.minimum_duration > t.target_duration for t in tasks):
        raise ValueError("invalid task duration")
    return [t for t in tasks if t.split == split]


class HumanRating(Model):
    evaluator_id: str
    case_id: str
    label: Literal["A", "B"]
    hook: int = Field(ge=1, le=5)
    comprehensibility: int = Field(ge=1, le=5)
    causal_coherence: int = Field(ge=1, le=5)
    setup_payoff: int = Field(ge=1, le=5)
    information_sufficiency: int = Field(ge=1, le=5)
    redundancy: int = Field(ge=1, le=5)
    pacing: int = Field(ge=1, le=5)
    overall_editability: int = Field(ge=1, le=5)
    preference: Literal["A", "B", "tie", "neither"]
    confidence: Probability
    reason: Text


def metrics(result, source, calls=(), labelled_segment_ids=None):
    base = {"refusal": isinstance(result, Refusal), "inference_latency_seconds": sum(c["latency_seconds"] for c in calls),
        "planner_retries": sum(c["attempt"] > 1 for c in calls),
        "repair_rate": sum(c["attempt"] > 1 for c in calls) / max(1, sum(c["attempt"] == 1 for c in calls)),
        "context_tokens": sum(c["context_tokens"] for c in calls)}
    if isinstance(result, Refusal):
        return {**base, "reason_codes": result.reason_codes}
    seq = result.sequence
    ranges = [(c.source_range.start, c.source_range.end) for c in seq]
    total = sum(b - a for a, b in ranges)
    jumps = [abs(b[0] - a[1]) for a, b in zip(ranges, ranges[1:]) if abs(b[0] - a[1]) > 1e-6]
    selected_raw = {s.id for s in source.segments if any(s.range.start >= a and s.range.end <= b for a, b in ranges)}
    tokens = re.findall(r"\w+", " ".join(s.text for s in source.segments if s.id in selected_raw).casefold())
    grams = [tuple(tokens[i:i + 5]) for i in range(max(0, len(tokens) - 4))]
    return {**base, "duration": total, "target_duration_error": total - result.target_duration,
        "source_jumps": len(jumps), "average_jump_distance": mean(jumps) if jumps else 0,
        "transcript_segment_coverage": len(selected_raw) / len(source.segments),
        "candidate_recall": None if labelled_segment_ids is None else len(selected_raw & set(labelled_segment_ids)) / max(1, len(set(labelled_segment_ids))),
        "role_completeness_structural_only": len({c.narrative_role for c in seq} & {"hook", "setup", "payoff"}) / 3,
        "duplicate_source_ratio": 1 - union_length(ranges) / total,
        "duplicate_content_ratio": 1 - len(set(grams)) / len(grams) if grams else 0,
        "duplicate_content_definition": "Repeated normalized 5-token shingles; lexical proxy, not semantic redundancy",
        "overlap_violations": sum(min(b, d) > max(a, c) for i, (a, b) in enumerate(ranges) for c, d in ranges[i + 1:]),
        "source_order_preserved": all(a[0] <= b[0] for a, b in zip(ranges, ranges[1:])),
        "intentional_reorder": any(a[0] > b[0] for a, b in zip(ranges, ranges[1:])),
        "quality_warning": "Mechanical metrics do not establish narrative quality; positional baseline roles are proxies."}


def variance(results):
    valid = [r for r in results if not isinstance(r, Refusal)]
    sets = [{(c.source_range.start, c.source_range.end) for c in r.sequence} for r in valid]
    similarity = [len(a & b) / max(1, len(a | b)) for i, a in enumerate(sets) for b in sets[i + 1:]]
    role_sets = [{(c.source_range.start, c.source_range.end, c.narrative_role) for c in r.sequence} for r in valid]
    roles = [len(a & b) / max(1, len(a | b)) for i, a in enumerate(role_sets) for b in role_sets[i + 1:]]
    durations = [sum(c.source_range.end - c.source_range.start for c in r.sequence) for r in valid]
    return {"runs": len(results), "valid_plans": len(valid), "refusal_rate": 1 - len(valid) / max(1, len(results)),
        "selection_pairwise_jaccard": mean(similarity) if similarity else None,
        "role_pairwise_jaccard": mean(roles) if roles else None,
        "duration_population_stddev": pstdev(durations) if durations else None,
        "note": "Conditional stability among valid runs; refusals reported separately. Three deterministic-temperature requests may coincide."}


def blind_package(case_id, source, baseline, ai, seed):
    pairs = [("baseline", baseline), ("hierarchical", ai)]
    random.Random(seed).shuffle(pairs)
    public, key = {"case_id": case_id, "modality": "transcript-only; not rendered video", "variants": {}}, {}
    for label, (method, result) in zip(["A", "B"], pairs):
        key[label] = method
        if isinstance(result, Refusal):
            public["variants"][label] = {"status": "NO_SELECTION"}
        else:
            # Strip reasons, roles, IDs, scores and generator metadata to avoid label leakage.
            public["variants"][label] = {"spans": [{"duration": c.source_range.end - c.source_range.start,
                "text": " ".join(s.text for s in source.segments
                    if s.range.start >= c.source_range.start and s.range.end <= c.source_range.end)} for c in result.sequence]}
    return public, {"case_id": case_id, "mapping": key}
