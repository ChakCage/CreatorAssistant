from __future__ import annotations

import argparse
import json
from pathlib import Path

from creator_assistant.app import ServiceContainer
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import ProjectOptions, VideoFormat, VideoMetadata
from creator_assistant.infrastructure.job_store import JobStore
from creator_assistant.infrastructure.manifest_store import MANIFEST_NAME, ManifestLoader


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("--cycles", type=int, default=5)
    args = parser.parse_args()
    project = args.project.resolve()
    if not project.is_dir():
        raise SystemExit(f"Test project does not exist: {project}")
    initial_files = {
        str(path.relative_to(project)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in project.rglob("*")
        if path.is_file() and path.name not in {MANIFEST_NAME, MANIFEST_NAME + ".bak"}
    }
    container = ServiceContainer()
    service = container.projects
    service.job_store = JobStore(project.parent / "legacy_project_test_state")
    metadata = VideoMetadata(
        "5nTuu0FzAUg",
        "100 Players Simulate Minecraft's Magical Purge",
        7040.0,
        "https://youtu.be/5nTuu0FzAUg",
        formats=[
            VideoFormat("308", "webm", height=1440, fps=60, vcodec="vp9"),
            VideoFormat("298", "mp4", height=720, fps=60, vcodec="avc1"),
            VideoFormat("251", "webm", acodec="opus"),
            VideoFormat("140", "m4a", acodec="mp4a.40.2"),
        ],
    )
    options = ProjectOptions(
        download_maximum=False,
        create_proxy=False,
        download_audio=False,
        create_instrumental=False,
        create_reaper_project=False,
    )
    cycle_results = []
    for index in range(args.cycles):
        migration_events = []
        service.migrate_existing(
            metadata,
            project,
            job_id=f"legacy-check-{index + 1}",
            author_preset="MylesMC",
            cancellation=CancellationToken(),
            on_progress=migration_events.append,
        )
        result = service.execute(
            project.parent,
            metadata,
            options,
            CancellationToken(),
            lambda _event: None,
            project,
        )
        loaded = ManifestLoader().load(project / MANIFEST_NAME)
        cycle_results.append({
            "cycle": index + 1,
            "project_path": str(result.project_path),
            "manifest_status": loaded.status.value,
            "migration_steps": [event.message for event in migration_events],
        })
    final_files = {
        str(path.relative_to(project)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in project.rglob("*")
        if path.is_file() and path.name not in {MANIFEST_NAME, MANIFEST_NAME + ".bak"}
    }
    preserved = all(final_files.get(name) == value for name, value in initial_files.items())
    summary = {
        "cycles": cycle_results,
        "original_files_preserved": preserved,
        "initial_file_count": len(initial_files),
        "final_file_count": len(final_files),
        "backup": str(project / (MANIFEST_NAME + ".bak")),
        "backup_exists": (project / (MANIFEST_NAME + ".bak")).is_file(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if preserved and all(item["manifest_status"] == "VALID" for item in cycle_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
