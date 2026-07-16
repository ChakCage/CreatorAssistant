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
        for candidate in ranked:
            score = candidate.final_score or candidate.score
            if score < minimum_score or not (minimum_duration <= candidate.duration <= maximum_duration):
                continue
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
