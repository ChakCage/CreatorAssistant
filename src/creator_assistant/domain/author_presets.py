from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


class AuthorMatchKind(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    ALIAS_MATCH = "ALIAS_MATCH"
    MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True)
class AuthorPreset:
    preset_id: str
    display_name: str
    root_path: str
    youtube_channel_ids: tuple[str, ...] = field(default_factory=tuple)
    youtube_handles: tuple[str, ...] = field(default_factory=tuple)
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuthorPreset":
        root = str(raw.get("root_path") or "").strip()
        key = root.casefold()
        return cls(
            preset_id=str(raw.get("preset_id") or "preset-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]),
            display_name=str(raw.get("display_name") or Path(root).parent.name or Path(root).name),
            root_path=root,
            youtube_channel_ids=_values(raw.get("youtube_channel_ids")),
            youtube_handles=_values(raw.get("youtube_handles")),
            aliases=_values(raw.get("aliases")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "display_name": self.display_name,
            "root_path": self.root_path,
            "youtube_channel_ids": list(self.youtube_channel_ids),
            "youtube_handles": list(self.youtube_handles),
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class AuthorResolution:
    kind: AuthorMatchKind
    matches: tuple[AuthorPreset, ...] = field(default_factory=tuple)
    matched_by: str = ""
    suggestion: AuthorPreset | None = None

    @property
    def preset(self) -> AuthorPreset | None:
        return self.matches[0] if len(self.matches) == 1 else None


def _values(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def merge_presets(configured: Iterable[dict[str, Any]], discovered: Iterable[tuple[str, Path]]) -> list[AuthorPreset]:
    """Merge persisted mappings with runtime-discovered author folders."""
    by_path: dict[str, AuthorPreset] = {}
    for raw in configured:
        if isinstance(raw, dict):
            preset = AuthorPreset.from_dict(raw)
            if preset.root_path:
                by_path[preset.root_path.casefold()] = preset
    for display_name, path in discovered:
        root = str(path)
        key = root.casefold()
        if key not in by_path:
            by_path[key] = AuthorPreset.from_dict({"display_name": display_name, "root_path": root})
    return sorted(by_path.values(), key=lambda item: item.display_name.casefold())
