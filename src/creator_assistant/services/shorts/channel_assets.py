from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from creator_assistant.infrastructure.settings_store import config_root
from creator_assistant.services.shorts.brand_assets import BrandAsset, BrandAssetLibrary


@dataclass(frozen=True)
class ChannelProfile:
    id: str
    display_name: str
    source_author: str
    aliases: tuple[str, ...]
    subscribe_banner: str
    brand_asset_id: str = ""
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
    default_banner_start_offset: float = 0.0
    default_banner_display_duration: float = 0.0
    default_banner_loop: bool = True
    default_banner_trim_to_short: bool = True
    default_banner_freeze_last_frame: bool = False
    default_banner_audio_enabled: bool = False
    default_banner_audio_volume: int = 100

    @property
    def searchable(self) -> set[str]:
        values = {self.id, self.display_name, self.source_author, self.handle, *self.aliases, *self.channel_ids}
        return {item.casefold() for item in values if item}


class ChannelAssetStore:
    """Channel profiles backed by one cross-edition BrandAssetLibrary."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        library: BrandAssetLibrary | None = None,
        ffprobe_path: str = "",
        ffmpeg_path: str = "",
        project_roots: list[Path] | None = None,
    ) -> None:
        library_root = Path(root).parent / ".brand-assets" if root is not None else None
        self.library = library or BrandAssetLibrary(library_root, ffprobe_path=ffprobe_path, ffmpeg_path=ffmpeg_path)
        self.root = Path(root) if root is not None else self.library.profiles_root
        self._uses_shared_root = root is None
        self._migration_checked = False
        self.project_roots = [Path(value) for value in (project_roots or []) if value]

    def ensure_root(self) -> Path:
        self.library.ensure_root()
        self.root.mkdir(parents=True, exist_ok=True)
        if self._uses_shared_root and not self._migration_checked:
            self._migration_checked = True
            self.refresh_library()
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

    def delete_profile(self, profile_id: str) -> bool:
        """Remove only the profile reference; shared/original media remains untouched."""
        profile = self.get(profile_id)
        if not profile:
            return False
        profile_file = self.root / profile.id / "profile.json"
        if not profile_file.is_file():
            return False
        profile_file.unlink()
        try:
            profile_file.parent.rmdir()
        except OSError:
            pass
        return True

    def banner_path(self, profile: ChannelProfile | str | None) -> Path | None:
        if isinstance(profile, str):
            profile = self.get(profile)
        if not profile or not profile.enabled:
            return None
        if profile.brand_asset_id:
            managed = self.library.path_for(profile.brand_asset_id)
            if managed:
                return managed
        if not profile.subscribe_banner:
            return None
        legacy = self.root / profile.id / profile.subscribe_banner
        return legacy if legacy.is_file() else None

    def asset(self, profile: ChannelProfile | str | None) -> BrandAsset | None:
        if isinstance(profile, str):
            profile = self.get(profile)
        return self.library.get(profile.brand_asset_id) if profile and profile.brand_asset_id else None

    def resolve(
        self,
        *,
        channel_id: str = "",
        saved_profile_id: str = "",
        source_author: str = "",
        aliases: list[str] | None = None,
    ) -> ChannelProfile | None:
        profiles = [item for item in self.profiles() if item.enabled]
        if channel_id:
            key = channel_id.casefold()
            matches = [item for item in profiles if key in {cid.casefold() for cid in item.channel_ids} and self.banner_path(item)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None
        if saved_profile_id:
            saved = next((item for item in profiles if item.id.casefold() == saved_profile_id.casefold()), None)
            if saved and self.banner_path(saved):
                return saved
        if source_author:
            key = source_author.casefold()
            exact = [item for item in profiles if item.source_author.casefold() == key and self.banner_path(item)]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                return None
        for value in aliases or []:
            key = str(value).casefold()
            matches = [item for item in profiles if key in item.searchable and self.banner_path(item)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None
        if source_author:
            key = source_author.casefold()
            matches = [item for item in profiles if key in item.searchable and self.banner_path(item)]
            if len(matches) == 1:
                return matches[0]
        return None

    def import_banner(self, profile_id: str, source: Path, metadata: dict[str, Any] | None = None) -> ChannelProfile:
        return self.import_brand_asset(profile_id, source, metadata)

    def import_brand_asset(self, profile_id: str, source: Path, metadata: dict[str, Any] | None = None) -> ChannelProfile:
        asset = self.library.import_asset(source)
        safe_id = _safe_id(profile_id)
        self.ensure_root()
        previous = self.get(safe_id)
        profile_dir = self.root / safe_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        meta = metadata or {}
        raw = {
            "id": safe_id,
            "display_name": meta.get("display_name", previous.display_name if previous else safe_id),
            "handle": meta.get("handle", previous.handle if previous else ""),
            "source_author": meta.get("source_author", previous.source_author if previous else safe_id),
            "aliases": meta.get("aliases", list(previous.aliases) if previous else []),
            "channel_ids": list(previous.channel_ids) if previous else [],
            "subscribe_banner": asset.original_name,
            "brand_asset_id": asset.asset_id,
            "enabled": True,
            "default_cta": previous.default_cta if previous else "Подписаться",
            "default_banner_scale": previous.default_banner_scale if previous else 100,
            "default_banner_position": previous.default_banner_position if previous else {"x": 0, "y": 0},
            "default_banner_offset_x": previous.default_banner_offset_x if previous else 0,
            "default_banner_offset_y": previous.default_banner_offset_y if previous else 0,
            "default_banner_opacity": previous.default_banner_opacity if previous else 100,
            "default_banner_anchor": previous.default_banner_anchor if previous else "bottom_center",
            "default_banner_fit_mode": previous.default_banner_fit_mode if previous else "contain",
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
            "brand_asset_id": profile.brand_asset_id,
            "enabled": profile.enabled,
            "default_cta": profile.default_cta,
            "default_banner_scale": profile.default_banner_scale,
            "default_banner_offset_x": profile.default_banner_offset_x,
            "default_banner_offset_y": profile.default_banner_offset_y,
            "default_banner_opacity": profile.default_banner_opacity,
            "default_banner_anchor": profile.default_banner_anchor,
            "default_banner_fit_mode": profile.default_banner_fit_mode,
            "default_banner_start_offset": profile.default_banner_start_offset,
            "default_banner_display_duration": profile.default_banner_display_duration,
            "default_banner_loop": profile.default_banner_loop,
            "default_banner_trim_to_short": profile.default_banner_trim_to_short,
            "default_banner_freeze_last_frame": profile.default_banner_freeze_last_frame,
            "default_banner_audio_enabled": profile.default_banner_audio_enabled,
            "default_banner_audio_volume": profile.default_banner_audio_volume,
            "default_banner_position": profile.default_banner_position or {"x": 0, "y": 0},
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
            "default_banner_start_offset": float(settings.get("banner_start_offset", 0.0) or 0.0),
            "default_banner_display_duration": float(settings.get("banner_display_duration", 0.0) or 0.0),
            "default_banner_loop": bool(settings.get("banner_loop", True)),
            "default_banner_trim_to_short": bool(settings.get("banner_trim_to_short", True)),
            "default_banner_freeze_last_frame": bool(settings.get("banner_freeze_last_frame", False)),
            "default_banner_audio_enabled": bool(settings.get("banner_audio_enabled", False)),
            "default_banner_audio_volume": int(settings.get("banner_audio_volume", 100) or 100),
        })
        self._write_profile(self.root / profile.id / "profile.json", raw)

    def refresh_library(self, developer_only: bool = False) -> dict[str, Any]:
        """Copy legacy/edition assets into shared storage without deleting sources."""
        legacy_base = config_root().parent
        sources = [legacy_base / "Developer" / "user_assets" / "channels"]
        if not developer_only:
            sources.extend([
                legacy_base / "user_assets" / "channels",
                legacy_base / "Commercial" / "user_assets" / "channels",
            ])
        report: dict[str, Any] = {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sources": [str(path) for path in sources],
            "imported_assets": [],
            "deduplicated_assets": [],
            "profiles": [],
            "errors": [],
        }
        known_hashes = {item.content_hash for item in self.library.assets()}
        for source_root in sources:
            if not source_root.is_dir() or source_root.resolve() == self.root.resolve():
                continue
            for profile_file in sorted(source_root.glob("*/profile.json")):
                try:
                    raw = json.loads(profile_file.read_text(encoding="utf-8-sig"))
                    profile_id = _safe_id(str(raw.get("id") or profile_file.parent.name))
                    legacy_name = str(raw.get("subscribe_banner") or raw.get("subscribe_banner_path") or "")
                    legacy_asset = profile_file.parent / legacy_name
                    if not legacy_asset.is_file():
                        report["errors"].append({"profile": profile_id, "error": "asset_missing", "path": str(legacy_asset)})
                        continue
                    asset = self.library.import_asset(legacy_asset)
                    bucket = "deduplicated_assets" if asset.content_hash in known_hashes else "imported_assets"
                    if asset.asset_id not in report[bucket]:
                        report[bucket].append(asset.asset_id)
                    known_hashes.add(asset.content_hash)
                    target_file = self.root / profile_id / "profile.json"
                    existing = json.loads(target_file.read_text(encoding="utf-8-sig")) if target_file.is_file() else {}
                    merged = dict(raw)
                    merged.update(existing)
                    merged["id"] = profile_id
                    merged["brand_asset_id"] = str(existing.get("brand_asset_id") or asset.asset_id)
                    merged["subscribe_banner"] = str(existing.get("subscribe_banner") or legacy_name)
                    merged["aliases"] = sorted(set(map(str, raw.get("aliases", []))) | set(map(str, existing.get("aliases", []))), key=str.casefold)
                    merged["channel_ids"] = sorted(set(map(str, raw.get("channel_ids", []))) | set(map(str, existing.get("channel_ids", []))), key=str.casefold)
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    self._write_profile(target_file, merged)
                    if profile_id not in report["profiles"]:
                        report["profiles"].append(profile_id)
                except (OSError, ValueError, TypeError) as exc:
                    report["errors"].append({"profile_file": str(profile_file), "error": str(exc)})
        if not developer_only:
            self._import_project_references(report, known_hashes)
        self.library.root.mkdir(parents=True, exist_ok=True)
        temporary = self.library.migration_report_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(self.library.migration_report_path))
        return report

    def _import_project_references(self, report: dict[str, Any], known_hashes: set[str]) -> None:
        """Recover only explicitly referenced, existing assets from known Shorts projects."""
        for project_root in self.project_roots:
            if not project_root.is_dir():
                continue
            try:
                manifests = project_root.rglob("shorts_manifest.json")
                for manifest_path in manifests:
                    try:
                        raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                        candidates = list(raw.get("candidates") or []) + list(raw.get("approved_clips") or [])
                        for candidate in candidates:
                            branding = dict(candidate.get("branding_settings") or {})
                            source = Path(str(branding.get("channel_banner_path") or ""))
                            if not source.is_file() or source.suffix.casefold() not in {
                                ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".webm",
                            }:
                                continue
                            profile_id = str(branding.get("channel_profile_id") or source.stem or "recovered")
                            profile = self.import_brand_asset(profile_id, source, {
                                "display_name": profile_id,
                                "source_author": profile_id,
                                "aliases": [profile_id],
                            })
                            asset = self.asset(profile)
                            if not asset:
                                continue
                            bucket = "deduplicated_assets" if asset.content_hash in known_hashes else "imported_assets"
                            if asset.asset_id not in report[bucket]:
                                report[bucket].append(asset.asset_id)
                            known_hashes.add(asset.content_hash)
                    except (OSError, ValueError, TypeError) as exc:
                        report["errors"].append({"manifest": str(manifest_path), "error": str(exc)})
            except OSError as exc:
                report["errors"].append({"project_root": str(project_root), "error": str(exc)})

    @staticmethod
    def _write_profile(path: Path, raw: dict[str, Any]) -> None:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(path))

    @staticmethod
    def _profile_from_dict(raw: dict[str, Any]) -> ChannelProfile:
        return ChannelProfile(
            id=str(raw.get("id") or ""),
            display_name=str(raw.get("display_name") or raw.get("id") or ""),
            handle=str(raw.get("handle") or ""),
            source_author=str(raw.get("source_author") or ""),
            aliases=tuple(str(item) for item in raw.get("aliases", []) if str(item).strip()),
            channel_ids=tuple(str(item) for item in raw.get("channel_ids", []) if str(item).strip()),
            subscribe_banner=str(raw.get("subscribe_banner") or raw.get("subscribe_banner_path") or ""),
            brand_asset_id=str(raw.get("brand_asset_id") or raw.get("asset_id") or ""),
            enabled=bool(raw.get("enabled", True)),
            default_cta=str(raw.get("default_cta") or "Подписаться"),
            default_banner_scale=int(raw.get("default_banner_scale", 100) or 100),
            default_banner_offset_x=int(raw.get("default_banner_offset_x", 0) or 0),
            default_banner_offset_y=int(raw.get("default_banner_offset_y", 0) or 0),
            default_banner_opacity=int(raw.get("default_banner_opacity", 100) or 100),
            default_banner_anchor=str(raw.get("default_banner_anchor") or "bottom_center"),
            default_banner_fit_mode=str(raw.get("default_banner_fit_mode") or "contain"),
            default_banner_start_offset=float(raw.get("default_banner_start_offset", 0.0) or 0.0),
            default_banner_display_duration=float(raw.get("default_banner_display_duration", 0.0) or 0.0),
            default_banner_loop=bool(raw.get("default_banner_loop", True)),
            default_banner_trim_to_short=bool(raw.get("default_banner_trim_to_short", True)),
            default_banner_freeze_last_frame=bool(raw.get("default_banner_freeze_last_frame", False)),
            default_banner_audio_enabled=bool(raw.get("default_banner_audio_enabled", False)),
            default_banner_audio_volume=int(raw.get("default_banner_audio_volume", 100) or 100),
            default_banner_position=raw.get("default_banner_position") if isinstance(raw.get("default_banner_position"), dict) else {"x": 0, "y": 0},
        )


def _safe_id(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "channel"))
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "channel"
