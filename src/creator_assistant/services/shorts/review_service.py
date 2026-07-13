from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from creator_assistant.domain.shorts.errors import InvalidClipError
from creator_assistant.domain.shorts.models import Candidate
from creator_assistant.services.shorts.manifest import ShortsManifestStore
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectPaths


class CandidateReviewService:
    def __init__(self, paths: ShortsProjectPaths, source_duration: float) -> None:
        self.paths, self.source_duration = paths, source_duration
        self.manifest_store = ShortsManifestStore(paths.manifest)

    def save(self, candidates: list[Candidate]) -> None:
        manifest = self.manifest_store.load()
        if manifest is None:
            raise InvalidClipError("Manifest проекта Shorts не найден.")
        manifest.candidates = [asdict(item) for item in candidates]
        manifest.approved_clips = [asdict(item) for item in candidates if item.status == "approved"]
        self._atomic_json(self.paths.analysis / "candidates.json", manifest.candidates)
        approved_ids = set()
        for candidate in candidates:
            if candidate.status != "approved":
                continue
            approved_ids.add(candidate.id)
            self._atomic_json(self.paths.approved / f"{candidate.id}.json", asdict(candidate))
        for old in self.paths.approved.glob("short_*.json"):
            if old.stem not in approved_ids:
                old.unlink(missing_ok=True)
        self.manifest_store.save(manifest)

    def update_boundaries(self, candidate: Candidate, start: float, end: float) -> Candidate:
        if start < 0:
            raise InvalidClipError("Начало не может быть меньше 0.")
        if end > self.source_duration + 0.001:
            raise InvalidClipError("Конец выходит за длительность исходника.")
        if end <= start:
            raise InvalidClipError("Конец должен быть позже начала.")
        candidate.start, candidate.end = round(start, 3), round(end, 3)
        return candidate

    @staticmethod
    def _atomic_json(path: Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(path))
