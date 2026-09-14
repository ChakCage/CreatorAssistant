"""Remove all synthetic semantic labels for a fairer local-model smoke input."""
import json
from pathlib import Path

folder = Path(__file__).resolve().parents[1] / "examples/narrative_short"
annotated = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
raw = {"source_id": "vlog", "source_uri": "fixture://hotel-vlog", "duration": 3600,
       "segments": [{"id": f"seg_{i:03d}", "start": s["range"]["start"], "end": s["range"]["end"],
                     "text": s["transcript"]} for i, s in enumerate(annotated["segments"])]}
(folder / "raw_transcript.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
