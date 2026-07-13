from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from creator_assistant.domain.shorts.models import ShortsManifest, SourceInfo


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ShortsManifestStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def create(self, project_id: str, source: SourceInfo) -> ShortsManifest:
        now = utc_now()
        manifest = ShortsManifest(
            schema_version=SCHEMA_VERSION,
            shorts_project_id=project_id,
            source_path=source.path,
            source_fingerprint=source.fingerprint,
            source_size=source.size,
            source_mtime=source.mtime,
            source_duration=source.duration,
            created_at=now,
            updated_at=now,
        )
        self.save(manifest)
        return manifest

    def load(self) -> Optional[ShortsManifest]:
        if not self.path.is_file():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return ShortsManifest.from_dict(data)

    def save(self, manifest: ShortsManifest) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        manifest.updated_at = utc_now()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(self.path))
