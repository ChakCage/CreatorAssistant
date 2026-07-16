from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from creator_assistant.infrastructure.settings_store import config_root


@dataclass(frozen=True)
class ChannelProfile:
    id: str
    display_name: str
    source_author: str
    aliases: tuple[str, ...]
    subscribe_banner: str
    enabled: bool = True
    handle: str = ""
    channel_ids: tuple[str, ...] = ()
    default_cta: str = "Подписаться"
    default_banner_scale: int = 100
    default_banner_position: dict[str, int] | None = None
    default_banner_offset_x: int = 0
    default_banner_offset_y: int = 0
    default_banner_opacity: int = 100
    default_banner_anchor: str = "bottom_center"
    default_banner_fit_mode: str = "contain"

    @property
    def searchable(self) -> set[str]:
        values = {self.id, self.display_name, self.source_author, self.handle, *self.aliases, *self.channel_ids}
        return {item.casefold() for item in values if item}


class ChannelAssetStore:
    """Permanent user-managed channel assets outside dist and outside the repo."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (config_root() / "user_assets" / "channels")

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def profiles(self) -> list[ChannelProfile]:
        self.ensure_root()
        result: list[ChannelProfile] = []
        for profile_file in sorted(self.root.glob("*/profile.json")):
            try:
                raw = json.loads(profile_file.read_text(encoding="utf-8-sig"))
                result.append(self._profile_from_dict(raw))
            except (OSError, ValueError, TypeError):
                continue
        return result

    def get(self, profile_id: str) -> ChannelProfile | None:
        key = str(profile_id or "").casefold()
        return next((profile for profile in self.profiles() if profile.id.casefold() == key), None)

    def banner_path(self, profile: ChannelProfile | str | None) -> Path | None:
        if isinstance(profile, str):
            profile = self.get(profile)
        if not profile or not profile.enabled or not profile.subscribe_banner:
            return None
        path = self.root / profile.id / profile.subscribe_banner
        return path if path.is_file() else None

    def resolve(
        self,
        *,
        channel_id: str = "",
        saved_profile_id: str = "",
        source_author: str = "",
        aliases: list[str] | None = None,
    ) -> ChannelProfile | None:
        profiles = [item for item in self.profiles() if item.enabled]
        if saved_profile_id:
            saved = next((item for item in profiles if item.id.casefold() == saved_profile_id.casefold()), None)
            if saved and self.banner_path(saved):
                return saved
        if channel_id:
            key = channel_id.casefold()
            exact = next((item for item in profiles if key in {cid.casefold() for cid in item.channel_ids}), None)
            if exact and self.banner_path(exact):
                return exact
        if source_author:
            key = source_author.casefold()
            exact_author = next((item for item in profiles if item.source_author.casefold() == key), None)
            if exact_author and self.banner_path(exact_author):
                return exact_author
        for value in aliases or []:
            key = str(value).casefold()
            match = next((item for item in profiles if key in item.searchable), None)
            if match and self.banner_path(match):
                return match
        if source_author:
            key = source_author.casefold()
            match = next((item for item in profiles if key in item.searchable), None)
            if match and self.banner_path(match):
                return match
        return None

    def import_banner(self, profile_id: str, source: Path, metadata: dict[str, Any] | None = None) -> ChannelProfile:
        if not source.is_file():
            raise FileNotFoundError(source)
        safe_id = _safe_id(profile_id)
        profile_dir = self.ensure_root() / safe_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        extension = source.suffix.lower() if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
        filename = f"{safe_id}_subscribe{extension}"
        shutil.copy2(source, profile_dir / filename)
        raw = {
            "id": safe_id,
            "display_name": metadata.get("display_name", safe_id) if metadata else safe_id,
            "handle": metadata.get("handle", "") if metadata else "",
            "source_author": metadata.get("source_author", safe_id) if metadata else safe_id,
            "aliases": metadata.get("aliases", []) if metadata else [],
            "subscribe_banner": filename,
            "enabled": True,
            "default_cta": "Подписаться",
            "default_banner_scale": 100,
            "default_banner_position": {"x": 50, "y": 1600},
        }
        self._write_profile(profile_dir / "profile.json", raw)
        return self._profile_from_dict(raw)

    def save_profile(self, profile: ChannelProfile) -> None:
        profile_dir = self.ensure_root() / profile.id
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._write_profile(profile_dir / "profile.json", {
            "id": profile.id,
            "display_name": profile.display_name,
            "handle": profile.handle,
            "source_author": profile.source_author,
            "aliases": list(profile.aliases),
            "channel_ids": list(profile.channel_ids),
            "subscribe_banner": profile.subscribe_banner,
            "enabled": profile.enabled,
            "default_cta": profile.default_cta,
            "default_banner_scale": profile.default_banner_scale,
            "default_banner_offset_x": profile.default_banner_offset_x,
            "default_banner_offset_y": profile.default_banner_offset_y,
            "default_banner_opacity": profile.default_banner_opacity,
            "default_banner_anchor": profile.default_banner_anchor,
            "default_banner_fit_mode": profile.default_banner_fit_mode,
            "default_banner_position": profile.default_banner_position or {"x": 50, "y": 1600},
        })

    def save_banner_defaults(self, profile_id: str, settings: dict[str, Any]) -> None:
        profile = self.get(profile_id)
        if not profile:
            return
        raw = json.loads((self.root / profile.id / "profile.json").read_text(encoding="utf-8-sig"))
        raw.update({
            "default_banner_scale": int(settings.get("banner_scale", 100) or 100),
            "default_banner_offset_x": int(settings.get("banner_offset_x", 0) or 0),
            "default_banner_offset_y": int(settings.get("banner_offset_y", 0) or 0),
            "default_banner_opacity": int(settings.get("banner_opacity", 100) or 100),
            "default_banner_anchor": str(settings.get("banner_anchor", "bottom_center") or "bottom_center"),
            "default_banner_fit_mode": str(settings.get("banner_fit_mode", "contain") or "contain"),
        })
        self._write_profile(self.root / profile.id / "profile.json", raw)

    @staticmethod
    def _write_profile(path: Path, raw: dict[str, Any]) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))

    @staticmethod
    def _profile_from_dict(raw: dict[str, Any]) -> ChannelProfile:
        aliases = tuple(str(item) for item in raw.get("aliases", []) if str(item).strip())
        channel_ids = tuple(str(item) for item in raw.get("channel_ids", []) if str(item).strip())
        return ChannelProfile(
            id=str(raw.get("id") or ""),
            display_name=str(raw.get("display_name") or raw.get("id") or ""),
            handle=str(raw.get("handle") or ""),
            source_author=str(raw.get("source_author") or ""),
            aliases=aliases,
            channel_ids=channel_ids,
            subscribe_banner=str(raw.get("subscribe_banner") or raw.get("subscribe_banner_path") or ""),
            enabled=bool(raw.get("enabled", True)),
            default_cta=str(raw.get("default_cta") or "Подписаться"),
            default_banner_scale=int(raw.get("default_banner_scale", 100) or 100),
            default_banner_offset_x=int(raw.get("default_banner_offset_x", 0) or 0),
            default_banner_offset_y=int(raw.get("default_banner_offset_y", 0) or 0),
            default_banner_opacity=int(raw.get("default_banner_opacity", 100) or 100),
            default_banner_anchor=str(raw.get("default_banner_anchor") or "bottom_center"),
            default_banner_fit_mode=str(raw.get("default_banner_fit_mode") or "contain"),
            default_banner_position=raw.get("default_banner_position") if isinstance(raw.get("default_banner_position"), dict) else {"x": 50, "y": 1600},
        )


def _safe_id(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "channel"))
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "channel"
