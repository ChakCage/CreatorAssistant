from __future__ import annotations

from creator_assistant.domain.shorts.models import Candidate


class AutomaticCandidateSelector:
    def select(self, candidates: list[Candidate], settings: dict) -> tuple[list[Candidate], str]:
        minimum_score = float(settings.get("minimum_score", 80.0))
        maximum = max(0, int(settings.get("maximum_per_source", 10)))
        minimum_duration = float(settings.get("minimum_duration", 25.0))
        maximum_duration = float(settings.get("maximum_duration", 75.0))
        distance = float(settings.get("minimum_temporal_distance", 30.0))
        ranked = sorted(candidates, key=lambda item: (-(item.final_score or item.score), item.start, item.id))
        selected: list[Candidate] = []
        seen_candidate_ids: set[str] = set()
        seen_segments: set[tuple[int, int]] = set()
        for candidate in ranked:
            score = candidate.final_score or candidate.score
            if score < minimum_score or not (minimum_duration <= candidate.duration <= maximum_duration):
                continue
            candidate_key = str(candidate.id).strip().casefold()
            segment_key = (round(candidate.start * 1000), round(candidate.end * 1000))
            if candidate_key in seen_candidate_ids or segment_key in seen_segments:
                continue
            # Ranked order guarantees that only the best eligible internal
            # alternative represents this candidate/segment.
            seen_candidate_ids.add(candidate_key)
            seen_segments.add(segment_key)
            if any(
                abs(candidate.start - previous.start) < distance
                or min(candidate.end, previous.end) - max(candidate.start, previous.start) > 0
                for previous in selected
            ):
                continue
            selected.append(candidate)
            if len(selected) >= maximum:
                break
        summary = (
            f"Найдено {len(candidates)} окон, отобрано {len(selected)} Shorts: "
            "остальные ниже порога или являются дублями"
        )
        return selected, summary


def unique_automation_shorts(shorts):
    """Keep one best render task per source/candidate and per source/segment."""
    ranked = sorted(
        shorts,
        key=lambda item: (-(item.score or 0), item.candidate_rank or 10**9, item.start, item.candidate_id),
    )
    kept = []
    candidate_keys: set[tuple[str, str]] = set()
    segment_keys: set[tuple[str, int, int]] = set()
    for short in ranked:
        candidate_key = (short.source_id, str(short.candidate_id).strip().casefold())
        segment_key = (short.source_id, round(short.start * 1000), round(short.end * 1000))
        if candidate_key in candidate_keys or segment_key in segment_keys:
            continue
        candidate_keys.add(candidate_key)
        segment_keys.add(segment_key)
        kept.append(short)
    return sorted(kept, key=lambda item: (item.candidate_rank or 10**9, item.source_id, item.start))
