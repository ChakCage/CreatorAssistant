import argparse
import json
import sys
from pathlib import Path
from ai_editor_copilot.domain.models import PlannerInput, UserCommand
from ai_editor_copilot.ingest.transcript import load_transcript
from ai_editor_copilot.style.profiles import profile
from ai_editor_copilot.planner.service import EditPlanner
from ai_editor_copilot.executors.base import DryRunExecutor
from ai_editor_copilot.feedback.events import save_json


def timecode(seconds):
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"


def summary(plan):
    lines = [f"Backend: {plan.generated_by.backend} / {plan.generated_by.model}", "Selected clips:"]
    for c in plan.sequence:
        lines.append(f"{timecode(c.source_range.start)}–{timecode(c.source_range.end)} — {c.narrative_role}: {c.reason}")
    duration = sum(c.source_range.end - c.source_range.start for c in plan.sequence)
    lines += [f"Total duration: {duration:.3f} sec", "Warnings:", *plan.warnings,
              "Unresolved:", *plan.unresolved_requirements]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Research planner: transcript → validated JSON → dry run")
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--minimum-duration", type=float, default=0)
    parser.add_argument("--style", default="narrative_short")
    parser.add_argument("--backend", choices=["mock", "local"], default="mock")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    planner = None
    try:
        if args.output.exists() and any(args.output.iterdir()):
            raise ValueError("output directory must be empty; use a new run directory to preserve evidence")
        context = PlannerInput(timeline=load_transcript(args.transcript), style=profile(args.style),
            command=UserCommand(text=args.prompt), target_duration=args.duration, minimum_duration=args.minimum_duration)
        if args.backend == "local":
            from ai_editor_copilot.adapters.local_creator_assistant import LocalCreatorAssistantLLMBackend
            backend = LocalCreatorAssistantLLMBackend()
            print("Preparing local qwen3.6:35b-a3b…", flush=True)
            backend.prepare()
        else:
            from ai_editor_copilot.adapters.mock import MockPlannerBackend
            backend = MockPlannerBackend()
        planner = EditPlanner(backend)
        plan = planner.plan(context)
        report = DryRunExecutor().execute(plan, context)
        save_json(args.output / "edit_plan.json", plan.model_dump(mode="json"))
        save_json(args.output / "dry_run.json", report)
        save_json(args.output / "validation.json", {"valid": True, "attempts": planner.attempts})
        save_json(args.output / "input.json", context.model_dump(mode="json"))
        text = summary(plan)
        (args.output / "summary.txt").write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        if planner is not None:
            save_json(args.output / "validation.json", {"valid": False, "attempts": planner.attempts, "error": str(exc)})
        print(f"Planning failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
