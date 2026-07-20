from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import AudioFeatures, Transcript, TranscriptSegment
from creator_assistant.services.shorts.candidate_generator import CandidateGenerator, CandidateSettings
from creator_assistant.services.shorts.duplicate_filter import DuplicateFilter
from creator_assistant.services.shorts.hybrid_analyzer import HybridCandidateAnalyzer
from creator_assistant.services.shorts.semantic_cache import SemanticCache
from creator_assistant.services.shorts.semantic_models import SemanticCandidateScore


class CachedQwenBackend:
    name = "ollama"
    model = "qwen3.6:35b-a3b"
    last_metrics = None
    last_runtime_info = {}

    def check(self, _token):
        return True

    def evaluate(self, values, _token, think=True):
        return [SemanticCandidateScore(
            candidate_id=item.candidate_id, semantic_score=88, hook_score=88,
            context_independence=88, conflict_score=88, development_score=88,
            payoff_score=88, emotion_score=80, entertainment_score=90,
            usefulness_score=70, retention_score=88, completeness_score=90,
            moment_type="achievement", verdict="Сильный", reason="Отдельное завершённое событие",
        ) for item in values]

    def global_select(self, values, count, _token, think=True):
        # Regression fixture: the model may legally return only one priority ID.
        return SimpleNamespace(candidate_ids=[values[0]["candidate_id"]])


def test_50_minute_pipeline_keeps_diverse_events_and_rebuilds_from_ai_cache(tmp_path):
    segments = []
    for index in range(100):
        start = index * 30.0
        event = index // 17
        text = (f"Событие {event}: уникальная завязка проблема развитие и яркая развязка победа. " * 8).strip()
        segments.append(TranscriptSegment(index, start, start + 29.0, text))
    transcript = Transcript("ru", 3000.0, " ".join(item.text for item in segments), segments)
    settings = CandidateSettings(minimum=25, desired=60, maximum=180, count=15)
    generated = CandidateGenerator().generate(transcript, [], AudioFeatures(), settings)
    assert min(item.start for item in generated) < 300
    assert max(item.start for item in generated) > 2600
    for index, item in enumerate(generated):
        item.score = item.heuristic_score = item.final_score = 75 + index % 20
    analyzer = HybridCandidateAnalyzer(DuplicateFilter())
    ai = {"enabled": True, "model": "qwen3.6:35b-a3b", "mode": "balanced", "cache": True,
          "long_video_threshold_tokens": 1000, "preliminary_count": 60}
    cache = SemanticCache(tmp_path / "semantic.json")
    first = analyzer.analyse(deepcopy(generated), transcript, [], AudioFeatures(), CachedQwenBackend(), ai, cache, CancellationToken(), content_type="gaming", requested_count=15)
    second = analyzer.analyse(deepcopy(generated), transcript, [], AudioFeatures(), CachedQwenBackend(), ai, SemanticCache(tmp_path / "semantic.json"), CancellationToken(), content_type="gaming", requested_count=15)
    assert len(first.candidates) >= 4
    assert len({int(((item.start + item.end) / 2) // 600) for item in first.candidates}) >= 3
    assert first.diagnostics["model_returned"] == 1
    assert first.diagnostics["shown_to_user"] > 1
    assert first.model == "qwen3.6:35b-a3b"
    assert second.cache_hit is True
    assert [item.start for item in second.candidates] == [item.start for item in first.candidates]
