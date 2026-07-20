from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from creator_assistant.services.shorts.project_template import ProjectShortsTemplate


class PlatformTemplateResolver:
    """Derive platform-specific composition without silently changing the project template."""

    def resolve(self, project: ProjectShortsTemplate, platform: str, override: dict[str, Any] | None = None) -> ProjectShortsTemplate:
        raw = copy.deepcopy(project.to_dict())
        if platform == "tiktok":
            branding = raw.setdefault("branding", {})
            if branding.get("show_channel_card"):
                branding["show_channel_card"] = False
                branding["channel_banner_path"] = ""
                branding["channel_profile_id"] = ""
        self._merge(raw, override or {})
        return ProjectShortsTemplate.from_dict(raw) or project

    def artifact_key(self, template: ProjectShortsTemplate) -> str:
        return hashlib.sha256(json.dumps(template.to_dict(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def needs_separate_render(self, youtube: ProjectShortsTemplate, tiktok: ProjectShortsTemplate) -> bool:
        return self.artifact_key(youtube) != self.artifact_key(tiktok)

    def _merge(self, target: dict[str, Any], override: dict[str, Any]) -> None:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict): self._merge(target[key], value)
            else: target[key] = copy.deepcopy(value)
