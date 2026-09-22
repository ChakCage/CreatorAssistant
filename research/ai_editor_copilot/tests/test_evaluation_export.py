"""The public summary must not count interrupted tasks or export transcript text."""
import json
import runpy
import sys
from pathlib import Path


def test_summary_excludes_unfinished_tasks_and_raw_text(tmp_path, monkeypatch):
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    root = tmp_path / "evaluation"
    for task in ("complete", "interrupted"):
        group = root / "source" / task
        write(group / "baseline_metrics.json", {"refusal": False})
        write(group / "run_01" / "metrics.json", {"refusal": True})
        write(group / "run_01" / "story_graph.json",
              {"decision": {"status": "NO_COHERENT_STORY"}})
        write(group / "run_01" / "raw_model_response.json", {"text": "PRIVATE TRANSCRIPT"})
    write(root / "source" / "complete" / "variance.json", {"runs": 1})
    output = tmp_path / "summary.json"
    monkeypatch.setattr(sys, "argv", ["summary", "--dataset", str(dataset),
        "--evaluation", str(root), "--output", str(output)])
    script = Path(__file__).resolve().parents[1] / "scripts" / "summarize_evaluation.py"
    runpy.run_path(str(script), run_name="__main__")
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["total_runs"] == 1
    assert data["runs"][0]["task_id"] == "complete"
    assert len(data["baselines"]) == 1
    assert "PRIVATE TRANSCRIPT" not in output.read_text(encoding="utf-8")
