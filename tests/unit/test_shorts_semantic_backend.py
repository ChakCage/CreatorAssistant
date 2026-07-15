import json

import pytest

from creator_assistant.domain.errors import JobCancelledError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.services.shorts.semantic_backend import (
    OllamaModelInfo,
    OllamaSemanticScorer,
    SemanticBackendError,
    SemanticResponseError,
    choose_installed_model,
)
from creator_assistant.services.shorts.semantic_cache import SemanticCache, semantic_cache_payload
from creator_assistant.services.shorts.semantic_models import SemanticCandidateInput


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.value, ensure_ascii=False).encode("utf-8")


def candidate(candidate_id="c1", transcript="сильный игровой момент"):
    return SemanticCandidateInput(
        candidate_id=candidate_id, start=10, end=40, duration=30, transcript=transcript,
        heuristic_score=71, speech_density=0.7, scene_activity=0.6, audio_activity=0.8,
        pause_count=1,
    )


def score(candidate_id="c1"):
    return {
        "candidate_id": candidate_id, "semantic_score": 84, "hook_score": 80,
        "context_independence": 82, "conflict_score": 75, "development_score": 81,
        "payoff_score": 90, "emotion_score": 76, "entertainment_score": 88,
        "usefulness_score": 50, "retention_score": 86, "completeness_score": 89,
        "moment_type": "achievement", "verdict": "Сильный момент",
        "reason": "Есть понятная цель и завершение.", "weaknesses": [],
        "suggested_start": 11, "suggested_end": 39,
    }


def test_lists_local_model_and_rejects_remote_endpoint():
    backend = OllamaSemanticScorer(opener=lambda *_args, **_kwargs: Response({
        "models": [{"name": "qwen3:14b", "size": 9_000_000_000}]
    }))
    assert backend.model_info()["name"] == "qwen3:14b"
    with pytest.raises(SemanticBackendError, match="локальный"):
        OllamaSemanticScorer(endpoint="https://example.com", opener=lambda *_a, **_k: None).list_models()


def test_installed_model_metadata_and_recommendation():
    raw = {
        "name": "qwen3.6:35b-a3b",
        "size": 24_000_000_000,
        "digest": "abc123",
        "modified_at": "2026-07-15T10:00:00Z",
        "details": {"parameter_size": "35B", "quantization_level": "Q4_K_M"},
        "capabilities": ["completion", "thinking"],
    }
    info = OllamaModelInfo.from_api(raw)
    assert info.name == "qwen3.6:35b-a3b"
    assert info.parameter_size == "35B"
    assert info.quantization == "Q4_K_M"
    assert info.digest == "abc123"
    assert info.recommendation == "Глубокий анализ"
    assert "35B" in info.display


def test_choose_installed_model_prefers_saved_then_deep_then_fast():
    fast = OllamaModelInfo("qwen3:14b")
    deep = OllamaModelInfo("qwen3.6:35b-a3b")
    other = OllamaModelInfo("llama3.1:8b")
    assert choose_installed_model([fast, deep], "qwen3:14b") == fast
    assert choose_installed_model([fast, deep], "missing") == deep
    assert choose_installed_model([other], "missing") == other


def test_structured_score_uses_schema_and_records_metrics():
    requests = []

    def opener(request, **_kwargs):
        requests.append(json.loads(request.data))
        return Response({"message": {"content": json.dumps({"results": [score()]})},
                         "eval_count": 12, "eval_duration": 100})

    backend = OllamaSemanticScorer(opener=opener)
    result = backend.evaluate([candidate()], CancellationToken())
    assert result[0].semantic_score == 84
    assert requests[0]["format"]["type"] == "object"
    assert requests[0]["options"]["temperature"] == 0
    assert requests[0]["options"]["num_ctx"] == 16384
    assert backend.last_metrics.eval_count == 12


def test_structured_score_honors_context_and_thinking_flag():
    requests = []

    def opener(request, **_kwargs):
        requests.append(json.loads(request.data))
        return Response({"message": {"content": json.dumps({"results": [score()]})}})

    backend = OllamaSemanticScorer(opener=opener, context_length=32768)
    backend.evaluate([candidate()], CancellationToken(), think=True)
    assert requests[0]["think"] is True
    assert requests[0]["options"]["num_ctx"] == 32768


def test_content_type_guidance_is_added_to_prompt():
    requests = []

    def opener(request, **_kwargs):
        requests.append(json.loads(request.data))
        return Response({"message": {"content": json.dumps({"results": [score()]})}})

    item = candidate()
    item.content_type = "education"
    OllamaSemanticScorer(opener=opener).evaluate([item], CancellationToken())
    prompt = requests[0]["messages"][1]["content"]
    assert "Обучающий ролик" in prompt
    assert '"content_type":"education"' in prompt


def test_invalid_structured_response_retries_once():
    calls = 0

    def opener(_request, **_kwargs):
        nonlocal calls
        calls += 1
        content = "not-json" if calls == 1 else json.dumps({"results": [score()]})
        return Response({"message": {"content": content}})

    assert OllamaSemanticScorer(opener=opener).evaluate([candidate()], CancellationToken())
    assert calls == 2


def test_rejects_unknown_candidate_ids():
    backend = OllamaSemanticScorer(opener=lambda *_a, **_k: Response({
        "message": {"content": json.dumps({"results": [score("invented")]})}
    }))
    with pytest.raises(SemanticResponseError, match="candidate_id"):
        backend.evaluate([candidate()], CancellationToken())


def test_timeout_is_converted_to_backend_error():
    def timeout(*_args, **_kwargs):
        raise TimeoutError("slow")

    with pytest.raises(SemanticBackendError, match="недоступен"):
        OllamaSemanticScorer(opener=timeout).list_models()


def test_cancellation_is_checked_after_api_response():
    token = CancellationToken()

    def opener(*_args, **_kwargs):
        token.cancel()
        return Response({"message": {"content": json.dumps({"results": [score()]})}})

    with pytest.raises(JobCancelledError):
        OllamaSemanticScorer(opener=opener).evaluate([candidate()], token)


def test_semantic_cache_is_atomic_and_ignores_render_settings(tmp_path):
    cache = SemanticCache(tmp_path / "semantic.json")
    payload = semantic_cache_payload([candidate()], model="qwen3:14b", content_type="gaming")
    cache.put(payload, {"results": [score()]})
    assert SemanticCache(tmp_path / "semantic.json").get(payload)["results"][0]["candidate_id"] == "c1"
    same = dict(payload, crop_mode="blur", subtitle_style="gaming")
    assert SemanticCache.key(payload) != SemanticCache.key(same)
    assert SemanticCache.key(payload) != SemanticCache.key(dict(payload, model="other"))


def test_semantic_cache_key_includes_model_identity_and_mode():
    payload = semantic_cache_payload(
        [candidate()],
        model="qwen3:14b",
        digest="digest-a",
        quantization="Q4_K_M",
        analysis_mode="balanced",
        transcript_hash="transcript-a",
        content_type="gaming",
    )
    assert SemanticCache.key(payload) != SemanticCache.key(dict(payload, digest="digest-b"))
    assert SemanticCache.key(payload) != SemanticCache.key(dict(payload, analysis_mode="deep"))
    assert SemanticCache.key(payload) != SemanticCache.key(dict(payload, transcript_hash="transcript-b"))
