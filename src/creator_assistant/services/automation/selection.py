from __future__ import annotations

from creator_assistant.domain.shorts.models import Candidate


class AutomaticCandidateSelector:
    def select(self, candidates: list[Candidate], settings: dict) -> tuple[list[Candidate], str]:
        selected, summary, _details = self.evaluate(candidates, settings)
        return selected, summary

    def evaluate(self, candidates: list[Candidate], settings: dict) -> tuple[list[Candidate], str, list[dict]]:
        minimum_score = float(settings.get("minimum_score", 80.0))
        maximum = max(0, int(settings.get("maximum_per_source", 10)))
        minimum_duration = float(settings.get("minimum_duration", 25.0))
        maximum_duration = float(settings.get("maximum_duration", 75.0))
        distance = float(settings.get("minimum_temporal_distance", 30.0))
        ranked = sorted(candidates, key=lambda item: (-(item.final_score or item.score), item.start, item.id))
        selected: list[Candidate] = []
        seen_candidate_ids: set[str] = set()
        seen_segments: set[tuple[int, int]] = set()
        details: list[dict] = []
        for candidate in ranked:
            score = candidate.final_score or candidate.score
            detail = {
                "candidate_id": candidate.id,
                "rank": candidate.candidate_rank,
                "score": round(float(score), 3),
                "start": candidate.start,
                "end": candidate.end,
                "selected": False,
                "reason": "",
            }
            if score < minimum_score:
                detail["reason"] = "ниже порога"
                details.append(detail)
                continue
            if not (minimum_duration <= candidate.duration <= maximum_duration):
                detail["reason"] = "длительность вне диапазона"
                details.append(detail)
                continue
            candidate_key = str(candidate.id).strip().casefold()
            segment_key = (round(candidate.start * 1000), round(candidate.end * 1000))
            if candidate_key in seen_candidate_ids or segment_key in seen_segments:
                detail["reason"] = "дубликат candidate/segment"
                details.append(detail)
                continue
            # Ranked order guarantees that only the best eligible internal
            # alternative represents this candidate/segment.
            seen_candidate_ids.add(candidate_key)
            seen_segments.add(segment_key)
            if any(self._same_episode(candidate, previous, distance) for previous in selected):
                detail["reason"] = "внутренняя альтернатива уже выбранного момента"
                details.append(detail)
                continue
            if maximum and len(selected) >= maximum:
                detail["reason"] = "достигнут лимит с источника"
                details.append(detail)
                continue
            selected.append(candidate)
            detail["selected"] = True
            detail["reason"] = "выбран"
            details.append(detail)
        threshold_text = f"{minimum_score:g}"
        rejected = [item for item in details if not item["selected"]]
        if len(selected) == 1 and rejected and all(item["reason"] == "ниже порога" for item in rejected):
            summary = f"Выбрано 1 из {len(candidates)}: только один кандидат прошёл порог {threshold_text}"
        else:
            summary = f"Выбрано {len(selected)} из {len(candidates)} при пороге {threshold_text}"
        summary = f"Найдено {len(candidates)} окон, отобрано {len(selected)} Shorts. {summary}\n" + "\n".join(
            f"{item['candidate_id']} — {item['score']:.1f} — {item['reason']}"
            for item in details
        )
        return selected, summary, details

    @staticmethod
    def _same_episode(candidate: Candidate, previous: Candidate, distance: float) -> bool:
        overlap = max(0.0, min(candidate.end, previous.end) - max(candidate.start, previous.start))
        shorter = max(0.001, min(candidate.duration, previous.duration))
        # Partial overlap is common for distinct story beats.  Treat it as an
        # alternative only when at least 70% of the shorter window is identical.
        return abs(candidate.start - previous.start) < distance and overlap / shorter >= 0.70


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
