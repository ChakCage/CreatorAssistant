from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

from creator_assistant.domain.automation.models import AutomationJob


def run_automation_cli(argv: list[str], engine, output: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="creator-assistant")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-job")
    group.add_argument("--resume-job")
    group.add_argument("--list-jobs", action="store_true")
    group.add_argument("--show-job")
    args = parser.parse_args(argv)
    if args.list_jobs:
        output(json.dumps([job.to_dict() for job in engine.store.list()], ensure_ascii=False, indent=2))
        return 0
    if args.show_job:
        job = engine.store.load(args.show_job)
        if not job:
            output(f"Задание не найдено: {args.show_job}")
            return 2
        output(json.dumps(job.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.resume_job:
        job = engine.resume(args.resume_job)
        output(json.dumps(job.to_dict(), ensure_ascii=False, indent=2))
        return 0 if job.status != "FAILED" else 1
    raw = json.loads(Path(args.run_job).read_text(encoding="utf-8"))
    if raw.get("job_id") and raw.get("created_at"):
        job = AutomationJob.from_dict(raw)
        engine.store.save(job)
    else:
        source_values = raw.get("sources", [])
        paths = [str(item.get("path", "")) if isinstance(item, dict) else str(item) for item in source_values]
        job = engine.create_job(
            paths, mode=raw.get("mode", "APPROVAL_REQUIRED"),
            analysis_settings=raw.get("analysis_settings", {}),
            selection_settings=raw.get("selection_settings", {}),
            composition_preset=raw.get("composition_preset", {}),
            schedule_settings=raw.get("schedule_settings", {}),
            platforms=raw.get("platforms", ["youtube"]),
        )
        for target, value in zip(job.sources, source_values):
            if isinstance(value, dict):
                for key in ("shorts_project_path", "channel_id", "source_author", "folder_author"):
                    setattr(target, key, str(value.get(key, "")))
        profile = raw.get("profile", {})
        if profile:
            for key, value in profile.items():
                if hasattr(job.profile, key):
                    setattr(job.profile, key, value)
        engine.store.save(job)
    result = engine.run(job)
    output(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status != "FAILED" else 1
