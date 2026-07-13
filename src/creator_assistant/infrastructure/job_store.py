from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .settings_store import local_data_root


class JobStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or (local_data_root() / "jobs")

    def _path(self, video_id: str) -> Path:
        safe_id = "".join(char for char in video_id if char.isalnum() or char in "_-") or "unknown"
        return self.root / f"{safe_id}.json"

    def load(self, video_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(video_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def save(self, video_id: str, data: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(video_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(path))

    def resumable_path(self, video_id: str) -> Optional[Path]:
        data = self.load(video_id)
        if not data or data.get("status") == "completed":
            return None
        project = Path(str(data.get("project_path", "")))
        marker = data.get("created_by") == "CreatorAssistant"
        if marker and project.is_dir() and (project / "Материалы").is_dir():
            return project
        return None

    def project_path(self, video_id: str) -> Optional[Path]:
        """Return any recorded owned project, including completed/cancelled jobs."""
        data = self.load(video_id)
        if not data or data.get("created_by") != "CreatorAssistant":
            return None
        project = Path(str(data.get("project_path", "")))
        return project if project.is_dir() else None
