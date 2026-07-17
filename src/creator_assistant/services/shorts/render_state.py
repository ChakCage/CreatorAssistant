from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class RenderStateStore:
    """Private, atomic render metadata kept away from user-facing exports."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)
        self.root = self.project_root / ".creator-assistant" / "render-state"
        self.renders = self.project_root / "Renders"

    @staticmethod
    def _safe_key(render_key: str) -> str:
        value = "".join(char for char in str(render_key) if char.isalnum() or char in "-_")
        return value or hashlib.sha256(str(render_key).encode("utf-8")).hexdigest()

    def path_for(self, render_key: str) -> Path:
        return self.root / f"{self._safe_key(render_key)}.json"

    def save(self, render_key: str, metadata: dict[str, Any]) -> Path:
        if not str(render_key).strip():
            raise ValueError("render_key is required")
        path = self.path_for(render_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, **dict(metadata), "render_key": str(render_key)}
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(path))
        return path

    def load(self, render_key: str) -> dict[str, Any]:
        path = self.path_for(render_key)
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def migrate_legacy(self) -> int:
        """Move valid legacy sidecars, deleting them only after atomic persistence."""
        if not self.renders.is_dir():
            return 0
        legacy = sorted({*self.renders.glob("*.render"), *self.renders.glob("*.render.json")})
        migrated = 0
        for sidecar in legacy:
            try:
                raw = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(raw, dict):
                continue
            render_key = str(raw.get("render_key") or "").strip()
            if not render_key:
                render_key = hashlib.sha256(str(sidecar.resolve()).casefold().encode("utf-8")).hexdigest()
            base = sidecar.with_suffix("")
            if base.suffix.casefold() == ".render":
                base = base.with_suffix("")
            output = base.with_suffix(".mp4")
            payload = dict(raw)
            payload.setdefault("output_path", str(output))
            payload.setdefault("migrated_from", sidecar.name)
            self.save(render_key, payload)
            sidecar.unlink()
            migrated += 1
        return migrated

    def delete_job(self, job_id: str) -> int:
        """Delete only private state owned by an AutomationJob, never rendered media."""
        if not self.root.is_dir():
            return 0
        deleted = 0
        for path in self.root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(payload, dict) and str(payload.get("job_id") or "") == str(job_id):
                path.unlink()
                deleted += 1
        return deleted
