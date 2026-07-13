from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from creator_assistant.app import ServiceContainer
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import VideoMetadata
from creator_assistant.infrastructure.manifest_store import MANIFEST_NAME, ManifestLoader


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    manifest_path = project / MANIFEST_NAME
    before_files = {
        str(path.relative_to(project)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in project.rglob("*")
        if path.is_file() and path.name not in {MANIFEST_NAME, MANIFEST_NAME + ".bak"}
    }
    container = ServiceContainer()
    metadata = VideoMetadata(args.video_id, args.title, None, args.url)
    events = []
    result = container.projects.migrate_existing(
        metadata,
        project,
        job_id="migration-" + uuid.uuid4().hex,
        author_preset=args.author,
        cancellation=CancellationToken(),
        on_progress=events.append,
    )
    after_files = {
        str(path.relative_to(project)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in project.rglob("*")
        if path.is_file() and path.name not in {MANIFEST_NAME, MANIFEST_NAME + ".bak"}
    }
    loaded = ManifestLoader().load(manifest_path)
    summary = {
        "manifest_status": loaded.status.value,
        "manifest_path": str(manifest_path),
        "backup_path": str(manifest_path.with_name(manifest_path.name + ".bak")),
        "backup_exists": manifest_path.with_name(manifest_path.name + ".bak").is_file(),
        "user_files_unchanged": before_files == after_files,
        "discovered_files": len(result["discovered_files"]),
        "steps": [event.message for event in events],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if loaded.status.value == "VALID" and before_files == after_files else 1


if __name__ == "__main__":
    raise SystemExit(main())
