from __future__ import annotations

import re
from abc import ABC, abstractmethod

from creator_assistant.domain.shorts.models import AudioFeatures, Candidate, Scene


class CandidateScorerBackend(ABC):
    @abstractmethod
    def score(self, candidate: Candidate, scenes: list[Scene], audio: AudioFeatures) -> Candidate:
        raise NotImplementedError


class HeuristicCandidateScorer(CandidateScorerBackend):
    HOOK_WORDS = ("как", "почему", "никогда", "самый", "лучший", "вот", "смотрите", "представьте", "если")
    PAYOFF_WORDS = ("поэтому", "итог", "получилось", "в результате", "наконец", "побед", "готово")
    EMOTION = ("!", "невероят", "ужас", "вау", "шок", "смеш", "круто", "опас")

    def score(self, candidate: Candidate, scenes: list[Scene], audio: AudioFeatures) -> Candidate:
        text = candidate.text.strip()
        lower = text.casefold()
        words = re.findall(r"[\w'-]+", text, re.UNICODE)
        hook = 1.0 if lower.startswith(self.HOOK_WORDS) or "?" in text[:100] else 0.45
        self_contained = 1.0 if words and not lower.startswith(("и ", "но ", "а ", "потому что", "это он")) else 0.25
        payoff = 1.0 if any(item in lower[-180:] for item in self.PAYOFF_WORDS) else (0.65 if text.endswith((".", "!", "?")) else 0.25)
        emotion = min(1.0, 0.3 + sum(item in lower for item in self.EMOTION) * 0.25)
        scene_changes = sum(candidate.start < scene.start < candidate.end for scene in scenes)
        visual = min(1.0, 0.25 + scene_changes / max(1.0, candidate.duration / 8.0))
        pace = len(words) / max(candidate.duration, 1.0)
        speech_pace = max(0.0, 1.0 - abs(pace - 2.4) / 2.4)
        uniqueness = 1.0
        weighted = 25 * hook + 20 * self_contained + 15 * payoff + 15 * emotion + 10 * visual + 10 * speech_pace + 5 * uniqueness
        long_pauses = [pause for pause in audio.pauses if pause[0] < candidate.end and pause[1] > candidate.start and pause[1] - pause[0] >= 2.0]
        if long_pauses:
            weighted -= min(20, 6 * len(long_pauses))
            candidate.warnings.append("Внутри есть продолжительная пауза")
        if hook >= 0.9:
            candidate.reasons.append("Сильное начало")
        if payoff >= 0.9:
            candidate.reasons.append("Есть развязка")
        if visual >= 0.7:
            candidate.reasons.append("Высокая визуальная динамика")
        if self_contained < 0.5:
            candidate.warnings.append("Возможно, начало зависит от предыдущего контекста")
        if payoff < 0.5:
            candidate.warnings.append("Концовка может быть незавершённой")
        candidate.score = round(max(0.0, min(100.0, weighted)), 1)
        return candidate
