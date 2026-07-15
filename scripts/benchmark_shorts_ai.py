"""A/B benchmark for the heuristic and local semantic Shorts ranking pipelines."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import (
    AudioFeatures, Scene, Transcript, TranscriptSegment, TranscriptWord,
)
from creator_assistant.services.shorts.candidate_generator import CandidateGenerator, CandidateSettings
from creator_assistant.services.shorts.candidate_scorer import HeuristicCandidateScorer
from creator_assistant.services.shorts.duplicate_filter import DuplicateFilter, overlap_ratio
from creator_assistant.services.shorts.hybrid_analyzer import HybridCandidateAnalyzer
from creator_assistant.services.shorts.semantic_backend import MODE_PROFILES, OllamaSemanticScorer
from creator_assistant.services.shorts.semantic_cache import SemanticCache


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_inputs(folder: Path):
    raw = _load(folder / "transcript.json")
    segments = []
    for item in raw.get("segments", []):
        words = [TranscriptWord(**word) for word in item.get("words", [])]
        segments.append(TranscriptSegment(**{**item, "words": words}))
    transcript = Transcript(**{**raw, "segments": segments})
    scenes = [Scene(**item) for item in _load(folder / "scenes.json")]
    audio = AudioFeatures(**_load(folder / "audio_features.json"))
    return transcript, scenes, audio


def run(folder: Path, cache_path: Path, endpoint: str, model: str, final_count: int, mode: str):
    transcript, scenes, audio = load_inputs(folder)
    profile = MODE_PROFILES.get(mode, MODE_PROFILES["balanced"])
    candidate_settings = CandidateSettings(count=15, content_type="gaming")
    raw = CandidateGenerator().generate(transcript, scenes, audio, candidate_settings)
    scored = [HeuristicCandidateScorer().score(item, scenes, audio) for item in raw]
    heuristic = DuplicateFilter().filter(scored, final_count)

    # Regenerate objects because filtering mutates IDs and alternatives.
    raw = CandidateGenerator().generate(transcript, scenes, audio, candidate_settings)
    scored = [HeuristicCandidateScorer().score(item, scenes, audio) for item in raw]
    backend = OllamaSemanticScorer(
        endpoint=endpoint,
        model=model,
        timeout=600,
        context_length=int(profile["context_length"]),
    )
    settings = {
        "enabled": True, "model": model, "mode": mode,
        "preliminary_count": int(profile["preliminary_count"]),
        "batch_size": int(profile["batch_size"]), "cache": True, "fallback": False, "global_comparison": True,
        "weights": {"semantic": .55, "heuristic": .25, "activity": .15, "uniqueness": .05},
    }
    started = time.perf_counter()
    result = HybridCandidateAnalyzer(DuplicateFilter()).analyse(
        scored, transcript, scenes, audio, backend, settings, SemanticCache(cache_path),
        CancellationToken(), content_type="gaming", requested_count=final_count,
    )
    elapsed = time.perf_counter() - started
    duplicate_pairs = sum(
        overlap_ratio(left, right) >= .62
        for index, left in enumerate(result.candidates)
        for right in result.candidates[index + 1:]
    )
    return {
        "elapsed_seconds": round(elapsed, 3), "cache_hit": result.cache_hit,
        "used_ai": result.used_ai, "fallback_reason": result.fallback_reason,
        "model": result.model, "model_digest": result.model_digest,
        "quantization": result.quantization, "mode": result.analysis_mode,
        "cache_key": result.cache_key,
        "duplicate_pairs": duplicate_pairs,
        "heuristic": [{"id": item.id, "start": item.start, "end": item.end, "score": item.score, "text": item.text[:160]} for item in heuristic],
        "hybrid": [{
            "id": item.id, "start": item.start, "end": item.end,
            "heuristic": item.heuristic_score, "semantic": item.semantic_score,
            "final": item.final_score, "type": item.ai_moment_type,
            "verdict": item.ai_verdict, "reason": item.ai_reason, "text": item.text[:160],
        } for item in result.candidates],
        "ollama_metrics": asdict(backend.last_metrics),
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("analysis", type=Path)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--models", nargs="+", help="Run the same benchmark for several installed Ollama model tags.")
    parser.add_argument("--mode", default="balanced", choices=("fast", "balanced", "deep"))
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    models = args.models or [args.model]
    if len(models) == 1:
        value = run(args.analysis, args.cache, args.endpoint, models[0], args.count, args.mode)
    else:
        value = {
            "analysis": str(args.analysis),
            "mode": args.mode,
            "models": [
                run(args.analysis, args.cache.with_name(f"{args.cache.stem}-{model.replace(':', '_')}{args.cache.suffix}"), args.endpoint, model, args.count, args.mode)
                for model in models
            ],
        }
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
