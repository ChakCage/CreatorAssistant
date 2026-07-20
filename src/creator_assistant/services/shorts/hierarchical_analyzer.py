from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import Transcript, TranscriptSegment
from creator_assistant.services.shorts.semantic_backend import PROMPT_VERSION, SemanticScorerBackend
from creator_assistant.services.shorts.semantic_cache import SemanticCache
from creator_assistant.services.shorts.semantic_models import SemanticCandidateInput, SemanticCandidateScore


def estimate_tokens(text: str) -> int:
    """Conservative language-neutral approximation suitable for Russian transcripts."""
    return max(1, round(len(text or "") / 3.2))


@dataclass(frozen=True)
class TranscriptBlock:
    index: int
    start: float
    end: float
    text: str
    transcript_hash: str
    estimated_tokens: int


@dataclass
class HierarchicalAnalysisResult:
    scores: list[SemanticCandidateScore]
    blocks: list[TranscriptBlock]
    completed_blocks: int
    cache_hits: int
    retries: int = 0
    resumed: bool = False
    model: str = ""
    context_length: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)


class HierarchicalLongVideoAnalyzer:
    """Analyse long transcripts in overlapping semantic/time blocks with resumable cache."""

    def split(
        self,
        transcript: Transcript,
        *,
        target_tokens: int = 14000,
        overlap_seconds: float = 75.0,
    ) -> list[TranscriptBlock]:
        segments = [item for item in transcript.segments if item.text.strip()]
        if not segments:
            return []
        target_tokens = max(1000, int(target_tokens))
        blocks: list[TranscriptBlock] = []
        cursor = 0
        while cursor < len(segments):
            chosen: list[TranscriptSegment] = []
            tokens = 0
            index = cursor
            while index < len(segments):
                item_tokens = estimate_tokens(segments[index].text)
                if chosen and tokens + item_tokens > target_tokens:
                    break
                chosen.append(segments[index])
                tokens += item_tokens
                index += 1
            if not chosen:
                chosen = [segments[cursor]]
                index = cursor + 1
            text = " ".join(item.text.strip() for item in chosen)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            blocks.append(TranscriptBlock(
                index=len(blocks), start=float(chosen[0].start), end=float(chosen[-1].end),
                text=text, transcript_hash=digest, estimated_tokens=estimate_tokens(text),
            ))
            if index >= len(segments):
                break
            boundary = chosen[-1].end - max(0.0, float(overlap_seconds))
            next_cursor = index
            while next_cursor > cursor + 1 and segments[next_cursor - 1].start >= boundary:
                next_cursor -= 1
            cursor = max(cursor + 1, next_cursor)
        return blocks

    def analyse(
        self,
        inputs: list[SemanticCandidateInput],
        transcript: Transcript,
        backend: SemanticScorerBackend,
        cache: SemanticCache,
        cancellation: CancellationToken,
        settings: dict[str, Any],
    ) -> HierarchicalAnalysisResult:
        blocks = self.split(
            transcript,
            target_tokens=int(settings.get("block_target_tokens", 14000)),
            overlap_seconds=float(settings.get("block_overlap_seconds", 75)),
        )
        by_id: dict[str, SemanticCandidateScore] = {}
        cache_hits = 0
        model = str(settings.get("_resolved_model") or settings.get("model") or getattr(backend, "model", ""))
        digest = str(settings.get("model_digest", ""))
        context = int(settings.get("context_length", getattr(backend, "context_length", 32768)))
        for block in blocks:
            cancellation.raise_if_cancelled()
            local = [item for item in inputs if item.end > block.start and item.start < block.end]
            if not local:
                continue
            payload = {
                "scope": "long-video-block",
                "prompt_version": PROMPT_VERSION,
                "model": model,
                "model_digest": digest,
                "num_ctx": context,
                "start": block.start,
                "end": block.end,
                "transcript_hash": block.transcript_hash,
                "candidates": [item.model_dump(mode="json") for item in local],
            }
            stored = cache.get(payload) if settings.get("cache", True) else None
            if stored and stored.get("status") == "validated":
                values = [SemanticCandidateScore.model_validate(item) for item in stored.get("parsed_json", [])]
                cache_hits += 1
            else:
                values = backend.evaluate(local, cancellation, think=True)
                if settings.get("cache", True):
                    cache.put(payload, {
                        "status": "validated",
                        "model": model,
                        "model_digest": digest,
                        "prompt_version": PROMPT_VERSION,
                        "num_ctx": context,
                        "start": block.start,
                        "end": block.end,
                        "transcript_hash": block.transcript_hash,
                        "ai_response": [item.model_dump(mode="json") for item in values],
                        "parsed_json": [item.model_dump(mode="json") for item in values],
                    })
            for value in values:
                current = by_id.get(value.candidate_id)
                if current is None or value.semantic_score > current.semantic_score:
                    by_id[value.candidate_id] = value
        ordered = [by_id[item.candidate_id] for item in inputs if item.candidate_id in by_id]
        return HierarchicalAnalysisResult(
            ordered, blocks, len(blocks), cache_hits, resumed=cache_hits > 0,
            model=model, context_length=context,
            diagnostics={"block_count": len(blocks), "completed_blocks": len(blocks)},
        )
