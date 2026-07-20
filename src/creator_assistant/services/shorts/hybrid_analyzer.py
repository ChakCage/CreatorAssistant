from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any, Dict, List

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import AudioFeatures, Candidate, Scene, Transcript
from creator_assistant.services.shorts.duplicate_filter import DuplicateFilter
from creator_assistant.services.shorts.semantic_backend import (
    PROMPT_VERSION,
    SemanticBackendError,
    SemanticScorerBackend,
    MODE_PROFILES,
    choose_installed_model,
)
from creator_assistant.services.shorts.semantic_cache import SemanticCache, semantic_cache_payload
from creator_assistant.services.shorts.semantic_models import SemanticCandidateInput, SemanticCandidateScore
from creator_assistant.services.shorts.hierarchical_analyzer import HierarchicalLongVideoAnalyzer, estimate_tokens


@dataclass
class HybridAnalysisResult:
    candidates: List[Candidate]
    used_ai: bool
    cache_hit: bool = False
    fallback_reason: str = ""
    backend: str = "disabled"
    model: str = ""
    model_digest: str = ""
    quantization: str = ""
    analysis_mode: str = "balanced"
    cache_key: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    rejected: List[Dict[str, Any]] = field(default_factory=list)


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
        mode = str(settings.get("mode", "balanced"))
        profile = MODE_PROFILES.get(mode, MODE_PROFILES["balanced"])
        preliminary_count = max(requested_count, int(settings.get("preliminary_count", profile["preliminary_count"])))
        duration = max((item.end for item in scored), default=0.0)
        preliminary = self._preliminary_pool(scored, preliminary_count, duration)
        clustered, duplicates_removed = self.duplicate_filter.filter_with_diagnostics(preliminary, preliminary_count)
        diagnostics: Dict[str, Any] = {
            "transcript_blocks": 1,
            "found_per_block": [len(preliminary)],
            "found_locally": len(preliminary),
            "before_deduplication": len(preliminary),
            "duplicates_removed": duplicates_removed,
            "removed_by_score": 0,
            "sent_to_global_ranking": len(clustered),
            "requested_count": requested_count,
        }
        for item in clustered:
            item.heuristic_score = item.score
            item.final_score = item.score
        if not settings.get("enabled", False) or backend.name == "disabled":
            result = self._temporally_diverse(clustered, requested_count, duration)
            diagnostics.update({"model_returned": 0, "shown_to_user": len(result)})
            return HybridAnalysisResult(result, False, backend=backend.name, diagnostics=diagnostics)

        context_window = {"fast": 8, "balanced": 20, "deep": 40, "quality": 40}.get(mode, 20)
        inputs = [self._input(item, transcript, scenes, audio, content_type, context_window) for item in clustered]
        model = str(settings.get("model", getattr(backend, "model", "")))
        cached = None
        hierarchy_metrics: Dict[str, Any] = {}
        try:
            model_info = None
            if hasattr(backend, "installed_models"):
                installed = backend.installed_models()
                strict_model = bool(settings.get("strict_model", True))
                model_info = next((item for item in installed if item.name == model), None)
                if model_info is None and not strict_model:
                    model_info = choose_installed_model(installed, model)
                if model_info is None:
                    raise SemanticBackendError(
                        f"Выбранная модель Ollama «{model}» не установлена. "
                        "Автоматическая замена другой моделью запрещена."
                    )
                backend.model = model_info.name
                model = model_info.name
                settings["_resolved_model"] = model
                if hasattr(backend, "prepare"):
                    backend.prepare(cancellation)
            digest = getattr(model_info, "digest", "") or str(settings.get("model_digest", ""))
            quantization = getattr(model_info, "quantization", "") or str(settings.get("model_quantization", ""))
            transcript_hash = hashlib.sha256(transcript.text.encode("utf-8")).hexdigest()
            payload = semantic_cache_payload(
                inputs, model=model, content_type=content_type, digest=digest,
                quantization=quantization, analysis_mode=mode, transcript_hash=transcript_hash,
            )
            cache_key = cache.key(payload)
            cached = cache.get(payload) if settings.get("cache", True) else None
            if cached:
                semantic = [SemanticCandidateScore.model_validate(item) for item in cached.get("results", [])]
                selected_ids = [str(item) for item in cached.get("selected_ids", [])]
                cache_hit = True
            else:
                if not hasattr(backend, "installed_models") and hasattr(backend, "check"):
                    backend.check(cancellation)
                token_estimate = estimate_tokens(transcript.text)
                if token_estimate > int(settings.get("long_video_threshold_tokens", 16000)):
                    hierarchical = HierarchicalLongVideoAnalyzer().analyse(
                        inputs, transcript, backend, cache, cancellation, settings,
                    )
                    semantic = hierarchical.scores
                    hierarchy_metrics = {
                        **hierarchical.diagnostics,
                        "transcript_tokens_estimated": token_estimate,
                        "block_cache_hits": hierarchical.cache_hits,
                        "resumed": hierarchical.resumed,
                        "context_length": hierarchical.context_length,
                    }
                    diagnostics.update({
                        "transcript_blocks": int(hierarchical.diagnostics.get("block_count", 0)),
                        "found_per_block": list(hierarchical.diagnostics.get("found_per_block", [])),
                        "found_locally": int(hierarchical.diagnostics.get("found_locally", 0)),
                    })
                else:
                    semantic = self._evaluate_batches(backend, inputs, cancellation, settings)
                selected_ids = []
                cache_hit = False
            by_id = {item.candidate_id: item for item in semantic}
            if set(by_id) != {item.id for item in clustered}:
                raise SemanticBackendError("AI-оценка не соответствует набору кандидатов.")
            self._combine(clustered, by_id, settings)
            if not selected_ids:
                selected_ids, model_returned = self._global_selection(
                    backend, clustered, requested_count, cancellation, settings, duration,
                )
                if settings.get("cache", True):
                    cache.put(payload, {
                        "prompt_version": PROMPT_VERSION,
                        "model": model, "digest": digest, "quantization": quantization,
                        "analysis_mode": mode,
                        "results": [item.model_dump(mode="json") for item in semantic],
                        "selected_ids": selected_ids,
                    })
            else:
                model_returned = len(selected_ids)
            result = self._ordered(clustered, selected_ids, requested_count)
            diagnostics.update({"model_returned": model_returned, "shown_to_user": len(result)})
            rejected = self._rejected(clustered, result, duplicates_removed)
            metrics = getattr(backend, "last_metrics", None)
            runtime = dict(getattr(backend, "last_runtime_info", {}) or {})
            size = int(runtime.get("size", 0) or 0)
            size_vram = int(runtime.get("size_vram", 0) or 0)
            runtime_metrics = {
                "allocated_context": int(runtime.get("context_length", 0) or 0),
                "size": size,
                "size_vram": size_vram,
                "gpu_percent": round(100 * size_vram / size, 1) if size else None,
                "cpu_percent": round(100 * (size - size_vram) / size, 1) if size else None,
            }
            return HybridAnalysisResult(
                result, True, cache_hit, backend=backend.name, model=model,
                model_digest=digest, quantization=quantization, analysis_mode=mode,
                cache_key=cache_key,
                metrics={**(metrics.__dict__.copy() if metrics else {}), **runtime_metrics, **hierarchy_metrics},
                diagnostics=diagnostics, rejected=rejected,
            )
        except Exception as exc:
            if cancellation.is_cancelled:
                cancellation.raise_if_cancelled()
            if not settings.get("fallback", False):
                raise
            message = str(exc).strip() or type(exc).__name__
            for item in clustered:
                item.selection_source = "heuristic_fallback"
                item.warnings.append(f"Локальная AI-оценка недоступна: {message}")
            return HybridAnalysisResult(
                clustered[:requested_count], False, bool(cached), message, backend.name, model,
                analysis_mode=mode, metrics={}, diagnostics={**diagnostics, "shown_to_user": min(requested_count, len(clustered))},
            )

    @staticmethod
    def _evaluate_batches(backend, inputs, cancellation, settings):
        profile = MODE_PROFILES.get(str(settings.get("mode", "balanced")), MODE_PROFILES["balanced"])
        batch_size = max(1, min(12, int(settings.get("batch_size", profile["batch_size"]))))
        values = []
        for offset in range(0, len(inputs), batch_size):
            cancellation.raise_if_cancelled()
            values.extend(backend.evaluate(
                inputs[offset:offset + batch_size], cancellation,
                think=bool(profile["think"]),
            ))
        return values

    @staticmethod
    def _global_selection(backend, candidates, count, cancellation, settings, duration=0.0):
        ranked = sorted(candidates, key=lambda item: -item.final_score)
        profile = MODE_PROFILES.get(str(settings.get("mode", "balanced")), MODE_PROFILES["balanced"])
        passes = int(profile["global_passes"]) if settings.get("global_comparison", True) else 0
        if passes <= 0:
            values = HybridCandidateAnalyzer._temporally_diverse(ranked, count, duration)
            return [item.id for item in values], len(values)
        summaries = [{
            "candidate_id": item.id, "start": item.start, "end": item.end,
            "text": item.text, "heuristic_score": item.heuristic_score,
            "semantic_score": item.semantic_score, "final_score": item.final_score,
            "moment_type": item.ai_moment_type, "reason": item.ai_reason,
        } for item in ranked]
        selected = []
        for _pass in range(passes):
            selection = backend.global_select(summaries, count, cancellation, think=bool(profile["think"]))
            selected = selection.candidate_ids
            order = {candidate_id: index for index, candidate_id in enumerate(selected)}
            summaries.sort(key=lambda item: order.get(item["candidate_id"], len(order)))
        model_returned = len(dict.fromkeys(selected))
        # Structured output intentionally permits fewer than requested.  Treat
        # the model list as priority, not as permission to discard the pool.
        selected = list(dict.fromkeys(selected))
        selected_set = set(selected)
        remaining = [item for item in ranked if item.id not in selected_set]
        selected.extend(item.id for item in HybridCandidateAnalyzer._temporally_diverse(remaining, count - len(selected), duration))
        return selected[:count], model_returned

    @staticmethod
    def _ordered(candidates, selected_ids, count):
        by_id = {item.id: item for item in candidates}
        result = [by_id[item] for item in selected_ids if item in by_id]
        seen = {item.id for item in result}
        result.extend(item for item in sorted(candidates, key=lambda value: -value.final_score) if item.id not in seen)
        return result[:count]

    @staticmethod
    def _preliminary_pool(candidates, count, duration):
        ranked = sorted(candidates, key=lambda item: -item.score)
        if duration <= 1200 or len(ranked) <= count:
            return ranked[:count]
        # Reserve roughly half of the pool for best events from evenly spaced
        # time regions, then fill by score. This prevents a long intro/event
        # cluster from starving the middle and end of a long recording.
        bucket_count = min(max(3, round(duration / 600)), max(3, count // 2))
        chosen = []
        for bucket in range(bucket_count):
            start, end = duration * bucket / bucket_count, duration * (bucket + 1) / bucket_count
            local = [item for item in ranked if start <= (item.start + item.end) / 2 < end]
            if local:
                chosen.append(local[0])
        seen = {id(item) for item in chosen}
        chosen.extend(item for item in ranked if id(item) not in seen)
        return chosen[:count]

    @staticmethod
    def _temporally_diverse(candidates, count, duration):
        if count <= 0:
            return []
        ranked = sorted(candidates, key=lambda item: -item.final_score)
        if duration <= 1200 or len(ranked) <= 2:
            return ranked[:count]
        regions = min(5, max(3, count))
        selected = []
        for region in range(regions):
            start, end = duration * region / regions, duration * (region + 1) / regions
            local = [item for item in ranked if start <= (item.start + item.end) / 2 < end and item not in selected]
            if local:
                selected.append(local[0])
            if len(selected) >= count:
                return selected
        selected.extend(item for item in ranked if item not in selected)
        return selected[:count]

    @staticmethod
    def _rejected(pool, selected, duplicates_removed):
        selected_ids = {item.id for item in selected}
        values = [
            {"candidate_id": item.id, "start": item.start, "end": item.end, "score": item.final_score,
             "reason": "Не вошёл в запрошенный лимит после глобального ранжирования"}
            for item in pool if item.id not in selected_ids
        ]
        if duplicates_removed:
            values.append({"candidate_id": "duplicates", "reason": f"Объединено настоящих временных дублей: {duplicates_removed}"})
        return values

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
            item.ai_model = str(settings.get("_resolved_model", ""))
            item.ai_mode = str(settings.get("mode", "balanced"))
            item.reasons.append(f"AI: {value.reason}")
            item.warnings.extend(value.weaknesses)
            if value.suggested_start is not None and value.suggested_end is not None:
                bounds = [round(value.suggested_start, 3), round(value.suggested_end, 3)]
                if value.suggested_end > value.suggested_start and bounds not in item.alternatives:
                    item.alternatives.append(bounds)

    @staticmethod
    def _input(candidate, transcript, scenes, audio, content_type, context_window=20):
        before = [s.text.strip() for s in transcript.segments if candidate.start - context_window <= s.end <= candidate.start]
        after = [s.text.strip() for s in transcript.segments if candidate.end <= s.start <= candidate.end + context_window]
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
