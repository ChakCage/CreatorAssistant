from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from creator_assistant.domain.shorts.models import SourceInfo
from creator_assistant.services.shorts.manifest import ShortsManifestStore


@dataclass(frozen=True)
class ShortsProjectPaths:
    root: Path
    analysis: Path
    cache: Path
    thumbnails: Path
    approved: Path
    subtitles: Path
    renders: Path
    reports: Path
    render_state: Path
    manifest: Path


class ShortsProjectStore:
    @staticmethod
    def suggested_root(source: Path) -> Path:
        """Choose a predictable Shorts root without leaking into an author root.

        A Creator Assistant project is identified by its private metadata marker.
        Standalone media receives a sibling ``<stem> Shorts`` directory.
        """
        source = source.resolve()
        for parent in source.parents:
            if (parent / ".creator-assistant" / "manifest.json").is_file():
                return parent / "Shorts"
        return source.parent / f"{source.stem} Shorts"

    DIRECTORY_NAMES = ("Analysis", "Cache", "Approved", "Subtitles", "Renders", "Reports")

    @staticmethod
    def paths(root: Path) -> ShortsProjectPaths:
        return ShortsProjectPaths(
            root=root,
            analysis=root / "Analysis",
            cache=root / "Cache",
            thumbnails=root / "Cache" / "thumbnails",
            approved=root / "Approved",
            subtitles=root / "Subtitles",
            renders=root / "Renders",
            reports=root / "Reports",
            render_state=root / ".creator-assistant" / "render-state",
            manifest=root / "shorts_manifest.json",
        )

    def create(self, root: Path, source: SourceInfo) -> ShortsProjectPaths:
        paths = self.paths(root)
        for folder in (paths.analysis, paths.cache, paths.thumbnails, paths.approved, paths.subtitles, paths.renders, paths.reports, paths.render_state):
            folder.mkdir(parents=True, exist_ok=True)
        project_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(source.name).stem).strip("-")[:40]
        project_id = f"{project_id or 'shorts'}-{uuid.uuid4().hex[:8]}"
        ShortsManifestStore(paths.manifest).create(project_id, source)
        return paths

    def open_or_create(self, root: Path, source: SourceInfo) -> ShortsProjectPaths:
        paths = self.paths(root)
        if paths.manifest.is_file():
            manifest = ShortsManifestStore(paths.manifest).load()
            if manifest and manifest.source_fingerprint == source.fingerprint:
                from creator_assistant.services.shorts.render_state import RenderStateStore
                RenderStateStore(paths.root).migrate_legacy()
                return paths
        return self.create(root, source)
