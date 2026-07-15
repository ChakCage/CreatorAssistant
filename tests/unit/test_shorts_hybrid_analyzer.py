from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import AudioFeatures, Candidate, Scene, Transcript, TranscriptSegment
from creator_assistant.services.shorts.duplicate_filter import DuplicateFilter
from creator_assistant.services.shorts.hybrid_analyzer import HybridCandidateAnalyzer
from creator_assistant.services.shorts.semantic_backend import SemanticBackendError, SemanticScorerBackend
from creator_assistant.services.shorts.semantic_cache import SemanticCache
from creator_assistant.services.shorts.semantic_models import GlobalSelectionResponse, SemanticCandidateScore


def semantic(candidate_id, value):
    return SemanticCandidateScore(
        candidate_id=candidate_id, semantic_score=value, hook_score=value,
        context_independence=value, conflict_score=value, development_score=value,
        payoff_score=value, emotion_score=value, entertainment_score=value,
        usefulness_score=value, retention_score=value, completeness_score=value,
        moment_type="tense", verdict="Готовый сюжет", reason="Есть hook и развязка.",
        weaknesses=[], suggested_start=None, suggested_end=None,
    )


class FakeBackend(SemanticScorerBackend):
    name = "fake-local"

    def __init__(self, scores=None, fail=False):
        self.scores = scores or {}
        self.fail = fail
        self.evaluate_calls = 0
        self.global_calls = 0

    def list_models(self):
        return [{"name": "qwen3:14b"}]

    def check(self, cancellation=None):
        if self.fail:
            raise SemanticBackendError("offline")
        return self.list_models()[0]

    def evaluate(self, candidates, cancellation, *, think=False):
        self.evaluate_calls += 1
        return [semantic(item.candidate_id, self.scores.get(item.candidate_id, 50)) for item in candidates]

    def global_select(self, candidates, count, cancellation, *, think=False):
        self.global_calls += 1
        ranked = sorted(candidates, key=lambda item: -item["semantic_score"])
        return GlobalSelectionResponse(candidate_ids=[item["candidate_id"] for item in ranked[:count]])


def inputs():
    candidates = [
        Candidate("a", 0, 20, 80, "начало первая история завершение", reasons=["Сильное начало"]),
        Candidate("b", 30, 50, 70, "другая напряженная история победа"),
        Candidate("c", 60, 80, 90, "третья история без сильной концовки"),
    ]
    transcript = Transcript("ru", 90, "", [
        TranscriptSegment(1, 0, 20, candidates[0].text),
        TranscriptSegment(2, 30, 50, candidates[1].text),
        TranscriptSegment(3, 60, 80, candidates[2].text),
    ])
    return candidates, transcript, [Scene(0, 30), Scene(30, 60), Scene(60, 90)], AudioFeatures()


def settings(**overrides):
    value = {
        "enabled": True, "model": "qwen3:14b", "cache": True, "fallback": True,
        "global_comparison": True, "preliminary_count": 40, "batch_size": 2,
        "weights": {"semantic": .55, "heuristic": .25, "activity": .15, "uniqueness": .05},
    }
    value.update(overrides)
    return value


def test_hybrid_reranks_semantically_and_uses_global_selection(tmp_path):
    candidates, transcript, scenes, audio = inputs()
    backend = FakeBackend({"short_001": 40, "short_002": 95, "short_003": 60})
    result = HybridCandidateAnalyzer(DuplicateFilter()).analyse(
        candidates, transcript, scenes, audio, backend, settings(),
        SemanticCache(tmp_path / "ai.json"), CancellationToken(), content_type="gaming", requested_count=2,
    )
    assert result.used_ai
    assert result.candidates[0].semantic_score == 95
    assert result.candidates[0].selection_source == "hybrid_ai"
    assert backend.evaluate_calls == 2  # batches of two
    assert backend.global_calls == 1


def test_hybrid_cache_skips_all_model_calls(tmp_path):
    cache = SemanticCache(tmp_path / "ai.json")
    first_backend = FakeBackend({"short_001": 80, "short_002": 70, "short_003": 60})
    values = inputs()
    HybridCandidateAnalyzer(DuplicateFilter()).analyse(
        *values, first_backend, settings(), cache, CancellationToken(),
        content_type="gaming", requested_count=2,
    )
    second_backend = FakeBackend(fail=True)
    values = inputs()
    result = HybridCandidateAnalyzer(DuplicateFilter()).analyse(
        *values, second_backend, settings(), SemanticCache(tmp_path / "ai.json"), CancellationToken(),
        content_type="gaming", requested_count=2,
    )
    assert result.cache_hit and result.used_ai
    assert second_backend.evaluate_calls == second_backend.global_calls == 0


def test_hybrid_falls_back_without_losing_candidates(tmp_path):
    candidates, transcript, scenes, audio = inputs()
    result = HybridCandidateAnalyzer(DuplicateFilter()).analyse(
        candidates, transcript, scenes, audio, FakeBackend(fail=True), settings(),
        SemanticCache(tmp_path / "ai.json"), CancellationToken(), content_type="gaming", requested_count=2,
    )
    assert not result.used_ai
    assert "offline" in result.fallback_reason
    assert len(result.candidates) == 2
    assert all(item.selection_source == "heuristic_fallback" for item in result.candidates)


def test_disabled_ai_preserves_heuristic_order(tmp_path):
    candidates, transcript, scenes, audio = inputs()
    result = HybridCandidateAnalyzer(DuplicateFilter()).analyse(
        candidates, transcript, scenes, audio, FakeBackend(), settings(enabled=False),
        SemanticCache(tmp_path / "ai.json"), CancellationToken(), content_type="gaming", requested_count=2,
    )
    assert not result.used_ai
    assert [item.heuristic_score for item in result.candidates] == [90, 80]

