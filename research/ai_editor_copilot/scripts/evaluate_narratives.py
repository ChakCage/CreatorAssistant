"""Private evaluation runner. No raw media/transcripts ever copied into tracked fixtures."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from ai_editor_copilot.adapters.local_creator_assistant import LocalCreatorAssistantLLMBackend
from ai_editor_copilot.feedback.events import save_json
from ai_editor_copilot.narrative.evidence import load_evidence, file_sha256
from ai_editor_copilot.narrative.pipeline import HierarchicalPlanner, VERSION
from ai_editor_copilot.narrative.baseline import contiguous_baseline
from ai_editor_copilot.narrative.contracts import Refusal
from ai_editor_copilot.narrative.evaluation import load_tasks, metrics, variance, blind_package


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--tasks", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--source-id", help="Optional source subset; task definitions remain unchanged")
    p.add_argument("--task-id", help="Run one unchanged task in a new append-only evaluation directory")
    p.add_argument("--split", choices=["development", "held_out"], default="development")
    p.add_argument("--release-held-out", action="store_true")
    p.add_argument("--runs", type=int, default=3)
    args = p.parse_args()
    private = Path(__file__).resolve().parents[1] / "runs"
    if private not in args.output.resolve().parents or args.runs < 3:
        raise ValueError("use private runs output and at least three independent selections")
    tasks = load_tasks(args.tasks, args.split, args.release_held_out)
    if args.source_id:
        tasks = [t for t in tasks if t.source_id == args.source_id]
    if args.task_id:
        tasks = [t for t in tasks if t.id == args.task_id]
    if not tasks:
        raise ValueError("no matching evaluation tasks")
    if args.output.exists():
        raise FileExistsError("evaluation output is append-only; choose a new directory")
    args.output.mkdir(parents=True)
    module_root = Path(__file__).resolve().parents[1] / "src" / "ai_editor_copilot" / "narrative"
    save_json(args.output / "protocol.json", {"task_manifest_sha256": file_sha256(args.tasks),
        "source_code_sha256": {p.name: file_sha256(p) for p in sorted(module_root.glob("*.py"))},
        "split": args.split, "held_out_released": args.release_held_out,
        "runs": args.runs, "temperature": 0, "model": "qwen3.6:35b-a3b"})
    backend = LocalCreatorAssistantLLMBackend()
    save_json(args.output / "runtime.json", backend.prepare())
    for source_id in sorted({t.source_id for t in tasks}):
        directory = args.dataset / source_id
        source = load_evidence(directory)
        planner = HierarchicalPlanner(backend, args.output.parent / "analysis_cache", args.output / "call_journal")
        candidates, coverage = planner.analyze(source)
        root = args.output / source_id
        save_json(root / "candidate_index.json", [c.model_dump(mode="json") for c in candidates])
        annotations = []
        for c in candidates:
            metadata = c.annotation.model_dump(mode="json")
            annotations.append(metadata)
            for field, value in [("role", c.role), ("entities", c.entities), ("topics", c.topics)]:
                annotations.append({**metadata, "type": field, "value": json.dumps(value, ensure_ascii=False)})
        save_json(root / "semantic_timeline.json", {"source": source.id,
            "raw_transcript_sha256": source.transcript_sha256,
            "annotations": annotations,
            "unknown_modalities": ["visuals", "shot boundaries", "speaker identity"]})
        save_json(root / "coverage.json", coverage)
        save_json(root / "analysis_responses.json", planner.calls.records)
        for task in [t for t in tasks if t.source_id == source_id]:
            results = []
            baseline, baseline_context, _ = contiguous_baseline(source, task.request, task.target_duration, task.minimum_duration)
            save_json(root / task.id / "baseline.json", baseline.model_dump(mode="json"))
            save_json(root / task.id / "baseline_metrics.json", metrics(baseline, source))
            for run in range(args.runs):
                out = root / task.id / f"run_{run + 1:02d}"
                if out.exists():
                    raise FileExistsError("evaluation evidence is append-only; select a new output directory")
                out.mkdir(parents=True)
                planner.calls.records = []  # independent generation; no previous output enters prompt
                context = decision = None
                try:
                    if not coverage["full_transcript_analyzed"]:
                        result = Refusal(reason_codes=["INCOMPLETE_COVERAGE"], explanation="One or more raw segments were not analyzed")
                    else:
                        result, context, decision = planner.select(source, candidates, task.request, task.target_duration, task.minimum_duration)
                except ValueError as exc:
                    code = "CONTEXT_BUDGET" if "CONTEXT_BUDGET" in str(exc) else "INVALID_MODEL_OUTPUT"
                    result = Refusal(reason_codes=[code], explanation=str(exc))
                results.append(result)
                save_json(out / "input_manifest.json", {"source_id": source.id, "source_sha256": source.sha256,
                    "raw_transcript_reference": str((directory / "transcript.json").resolve()),
                    "raw_transcript_sha256": source.transcript_sha256, "task": task.model_dump(),
                    "model": backend.provenance.model, "run": run + 1, "prompt_version": VERSION})
                for name in ("candidate_index.json", "semantic_timeline.json"):
                    save_json(out / name, json.loads((root / name).read_text(encoding="utf-8")))
                save_json(out / "planner_input.json", context.model_dump(mode="json") if context else {"task": task.model_dump(), "candidate_index": str((root / "candidate_index.json").resolve())})
                save_json(out / "raw_model_response.json", planner.calls.records)
                save_json(out / "retrieval_audit.json", planner.retrieval_audit)
                save_json(out / ("refusal.json" if isinstance(result, Refusal) else "edit_plan.json"), result.model_dump(mode="json"))
                save_json(out / "story_graph.json", {"source_sha256": source.sha256,
                    "generated_by": planner.provenance.model_dump(), "created_at": datetime.now(timezone.utc).isoformat(),
                    "decision": decision.model_dump(mode="json") if decision else {"status": "NO_GRAPH"}})
                save_json(out / "validation.json", {"valid_plan": not isinstance(result, Refusal), "coverage": coverage,
                    "raw_evidence_reconstruction": context is not None, "human_coherence": "NOT_EVALUATED"})
                save_json(out / "metrics.json", metrics(result, source, planner.calls.records))
                public, key = blind_package(task.id + f"-{run}", source, baseline, result, task.id + str(run))
                save_json(out / "evaluation_package.json", public)
                save_json(args.output / "evaluator_private_keys" / f"{task.id}-{run}.json", key)
                print(task.id, run + 1, "REFUSAL" if isinstance(result, Refusal) else "VALID_PLAN", flush=True)
            save_json(root / task.id / "variance.json", variance(results))


if __name__ == "__main__":
    main()
