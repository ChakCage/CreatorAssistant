from __future__ import annotations

from dataclasses import dataclass

from creator_assistant.domain.shorts.models import AudioFeatures, Candidate, Scene, Transcript


@dataclass(frozen=True)
class CandidateSettings:
    minimum: float = 25.0
    maximum: float = 60.0
    desired: float = 45.0
    count: int = 15
    content_type: str = "gaming"


class CandidateGenerator:
    def generate(self, transcript: Transcript, scenes: list[Scene], audio: AudioFeatures, settings: CandidateSettings) -> list[Candidate]:
        segments = [item for item in transcript.segments if item.text.strip() and item.end > item.start]
        if not segments:
            return []
        candidates: list[Candidate] = []
        for left, first in enumerate(segments):
            best_right = None
            best_distance = float("inf")
            for right in range(left, len(segments)):
                duration = segments[right].end - first.start
                if duration > settings.maximum:
                    break
                if duration >= settings.minimum:
                    distance = abs(duration - settings.desired)
                    if distance < best_distance:
                        best_distance, best_right = distance, right
            if best_right is None:
                continue
            last = segments[best_right]
            start = self._nearest_boundary(first.start, scenes, audio, before=True)
            end = self._nearest_boundary(last.end, scenes, audio, before=False)
            if end - start > settings.maximum + 3:
                start, end = first.start, last.end
            text = " ".join(item.text.strip() for item in segments[left:best_right + 1])
            candidates.append(Candidate(
                id=f"short_{len(candidates) + 1:03d}", start=round(max(0.0, start), 3), end=round(end, 3),
                score=0.0, text=text, reasons=["Границы совпадают с законченными фразами"],
            ))
        # Generate extra windows before scoring so the ranker has real alternatives.
        return candidates[: max(settings.count * 4, settings.count)]

    @staticmethod
    def _nearest_boundary(value: float, scenes: list[Scene], audio: AudioFeatures, before: bool) -> float:
        boundaries = [scene.start for scene in scenes] + [scene.end for scene in scenes]
        boundaries += [point for pause in audio.pauses for point in pause]
        candidates = [point for point in boundaries if abs(point - value) <= 1.5 and ((point <= value) if before else (point >= value))]
        return min(candidates, key=lambda point: abs(point - value)) if candidates else value
