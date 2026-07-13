from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from creator_assistant.infrastructure.manifest_store import MANIFEST_NAME, ManifestLoader
from creator_assistant.infrastructure.settings_store import local_data_root


@dataclass(frozen=True)
class ProjectRoot:
    path: Path
    preset_id: str = ""
    display_name: str = ""


class ProjectIndex:
    """Disposable, atomic cache of project identities under configured roots."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (local_data_root() / "project_index.json")

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return self._empty()
        if not isinstance(raw, dict) or not isinstance(raw.get("projects"), list):
            return self._empty()
        return raw

    def lookup(self, video_id: str) -> list[dict[str, Any]]:
        data = self.load()
        changed = False
        found = []
        for raw in data.get("projects", []):
            if not isinstance(raw, dict) or str(raw.get("video_id") or "") != video_id:
                continue
            item = dict(raw)
            stale = not Path(str(item.get("project_path") or "")).is_dir()
            if bool(item.get("stale")) != stale:
                item["stale"] = stale
                raw["stale"] = stale
                changed = True
            found.append(item)
        if changed:
            self._write(data)
        return found

    def rescan(self, roots: Iterable[ProjectRoot]) -> dict[str, Any]:
        previous = self.load()
        old_by_path = {
            str(item.get("project_path") or "").casefold(): dict(item)
            for item in previous.get("projects", []) if isinstance(item, dict)
        }
        projects: list[dict[str, Any]] = []
        warnings: list[str] = []
        scanned_roots: list[str] = []
        loader = ManifestLoader()
        configured_roots = self._unique_roots(roots)
        configured_keys = {str(root.path).casefold() for root in configured_roots}
        for root in configured_roots:
            if not root.path.is_dir():
                warnings.append(f"Недоступна папка проектов: {root.path}")
                continue
            scanned_roots.append(str(root.path))
            try:
                children = list(root.path.iterdir())
            except OSError as exc:
                warnings.append(f"Не удалось прочитать {root.path}: {exc}")
                continue
            for child in children:
                if not child.is_dir():
                    continue
                manifest_path = child / MANIFEST_NAME
                if not manifest_path.is_file():
                    continue
                loaded = loader.load(manifest_path)
                raw = loaded.raw if isinstance(loaded.raw, dict) else {}
                manifest = loaded.manifest
                video_id = manifest.video_id if manifest else str(raw.get("video_id") or "")
                if not video_id:
                    continue
                projects.append({
                    "video_id": video_id,
                    "title": manifest.title if manifest else str(raw.get("title") or child.name),
                    "project_path": str(child),
                    "root_path": str(root.path),
                    "preset_id": root.preset_id,
                    "display_name": root.display_name,
                    "manifest_status": loaded.status.value,
                    "stale": False,
                })
        current_paths = {str(item["project_path"]).casefold() for item in projects}
        for key, item in old_by_path.items():
            if key in current_paths:
                continue
            root_key = str(item.get("root_path") or "").casefold()
            if root_key in configured_keys:
                item["stale"] = True
                projects.append(item)
        data = {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "projects": projects,
            "warnings": warnings,
            "scanned_roots": scanned_roots,
        }
        self._write(data)
        return data

    def record(self, *, video_id: str, title: str, project_path: Path, root_path: Path, preset_id: str = "", display_name: str = "") -> None:
        if not video_id or not project_path.is_dir():
            return
        data = self.load()
        key = str(project_path).casefold()
        projects = [
            dict(item) for item in data.get("projects", [])
            if isinstance(item, dict) and str(item.get("project_path") or "").casefold() != key
        ]
        projects.append({
            "video_id": video_id,
            "title": title,
            "project_path": str(project_path),
            "root_path": str(root_path),
            "preset_id": preset_id,
            "display_name": display_name,
            "manifest_status": "VALID",
            "stale": False,
        })
        data.update({
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "projects": projects,
        })
        self._write(data)

    @staticmethod
    def _unique_roots(roots: Iterable[ProjectRoot]) -> list[ProjectRoot]:
        unique: dict[str, ProjectRoot] = {}
        for root in roots:
            key = str(root.path).casefold()
            if key and key not in unique:
                unique[key] = root
        return list(unique.values())

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(self.path))

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": self.SCHEMA_VERSION, "updated_at": "", "projects": [], "warnings": [], "scanned_roots": []}
