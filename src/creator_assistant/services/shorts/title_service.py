from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from creator_assistant.domain.shorts.models import Candidate, SourceInfo, Transcript
from creator_assistant.infrastructure.manifest_store import MANIFEST_NAME
from creator_assistant.services.shorts.shorts_project_store import ShortsProjectPaths


TECHNICAL_TITLES = {
    "видос", "видео", "video", "final", "output", "render", "готовое видео",
    "готовое", "готовый ролик", "ролик", "vid", "movie",
}


@dataclass(frozen=True)
class OriginalTitle:
    title: str
    source: str


class ShortTitleService:
    def resolve_original_title(self, source: SourceInfo, paths: ShortsProjectPaths | None = None) -> OriginalTitle:
        source_path = Path(source.path)
        if paths:
            manifest_title = self._creator_manifest_title(paths.root.parent)
            if self._is_good_title(manifest_title):
                return OriginalTitle(manifest_title, "YouTube metadata")
            shorts_title = self._shorts_manifest_title(paths)
            if self._is_good_title(shorts_title):
                return OriginalTitle(shorts_title, "Shorts manifest")
            folder_title = self._project_folder_title(paths.root.parent)
            if self._is_good_title(folder_title):
                return OriginalTitle(folder_title, "название папки проекта")
        for parent in source_path.parents:
            manifest_title = self._creator_manifest_title(parent)
            if self._is_good_title(manifest_title):
                return OriginalTitle(manifest_title, "YouTube metadata")
        folder_title = self._project_folder_title(source_path.parent)
        if self._is_good_title(folder_title):
            return OriginalTitle(folder_title, "название папки проекта")
        return OriginalTitle(source_path.stem, "имя файла")

    def validate_hook(self, value: str) -> bool:
        text = self._cleanup(value)
        if not text:
            return False
        if text.casefold() in TECHNICAL_TITLES:
            return False
        words = text.split()
        return 2 <= len(words) <= 7 and not text.endswith(".")

    def heuristic_hook(self, candidate: Candidate, transcript: Transcript | None, original_title: str) -> str:
        text = candidate.text or self._transcript_for_candidate(candidate, transcript)
        title = self._cleanup(text or original_title).upper()
        words = [item for item in re.split(r"\s+", title) if item]
        hook = " ".join(words[:7]).rstrip(".!?")
        if self.validate_hook(hook):
            return hook
        fallback = self._cleanup(original_title).upper()
        fallback_words = [item for item in re.split(r"\s+", fallback) if item and item.casefold() not in TECHNICAL_TITLES]
        hook = " ".join(fallback_words[:7]).rstrip(".!?")
        return hook if self.validate_hook(hook) else "ЛУЧШИЙ МОМЕНТ В ЭТОМ ШОРТЕ"

    def translate_title_fallback(self, original_title: str) -> str:
        # Safe deterministic fallback: keeps manual edits untouched and never uses a technical filename.
        return self._cleanup(original_title)

    def _shorts_manifest_title(self, paths: ShortsProjectPaths) -> str:
        try:
            raw = json.loads(paths.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        for key in ("original_video_title", "title", "source_title"):
            value = str(raw.get(key) or "").strip()
            if value:
                return value
        return ""

    def _creator_manifest_title(self, project_root: Path) -> str:
        manifest = project_root / MANIFEST_NAME
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(raw.get("title") or "").strip()

    def _project_folder_title(self, folder: Path) -> str:
        name = folder.name.strip()
        if name.casefold() == "shorts" and folder.parent != folder:
            name = folder.parent.name.strip()
        return name

    def _is_good_title(self, value: str) -> bool:
        cleaned = self._cleanup(value)
        return bool(cleaned) and cleaned.casefold() not in TECHNICAL_TITLES and len(cleaned) >= 3

    def _cleanup(self, value: str) -> str:
        return " ".join(str(value or "").strip().strip("\"'“”«»").split()).rstrip(".")

    def _transcript_for_candidate(self, candidate: Candidate, transcript: Transcript | None) -> str:
        if not transcript:
            return candidate.text
        parts = [
            segment.text.strip()
            for segment in transcript.segments
            if segment.end > candidate.start and segment.start < candidate.end and segment.text.strip()
        ]
        return " ".join(parts)
