from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import AudioFeatures, Candidate, Scene, Transcript
from creator_assistant.services.shorts.duplicate_filter import DuplicateFilter
from creator_assistant.services.shorts.semantic_backend import (
    PROMPT_VERSION,
    SemanticBackendError,
    SemanticScorerBackend,
)
from creator_assistant.services.shorts.semantic_cache import SemanticCache, semantic_cache_payload
from creator_assistant.services.shorts.semantic_models import SemanticCandidateInput, SemanticCandidateScore


@dataclass
class HybridAnalysisResult:
    candidates: List[Candidate]
    used_ai: bool
    cache_hit: bool = False
    fallback_reason: str = ""
    backend: str = "disabled"
    model: str = ""


class HybridCandidateAnalyzer:
    """Rerank heuristic candidates semantically while keeping a deterministic fallback."""

    def __init__(self, duplicate_filter: DuplicateFilter) -> None:
        self.duplicate_filter = duplicate_filter

    def analyse(
        self,
        scored: List[Candidate],
        transcript: Transcript,
        scenes: List[Scene],
        audio: AudioFeatures,
        backend: SemanticScorerBackend,
        settings: Dict[str, Any],
        cache: SemanticCache,
        cancellation: CancellationToken,
        *,
        content_type: str,
        requested_count: int,
    ) -> HybridAnalysisResult:
        cancellation.raise_if_cancelled()
        preliminary_count = max(requested_count, int(settings.get("preliminary_count", 40)))
        preliminary = sorted(scored, key=lambda item: -item.score)[:preliminary_count]
        clustered = self.duplicate_filter.filter(preliminary, preliminary_count)
        for item in clustered:
            item.heuristic_score = item.score
            item.final_score = item.score
        if not settings.get("enabled", False) or backend.name == "disabled":
            return HybridAnalysisResult(clustered[:requested_count], False, backend=backend.name)

        inputs = [self._input(item, transcript, scenes, audio, content_type) for item in clustered]
        model = str(settings.get("model", getattr(backend, "model", "")))
        payload = semantic_cache_payload(inputs, model=model, content_type=content_type)
        cached = cache.get(payload) if settings.get("cache", True) else None
        try:
            if cached:
                semantic = [SemanticCandidateScore.model_validate(item) for item in cached.get("results", [])]
                selected_ids = [str(item) for item in cached.get("selected_ids", [])]
                cache_hit = True
            else:
                backend.check(cancellation) if hasattr(backend, "check") else None
                semantic = self._evaluate_batches(backend, inputs, cancellation, settings)
                selected_ids = []
                cache_hit = False
            by_id = {item.candidate_id: item for item in semantic}
            if set(by_id) != {item.id for item in clustered}:
                raise SemanticBackendError("AI-оценка не соответствует набору кандидатов.")
            self._combine(clustered, by_id, settings)
            if not selected_ids:
                selected_ids = self._global_selection(backend, clustered, requested_count, cancellation, settings)
                if settings.get("cache", True):
                    cache.put(payload, {
                        "prompt_version": PROMPT_VERSION,
                        "results": [item.model_dump(mode="json") for item in semantic],
                        "selected_ids": selected_ids,
                    })
            result = self._ordered(clustered, selected_ids, requested_count)
            return HybridAnalysisResult(result, True, cache_hit, backend=backend.name, model=model)
        except Exception as exc:
            if cancellation.is_cancelled:
                cancellation.raise_if_cancelled()
            if not settings.get("fallback", True):
                raise
            message = str(exc).strip() or type(exc).__name__
            for item in clustered:
                item.selection_source = "heuristic_fallback"
                item.warnings.append(f"Локальная AI-оценка недоступна: {message}")
            return HybridAnalysisResult(
                clustered[:requested_count], False, bool(cached), message, backend.name, model
            )

    @staticmethod
    def _evaluate_batches(backend, inputs, cancellation, settings):
        batch_size = max(1, min(12, int(settings.get("batch_size", 8))))
        values = []
        for offset in range(0, len(inputs), batch_size):
            cancellation.raise_if_cancelled()
            values.extend(backend.evaluate(
                inputs[offset:offset + batch_size], cancellation,
                think=str(settings.get("mode", "balanced")) == "quality",
            ))
        return values

    @staticmethod
    def _global_selection(backend, candidates, count, cancellation, settings):
        ranked = sorted(candidates, key=lambda item: -item.final_score)
        if not settings.get("global_comparison", True):
            return [item.id for item in ranked[:count]]
        summaries = [{
            "candidate_id": item.id, "start": item.start, "end": item.end,
            "text": item.text, "heuristic_score": item.heuristic_score,
            "semantic_score": item.semantic_score, "final_score": item.final_score,
            "moment_type": item.ai_moment_type, "reason": item.ai_reason,
        } for item in ranked]
        selection = backend.global_select(
            summaries, count, cancellation,
            think=str(settings.get("mode", "balanced")) == "quality",
        )
        return selection.candidate_ids

    @staticmethod
    def _ordered(candidates, selected_ids, count):
        by_id = {item.id: item for item in candidates}
        result = [by_id[item] for item in selected_ids if item in by_id]
        seen = {item.id for item in result}
        result.extend(item for item in sorted(candidates, key=lambda value: -value.final_score) if item.id not in seen)
        return result[:count]

    @staticmethod
    def _combine(candidates, semantic, settings):
        weights = settings.get("weights", {})
        semantic_weight = float(weights.get("semantic", 0.55))
        heuristic_weight = float(weights.get("heuristic", 0.25))
        activity_weight = float(weights.get("activity", 0.15))
        uniqueness_weight = float(weights.get("uniqueness", 0.05))
        total = max(0.001, semantic_weight + heuristic_weight + activity_weight + uniqueness_weight)
        for item in candidates:
            value = semantic[item.id]
            activity = min(100.0, 50.0 + 5.0 * len(item.reasons) - 3.0 * len(item.warnings))
            final = (
                semantic_weight * value.semantic_score + heuristic_weight * item.heuristic_score
                + activity_weight * activity + uniqueness_weight * 100.0
            ) / total
            item.semantic_score = round(value.semantic_score, 1)
            item.final_score = item.score = round(final, 1)
            item.ai_moment_type = value.moment_type
            item.ai_verdict = value.verdict
            item.ai_reason = value.reason
            item.ai_weaknesses = list(value.weaknesses)
            item.selection_source = "hybrid_ai"
            item.reasons.append(f"AI: {value.reason}")
            item.warnings.extend(value.weaknesses)
            if value.suggested_start is not None and value.suggested_end is not None:
                bounds = [round(value.suggested_start, 3), round(value.suggested_end, 3)]
                if value.suggested_end > value.suggested_start and bounds not in item.alternatives:
                    item.alternatives.append(bounds)

    @staticmethod
    def _input(candidate, transcript, scenes, audio, content_type):
        before = [s.text.strip() for s in transcript.segments if candidate.start - 12 <= s.end <= candidate.start]
        after = [s.text.strip() for s in transcript.segments if candidate.end <= s.start <= candidate.end + 12]
        words = candidate.text.split()
        scene_changes = sum(candidate.start < scene.start < candidate.end for scene in scenes)
        pauses = sum(candidate.start < pause[1] and candidate.end > pause[0] for pause in audio.pauses)
        peaks = sum(candidate.start <= float(peak.get("time", -1)) <= candidate.end for peak in audio.peaks)
        return SemanticCandidateInput(
            candidate_id=candidate.id, start=candidate.start, end=candidate.end,
            duration=candidate.duration, transcript=candidate.text,
            context_before=" ".join(before[-3:]), context_after=" ".join(after[:3]),
            content_type=content_type, heuristic_score=candidate.heuristic_score,
            speech_density=len(words) / max(1.0, candidate.duration),
            scene_activity=scene_changes / max(1.0, candidate.duration),
            audio_activity=peaks / max(1.0, candidate.duration), pause_count=pauses,
            assumed_hook=" ".join(words[:12]), assumed_payoff=" ".join(words[-12:]),
        )
