from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from creator_assistant.domain.shorts.models import ShortsManifest


def settings_fingerprint(settings: Dict[str, Any]) -> str:
    payload = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ShortsCache:
    def __init__(self, manifest: ShortsManifest) -> None:
        self.manifest = manifest

    def source_matches(self, fingerprint: str) -> bool:
        return bool(fingerprint) and fingerprint == self.manifest.source_fingerprint

    def stage_valid(self, stage: str, artifact: Path, settings: Dict[str, Any] | None = None) -> bool:
        if stage not in self.manifest.completed_stages or not artifact.is_file() or artifact.stat().st_size <= 0:
            return False
        if settings is None:
            return True
        stored = (self.manifest.analysis_settings.get("stage_fingerprints") or {}).get(stage)
        return stored == settings_fingerprint(settings)

    def mark_complete(self, stage: str, settings: Dict[str, Any] | None = None) -> None:
        if stage not in self.manifest.completed_stages:
            self.manifest.completed_stages.append(stage)
        if settings is not None:
            stages = self.manifest.analysis_settings.setdefault("stage_fingerprints", {})
            stages[stage] = settings_fingerprint(settings)
