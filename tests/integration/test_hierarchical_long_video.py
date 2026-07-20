from pathlib import Path

import pytest

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import Transcript, TranscriptSegment
from creator_assistant.services.shorts.hierarchical_analyzer import HierarchicalLongVideoAnalyzer
from creator_assistant.services.shorts.semantic_backend import SemanticScorerBackend
from creator_assistant.services.shorts.semantic_cache import SemanticCache
from creator_assistant.services.shorts.semantic_models import GlobalSelectionResponse, SemanticCandidateInput, SemanticCandidateScore


def transcript_two_hours() -> Transcript:
    segments = []
    for index in range(120):
        marker = "начало" if index == 2 else "середина" if index == 60 else "финал" if index == 116 else "развитие"
        text = (f"эпизод {index} {marker} цель проблема действие результат " * 14).strip()
        segments.append(TranscriptSegment(index, index * 60.0, (index + 1) * 60.0 - .1, text))
    return Transcript("ru", 7200, " ".join(item.text for item in segments), segments)


def input_at(candidate_id: str, start: float) -> SemanticCandidateInput:
    return SemanticCandidateInput(
        candidate_id=candidate_id, start=start, end=start + 45, duration=45,
        transcript=f"{candidate_id} hook конфликт развитие развязка", heuristic_score=80,
        speech_density=1, scene_activity=.2, audio_activity=.2, pause_count=0,
    )


def score(item: SemanticCandidateInput) -> SemanticCandidateScore:
    return SemanticCandidateScore(
        candidate_id=item.candidate_id, semantic_score=90, hook_score=90,
        context_independence=90, conflict_score=90, development_score=90,
        payoff_score=90, emotion_score=80, entertainment_score=90, usefulness_score=60,
        retention_score=90, completeness_score=90, moment_type="achievement",
        verdict="strong", reason="complete event", weaknesses=[],
        suggested_start=item.start, suggested_end=item.end,
    )


class Backend(SemanticScorerBackend):
    name = "ollama"
    model = "qwen3.6:35b-a3b"
    calls = 0
    fail_after = 0

    def list_models(self): return []
    def global_select(self, candidates, count, cancellation, *, think=False):
        return GlobalSelectionResponse(candidate_ids=[item["candidate_id"] for item in candidates[:count]])
    def evaluate(self, candidates, cancellation, *, think=False):
        self.calls += 1
        if self.fail_after and self.calls > self.fail_after:
            raise RuntimeError("artificial interruption")
        return [score(item) for item in candidates]


def test_two_hour_transcript_is_fully_blocked_keeps_boundaries_and_resumes(tmp_path: Path):
    transcript = transcript_two_hours()
    analyzer = HierarchicalLongVideoAnalyzer()
    blocks = analyzer.split(transcript, target_tokens=2200, overlap_seconds=75)
    assert blocks[0].start == 0
    assert blocks[-1].end > 7100
    assert all(left.end >= right.start for left, right in zip(blocks, blocks[1:]))
    inputs = [input_at("early", 120), input_at("middle", 3600), input_at("boundary", blocks[1].start - 10), input_at("late", 6960)]
    cache = SemanticCache(tmp_path / "long-cache.json")
    failing = Backend(); failing.fail_after = 2
    with pytest.raises(RuntimeError, match="interruption"):
        analyzer.analyse(inputs, transcript, failing, cache, CancellationToken(), {
            "model": "qwen3.6:35b-a3b", "context_length": 32768,
            "block_target_tokens": 2200, "block_overlap_seconds": 75, "cache": True,
        })
    healthy = Backend()
    result = analyzer.analyse(inputs, transcript, healthy, cache, CancellationToken(), {
        "model": "qwen3.6:35b-a3b", "context_length": 32768,
        "block_target_tokens": 2200, "block_overlap_seconds": 75, "cache": True,
    })
    assert {item.candidate_id for item in result.scores} == {"early", "middle", "boundary", "late"}
    assert result.cache_hits >= 2
    assert result.resumed is True
    assert result.model == "qwen3.6:35b-a3b"
    assert result.context_length == 32768
