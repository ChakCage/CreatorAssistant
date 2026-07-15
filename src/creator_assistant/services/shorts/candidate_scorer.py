from __future__ import annotations

import re
from abc import ABC, abstractmethod

from creator_assistant.domain.shorts.models import AudioFeatures, Candidate, Scene


class CandidateScorerBackend(ABC):
    @abstractmethod
    def score(self, candidate: Candidate, scenes: list[Scene], audio: AudioFeatures, content_type: str = "gaming") -> Candidate:
        raise NotImplementedError


class HeuristicCandidateScorer(CandidateScorerBackend):
    HOOK_WORDS = ("как", "почему", "никогда", "самый", "лучший", "вот", "смотрите", "представьте", "если")
    PAYOFF_WORDS = ("поэтому", "итог", "получилось", "в результате", "наконец", "побед", "готово")
    EMOTION = ("!", "невероят", "ужас", "вау", "шок", "смеш", "круто", "опас")
    PROFILE_WORDS = {
        "gaming": ("опас", "ошиб", "побед", "проиг", "достиж", "битв", "фарм", "слома", "нашёл", "получилось"),
        "education": ("как", "почему", "объяс", "реш", "проблем", "результат", "способ", "нужно", "важно"),
        "talking": ("думаю", "мнение", "история", "честно", "спор", "смеш", "внезап", "вывод", "оказалось"),
    }
    PROFILE_WEIGHTS = {
        "gaming": {"hook": 27, "self": 16, "payoff": 18, "emotion": 17, "visual": 14, "pace": 5, "profile": 3},
        "education": {"hook": 20, "self": 24, "payoff": 22, "emotion": 6, "visual": 8, "pace": 8, "profile": 12},
        "talking": {"hook": 24, "self": 22, "payoff": 15, "emotion": 18, "visual": 4, "pace": 8, "profile": 9},
    }

    def score(self, candidate: Candidate, scenes: list[Scene], audio: AudioFeatures, content_type: str = "gaming") -> Candidate:
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
        profile_hits = sum(item in lower for item in self.PROFILE_WORDS.get(content_type, self.PROFILE_WORDS["gaming"]))
        profile_fit = min(1.0, 0.25 + profile_hits * 0.18)
        uniqueness = 1.0
        weights = self.PROFILE_WEIGHTS.get(content_type, self.PROFILE_WEIGHTS["gaming"])
        weighted = (
            weights["hook"] * hook + weights["self"] * self_contained + weights["payoff"] * payoff
            + weights["emotion"] * emotion + weights["visual"] * visual + weights["pace"] * speech_pace
            + weights["profile"] * profile_fit + 5 * uniqueness
        )
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
        if profile_fit >= 0.7:
            label = {"gaming": "игрового", "education": "обучающего", "talking": "разговорного"}.get(content_type, content_type)
            candidate.reasons.append(f"Соответствует профилю {label} ролика")
        if candidate.duration > 1.2 * 45 and payoff >= 0.9:
            candidate.reasons.append("Длительность выше желаемой, но сохранена для завершённой развязки")
        if self_contained < 0.5:
            candidate.warnings.append("Возможно, начало зависит от предыдущего контекста")
        if payoff < 0.5:
            candidate.warnings.append("Концовка может быть незавершённой")
        candidate.score = round(max(0.0, min(100.0, weighted)), 1)
        return candidate
