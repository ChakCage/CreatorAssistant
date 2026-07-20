from __future__ import annotations

from difflib import SequenceMatcher

from creator_assistant.domain.shorts.models import Candidate


def overlap_ratio(left: Candidate, right: Candidate) -> float:
    overlap = max(0.0, min(left.end, right.end) - max(left.start, right.start))
    shorter = max(0.001, min(left.duration, right.duration))
    return overlap / shorter


class DuplicateFilter:
    def clusters(self, candidates: list[Candidate]) -> list[list[Candidate]]:
        """Build transitive event clusters while preserving candidate objects."""
        clusters: list[list[Candidate]] = []
        for candidate in sorted(candidates, key=lambda item: (item.start, item.end)):
            matches = [cluster for cluster in clusters if any(self._similar(item, candidate) for item in cluster)]
            if not matches:
                clusters.append([candidate])
                continue
            primary = matches[0]
            primary.append(candidate)
            for extra in matches[1:]:
                primary.extend(extra)
                clusters.remove(extra)
        return clusters

    def filter(self, candidates: list[Candidate], limit: int = 15) -> list[Candidate]:
        kept: list[Candidate] = []
        ranked_clusters = sorted(
            self.clusters(candidates), key=lambda items: -max(item.score for item in items)
        )
        for cluster in ranked_clusters[:limit]:
            ranked = sorted(cluster, key=lambda item: (-item.score, item.start))
            primary = ranked[0]
            existing = {tuple(item) for item in primary.alternatives}
            for alternative in ranked[1:]:
                bounds = (alternative.start, alternative.end)
                if bounds not in existing and bounds != (primary.start, primary.end):
                    primary.alternatives.append([alternative.start, alternative.end])
                    existing.add(bounds)
            kept.append(primary)
        for index, candidate in enumerate(kept, 1):
            candidate.id = f"short_{index:03d}"
        return kept

    def filter_with_diagnostics(self, candidates: list[Candidate], limit: int = 15) -> tuple[list[Candidate], int]:
        """Return unique events and the exact number collapsed as real duplicates."""
        before = len(candidates)
        kept = self.filter(candidates, limit)
        return kept, max(0, before - len(kept))

    @staticmethod
    def _similar(left: Candidate, right: Candidate) -> bool:
        text = SequenceMatcher(None, left.text.casefold(), right.text.casefold()).ratio()
        left_words = left.text.casefold().split()
        right_words = right.text.casefold().split()
        edge = min(8, len(left_words), len(right_words))
        same_hook = edge >= 3 and SequenceMatcher(None, left_words[:edge], right_words[:edge]).ratio() >= 0.72
        same_payoff = edge >= 3 and SequenceMatcher(None, left_words[-edge:], right_words[-edge:]).ratio() >= 0.72
        same_edges = abs(left.start - right.start) < 2.0 and abs(left.end - right.end) < 2.0
        close_window = abs(left.start - right.start) < 5.0 and abs(left.end - right.end) < 5.0
        overlap = overlap_ratio(left, right)
        # Similar Minecraft vocabulary in two distant episodes is not a duplicate.
        # Text similarity is only supporting evidence when the time ranges also
        # overlap or describe practically identical boundaries.
        return (
            overlap >= 0.72
            or same_edges
            or (overlap >= 0.45 and text >= 0.70)
            or (close_window and (text >= 0.78 or same_hook or same_payoff))
        )
