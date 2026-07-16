from __future__ import annotations

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creator_assistant.app import ServiceContainer
from creator_assistant.domain.automation.models import AutomationMode
from creator_assistant.infrastructure.automation_job_store import AutomationJobStore
from creator_assistant.services.automation.engine import AutomationEngine
from creator_assistant.services.automation.shorts_pipeline import ExistingShortsAutomationPipeline


SOURCE = Path(r"E:\YouTube\Beppo\Завершённые\I Mined 48,235 Obsidian - Hardcore\Видос.mp4")
PROJECT = SOURCE.parent / "Shorts"
OUTPUT = ROOT / "_test_output" / "autopilot_real"


def run_mode(engine: AutomationEngine, mode: str) -> dict:
    job = engine.create_job(
        [str(SOURCE)], mode=mode,
        selection_settings={"minimum_score": 80, "maximum_per_source": 3},
        schedule_settings={
            "start_date": "2026-07-20", "timezone": "Europe/Moscow",
            "publications_per_day": 2, "preferred_time_slots": ["13:00", "19:00"],
        }, platforms=["youtube", "tiktok"],
    )
    job.sources[0].shorts_project_path = str(PROJECT)
    job.sources[0].source_author = "Beppo"
    engine.store.save(job)
    started = time.monotonic()
    result = engine.run(job)
    elapsed = time.monotonic() - started
    before_approval = result.status
    if mode == AutomationMode.APPROVAL_REQUIRED.value and result.status == "WAITING_FOR_APPROVAL":
        result = engine.approve_and_schedule(result.job_id)
    return {
        "job_id": result.job_id, "mode": mode, "status_before_approval": before_approval,
        "final_status": result.status, "elapsed_seconds": round(elapsed, 3),
        "candidates_found": sum(item.candidates_found for item in result.sources),
        "selected": len(result.shorts), "rendered": result.result.rendered_count,
        "needs_review": result.result.needs_review_count,
        "profile_ids": [item.profile_id for item in result.shorts],
        "subtitle_sizes": [item.subtitle_settings.get("size") for item in result.shorts],
        "titles": [item.title for item in result.shorts],
        "artifacts": [item.artifact.output_path if item.artifact else "" for item in result.shorts],
        "validated": [bool(item.artifact and item.artifact.validated) for item in result.shorts],
        "issues": [[issue.code for issue in item.issues] for item in result.shorts],
        "publishing_plan": result.result.publishing_plan.__dict__ if result.result.publishing_plan else None,
        "summary": result.result.summary,
    }


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    container = ServiceContainer()
    engine = AutomationEngine(ExistingShortsAutomationPipeline(container), AutomationJobStore(OUTPUT / "jobs"))
    report = [
        run_mode(engine, AutomationMode.APPROVAL_REQUIRED.value),
        run_mode(engine, AutomationMode.FULL_AUTOPILOT.value),
    ]
    path = OUTPUT / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=lambda value: value.__dict__), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, default=lambda value: value.__dict__))
    return 0 if all(item["final_status"] == "SCHEDULED" and item["rendered"] >= 1 for item in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
