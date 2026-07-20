from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from creator_assistant.infrastructure.settings_store import config_root
from creator_assistant.services.shorts.project_template import ProjectShortsTemplate


_BANNER_IDENTITY_KEYS = {
    "channel_banner_path", "banner_path", "subscribe_banner", "channel_profile_id",
    "profile_id", "source_author", "aliases",
}


@dataclass(frozen=True)
class GlobalShortsTemplate:
    template_id: str
    name: str
    composition: dict[str, Any]
    created_at: str
    updated_at: str

    def project_template(self) -> ProjectShortsTemplate:
        return ProjectShortsTemplate.from_dict(self.composition) or ProjectShortsTemplate()


class GlobalShortsTemplateLibrary:
    """Atomic user-level library; banner identity is always resolved from the source author."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config_root() / "shorts" / "global_templates.json")
        self._data = self._load()

    def templates(self) -> list[GlobalShortsTemplate]:
        values = []
        for raw in self._data.get("templates", []):
            if not isinstance(raw, dict):
                continue
            try:
                values.append(GlobalShortsTemplate(**raw))
            except TypeError:
                continue
        return values

    def get(self, template_id: str) -> GlobalShortsTemplate | None:
        return next((item for item in self.templates() if item.template_id == template_id), None)

    def create(self, name: str, template: ProjectShortsTemplate) -> GlobalShortsTemplate:
        clean_name = self._unique_name(name)
        now = datetime.now(timezone.utc).isoformat()
        value = GlobalShortsTemplate(
            template_id="template-" + uuid4().hex[:12], name=clean_name,
            composition=self._sanitize(template), created_at=now, updated_at=now,
        )
        self._data.setdefault("templates", []).append(asdict(value))
        self._save()
        return value

    def update(self, template_id: str, template: ProjectShortsTemplate) -> GlobalShortsTemplate:
        current = self.get(template_id)
        if current is None:
            raise KeyError(template_id)
        updated = GlobalShortsTemplate(
            current.template_id, current.name, self._sanitize(template), current.created_at,
            datetime.now(timezone.utc).isoformat(),
        )
        self._replace(updated)
        return updated

    def duplicate(self, template_id: str, name: str = "") -> GlobalShortsTemplate:
        current = self._required(template_id)
        return self.create(name or f"{current.name} — копия", current.project_template())

    def rename(self, template_id: str, name: str) -> GlobalShortsTemplate:
        current = self._required(template_id)
        updated = GlobalShortsTemplate(
            current.template_id, self._unique_name(name, exclude=template_id), current.composition,
            current.created_at, datetime.now(timezone.utc).isoformat(),
        )
        self._replace(updated)
        return updated

    def delete(self, template_id: str) -> bool:
        before = len(self._data.get("templates", []))
        self._data["templates"] = [item for item in self._data.get("templates", []) if item.get("template_id") != template_id]
        assignments = self._data.setdefault("assignments", {})
        for key in ("default", "youtube", "tiktok"):
            if assignments.get(key) == template_id:
                assignments[key] = ""
        changed = len(self._data["templates"]) != before
        if changed:
            self._save()
        return changed

    def assignment(self, scope: str) -> str:
        return str(self._data.get("assignments", {}).get(scope, ""))

    def assign(self, scope: str, template_id: str) -> None:
        if scope not in {"default", "youtube", "tiktok"}:
            raise ValueError(scope)
        if template_id and self.get(template_id) is None:
            raise KeyError(template_id)
        self._data.setdefault("assignments", {})[scope] = template_id
        self._save()

    def selected(self, scope: str = "default") -> GlobalShortsTemplate | None:
        return self.get(self.assignment(scope))

    def _required(self, template_id: str) -> GlobalShortsTemplate:
        value = self.get(template_id)
        if value is None:
            raise KeyError(template_id)
        return value

    def _replace(self, value: GlobalShortsTemplate) -> None:
        self._data["templates"] = [
            asdict(value) if item.get("template_id") == value.template_id else item
            for item in self._data.get("templates", [])
        ]
        self._save()

    def _unique_name(self, name: str, exclude: str = "") -> str:
        base = re.sub(r"\s+", " ", str(name or "Новый шаблон")).strip()[:100] or "Новый шаблон"
        used = {item.name.casefold() for item in self.templates() if item.template_id != exclude}
        if base.casefold() not in used:
            return base
        index = 2
        while f"{base} ({index})".casefold() in used:
            index += 1
        return f"{base} ({index})"

    @staticmethod
    def _sanitize(template: ProjectShortsTemplate) -> dict[str, Any]:
        raw = template.to_dict()
        branding = dict(raw.get("branding") or {})
        for key in _BANNER_IDENTITY_KEYS:
            branding.pop(key, None)
        raw["branding"] = branding
        return raw

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return {
            "schema_version": 1,
            "templates": list(raw.get("templates", [])) if isinstance(raw, dict) else [],
            "assignments": dict(raw.get("assignments", {})) if isinstance(raw, dict) else {},
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(self.path))
