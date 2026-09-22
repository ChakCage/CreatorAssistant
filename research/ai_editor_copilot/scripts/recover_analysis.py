"""Recover schema-valid block responses from an interrupted run without another LLM call.

Candidate references still undergo individual validation in HierarchicalPlanner.analyze.
Never turns a malformed response into a valid one, and never overwrites existing cache.
"""
import argparse
import json
from pathlib import Path
from ai_editor_copilot.feedback.events import save_json
from ai_editor_copilot.narrative.evidence import load_evidence
from ai_editor_copilot.narrative.chunks import make_chunks
from ai_editor_copilot.narrative.contracts import BlockResponse
from ai_editor_copilot.narrative.pipeline import EXTRACTION_VERSION, strict_json
from ai_editor_copilot.planner.service import stable_id


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--journal", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    args = p.parse_args()
    chunks = {}
    for directory in args.dataset.iterdir():
        if (directory / "input_manifest.json").exists():
            source = load_evidence(directory)
            for chunk in make_chunks(source):
                chunks["chunk:" + chunk["id"]] = (source, chunk)
    count = 0
    for path in sorted(args.journal.glob("*.json"), key=lambda f: f.stat().st_mtime_ns, reverse=True):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("stage") not in chunks or not record.get("response"):
            continue
        source, chunk = chunks[record["stage"]]
        # Verify the journal prompt actually carried the expected complete source chunk.
        if json.dumps(chunk, ensure_ascii=False) not in record.get("prompt", ""):
            continue
        try:
            value = BlockResponse.model_validate(strict_json(record["response"]))
        except ValueError:
            continue
        key = stable_id("analysis_", [EXTRACTION_VERSION, source.transcript_sha256, chunk, "qwen3.6:35b-a3b"])
        out = args.cache / (key + ".json")
        if out.exists():
            continue
        save_json(out, {"key": key, "value": value.model_dump(), "calls": [record],
            "recovered_from": str(path.resolve()), "recovery_note": "schema-valid only; each candidate must be validated separately"})
        count += 1
    print("Recovered schema-valid blocks:", count)


if __name__ == "__main__":
    main()
