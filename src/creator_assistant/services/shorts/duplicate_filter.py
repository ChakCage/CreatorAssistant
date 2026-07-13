from __future__ import annotations

from difflib import SequenceMatcher

from creator_assistant.domain.shorts.models import Candidate


def overlap_ratio(left: Candidate, right: Candidate) -> float:
    overlap = max(0.0, min(left.end, right.end) - max(left.start, right.start))
    shorter = max(0.001, min(left.duration, right.duration))
    return overlap / shorter


class DuplicateFilter:
    def filter(self, candidates: list[Candidate], limit: int = 15) -> list[Candidate]:
        kept: list[Candidate] = []
        for candidate in sorted(candidates, key=lambda item: (-item.score, item.start)):
            duplicate = next((item for item in kept if self._similar(item, candidate)), None)
            if duplicate:
                duplicate.alternatives.append([candidate.start, candidate.end])
                continue
            kept.append(candidate)
            if len(kept) >= limit:
                break
        for index, candidate in enumerate(kept, 1):
            candidate.id = f"short_{index:03d}"
        return kept

    @staticmethod
    def _similar(left: Candidate, right: Candidate) -> bool:
        text = SequenceMatcher(None, left.text.casefold(), right.text.casefold()).ratio()
        same_edges = abs(left.start - right.start) < 2.0 and abs(left.end - right.end) < 2.0
        return overlap_ratio(left, right) >= 0.72 or text >= 0.82 or same_edges
