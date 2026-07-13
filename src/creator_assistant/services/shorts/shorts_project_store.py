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
    manifest: Path


class ShortsProjectStore:
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
            manifest=root / "shorts_manifest.json",
        )

    def create(self, root: Path, source: SourceInfo) -> ShortsProjectPaths:
        paths = self.paths(root)
        for folder in (paths.analysis, paths.cache, paths.thumbnails, paths.approved, paths.subtitles, paths.renders, paths.reports):
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
                return paths
        return self.create(root, source)
