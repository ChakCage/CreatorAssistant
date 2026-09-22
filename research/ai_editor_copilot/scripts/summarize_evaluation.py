"""Export ONLY non-text research measurements; never publish transcript/model responses."""
import argparse
import json
from pathlib import Path
from ai_editor_copilot.feedback.events import save_json


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--evaluation", type=Path, action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    sources = {}
    for path in args.dataset.glob("*/input_manifest.json"):
        m = read(path)
        sources[path.parent.name] = {k: m[k] for k in ("duration", "source_sha256", "transcript_sha256", "language", "backend", "model", "ingested_at")}
    cases, coverage, baselines = [], [], []
    for root in args.evaluation:
        for path in sorted(root.glob("*/*/baseline_metrics.json")):
            if not (path.parent / "variance.json").exists():
                continue  # interrupted task groups are not final benchmark evidence
            baselines.append({"evaluation": root.name, "source_id": path.parents[1].name,
                "task_id": path.parent.name, "metrics": read(path)})
        for path in root.glob("*/coverage.json"):
            c = read(path)
            coverage.append({"evaluation": root.name, "source_id": path.parent.name,
                **{k: c[k] for k in ("chunks_produced", "chunks_analyzed", "transcript_segments", "segments_analyzed",
                    "segment_coverage_percent", "source_time_coverage_percent", "full_transcript_analyzed",
                    "candidates_before_dedup", "candidates_per_source_third")},
                "failed_chunks": len(c["failures"]), "duplicates_removed": len(c["duplicates_removed"]),
                "zero_duration_segments": len(c["zero_duration_raw_segments"]),
                "empty_segments": len(c["empty_raw_segments"]), "word_alignment_anomalies": len(c["word_alignment_anomalies"])})
        for path in sorted(root.glob("*/*/run_*/metrics.json")):
            if not (path.parents[1] / "variance.json").exists():
                continue
            m = read(path)
            plan_path = path.parent / "edit_plan.json"
            plan = read(plan_path) if plan_path.exists() else None
            graph = read(path.parent / "story_graph.json")
            decision = graph.get("decision", graph)
            refusal_origin = ("model_claim_not_ground_truth" if decision.get("status") == "NO_COHERENT_STORY"
                else "pipeline_validation_or_coverage") if m["refusal"] else None
            cases.append({"evaluation": root.name, "source_id": path.parents[2].name,
                "task_id": path.parents[1].name, "run": path.parent.name, "metrics": m,
                "refusal_origin": refusal_origin,
                "selected_source_ranges": [c["source_range"] for c in plan["sequence"]] if plan else [],
                "human_evaluation": "NOT_EVALUATED"})
    variances = [{"evaluation": root.name, "source_id": p.parents[1].name, "task_id": p.parent.name,
        **read(p)} for root in args.evaluation for p in sorted(root.glob("*/*/variance.json"))]
    save_json(args.output, {"sources": sources, "coverage": coverage, "runs": cases,
        "variance": variances, "baselines": baselines,
        "total_runs": len(cases), "valid_plans": sum(not c["metrics"]["refusal"] for c in cases),
        "human_evaluation": "NOT_EVALUATED", "recommendation": "NOT_READY_FOR_RESOLVE_EXECUTOR"})
    print("Summary:", len(cases), "runs;", sum(not c["metrics"]["refusal"] for c in cases), "valid plans")


if __name__ == "__main__":
    main()
