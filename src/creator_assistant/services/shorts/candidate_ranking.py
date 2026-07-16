from __future__ import annotations

from creator_assistant.domain.shorts.models import Candidate


def assign_candidate_ranks(candidates: list[Candidate]) -> bool:
    """Persist a deterministic final-analysis rank without reordering the UI list."""
    changed = False
    ranked = sorted(
        candidates,
        key=lambda item: (-(item.final_score or item.score), item.start, item.id),
    )
    for rank, candidate in enumerate(ranked, 1):
        if candidate.candidate_rank != rank:
            candidate.candidate_rank = rank
            changed = True
    return changed
