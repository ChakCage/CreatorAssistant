from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from creator_assistant.domain.shorts.models import Candidate
from creator_assistant.services.shorts.channel_assets import ChannelAssetStore, ChannelProfile
from creator_assistant.services.shorts.project_template import ProjectShortsTemplate, TRANSLATED_SOURCE_TITLE


@dataclass(frozen=True)
class ResolvedVerticalRenderSettings:
    subtitle: dict[str, Any]
    layout: dict[str, Any]
    branding: dict[str, Any]
    profile_id: str = ""


class VerticalRenderSettingsResolver:
    """Resolve the persisted Vertical Editor state used by automated renders.

    Candidate manifests may contain visual values copied from an older default.  Those
    values must not silently override the user's currently persisted preset.  Only
    candidate-specific content (subtitle cues and generated titles) is retained here.
    """

    _DYNAMIC_SUBTITLE_KEYS = ("cues",)
    _DYNAMIC_BRANDING_KEYS = (
        "source_author",
        "original_video_title",
        "original_video_title_source",
        "translated_video_title",
        "short_hook_title",
        "final_title_text",
    )

    def __init__(self, settings: dict[str, Any], assets: ChannelAssetStore | None = None) -> None:
        self.settings = settings
        self.assets = assets or ChannelAssetStore()

    def resolve(
        self,
        candidate: Candidate,
        *,
        channel_id: str = "",
        source_author: str = "",
        aliases: list[str] | None = None,
        selected_profile_id: str = "",
        selected_subtitle_preset: str = "",
        profile_layout: dict[str, Any] | None = None,
        job_layout: dict[str, Any] | None = None,
        project_template: ProjectShortsTemplate | dict[str, Any] | None = None,
        use_candidate_override: bool | None = None,
    ) -> ResolvedVerticalRenderSettings:
        template = (
            ProjectShortsTemplate.from_dict(project_template)
            if isinstance(project_template, dict) else project_template
        )
        override = candidate.settings_override if use_candidate_override is None else use_candidate_override
        subtitle_defaults = dict(self.settings.get("shorts_subtitle_defaults", {}))
        presets = self.settings.get("shorts_subtitle_presets", {})
        requested_style = str(selected_subtitle_preset or "").strip()
        style = requested_style or str(subtitle_defaults.get("style") or "clean")
        preset = dict(presets.get(style, {})) if isinstance(presets, dict) else {}

        # The active preset is persisted separately and is authoritative for its
        # style fields; defaults supply layout and any fields absent in the preset.
        subtitle = {**subtitle_defaults, **preset}
        subtitle["style"] = style
        if template:
            subtitle.update(template.subtitle)
        if override:
            subtitle.update({key: value for key, value in candidate.subtitle_settings.items() if key != "cues"})
        for key in self._DYNAMIC_SUBTITLE_KEYS:
            if key in candidate.subtitle_settings:
                subtitle[key] = candidate.subtitle_settings[key]

        layout = {
            "mode": str(subtitle_defaults.get("layout_mode") or "center_crop"),
            "crop_center": int(subtitle_defaults.get("crop_center", 50) or 50),
            "foreground_scale": int(subtitle_defaults.get("foreground_scale", 100) or 100),
            "background_color": str(subtitle_defaults.get("background_color") or "black"),
            **dict(profile_layout or {}),
            **dict(job_layout or {}),
        }
        if template:
            layout.update(template.layout)
        if override:
            layout.update(candidate.layout_settings)

        branding_defaults = dict(self.settings.get("shorts_branding_defaults", {}))
        profile = self._resolve_profile(
            channel_id=channel_id,
            source_author=source_author,
            aliases=aliases or [],
            selected_profile_id=selected_profile_id,
            saved_profile_id=str(branding_defaults.get("channel_profile_id") or ""),
        )
        banner = self.assets.banner_path(profile) if hasattr(self.assets, "banner_path") else None
        asset = self.assets.asset(profile) if profile and hasattr(self.assets, "asset") else None
        profile_branding = self._profile_branding(profile, banner, asset)
        branding = {**branding_defaults, **profile_branding}
        if template:
            branding.update(template.branding)
        if override:
            branding.update({
                key: value for key, value in candidate.branding_settings.items()
                if key not in {"channel_profile_id", "channel_banner_path"}
            })
        # Channel identity and image are resolved for the current author; template
        # geometry remains shared between channels.
        branding["channel_profile_id"] = profile.id if profile else ""
        branding["channel_banner_path"] = str(banner or "")
        for key in self._DYNAMIC_BRANDING_KEYS:
            value = candidate.branding_settings.get(key)
            if value not in (None, ""):
                branding[key] = value
        if template and template.title_mode == TRANSLATED_SOURCE_TITLE and not override:
            translated = str(candidate.branding_settings.get("translated_video_title") or "").strip()
            original = str(candidate.branding_settings.get("original_video_title") or "").strip()
            branding["final_title_text"] = translated or original
            branding["title_mode"] = TRANSLATED_SOURCE_TITLE
        if not str(branding.get("final_title_text") or "").strip():
            branding["final_title_text"] = str(candidate.title or "")
        return ResolvedVerticalRenderSettings(
            subtitle=subtitle,
            layout=layout,
            branding=branding,
            profile_id=profile.id if profile else "",
        )

    def _resolve_profile(
        self,
        *,
        channel_id: str,
        source_author: str,
        aliases: list[str],
        selected_profile_id: str,
        saved_profile_id: str,
    ) -> ChannelProfile | None:
        # A profile explicitly selected in Autopilot is authoritative.
        explicit_id = str(selected_profile_id or saved_profile_id or "").strip()
        if explicit_id:
            explicit = self.assets.get(explicit_id) if hasattr(self.assets, "get") else None
            if explicit and hasattr(self.assets, "banner_path") and self.assets.banner_path(explicit):
                return explicit

        linked = self.settings.get("shorts_channel_profile_links", {})
        linked_id = ""
        if isinstance(linked, dict):
            linked_id = next(
                (str(linked.get(str(value).casefold()) or "") for value in [source_author, *aliases]
                 if value and linked.get(str(value).casefold())),
                "",
            )
        if not hasattr(self.assets, "resolve"):
            return None
        return self.assets.resolve(
            channel_id=channel_id,
            saved_profile_id=linked_id,
            source_author=source_author,
            aliases=aliases,
        )

    @staticmethod
    def _profile_branding(profile: ChannelProfile | None, banner: Path | None, asset=None) -> dict[str, Any]:
        if not profile or not banner:
            return {
                "channel_profile_id": "",
                "channel_banner_path": "",
                "brand_asset_id": "",
                "brand_asset_status": "NEEDS_REVIEW",
                "show_channel_card": False,
            }
        return {
            "channel_profile_id": profile.id,
            "channel_banner_path": str(banner),
            "brand_asset_id": asset.asset_id if asset else profile.brand_asset_id,
            "brand_asset_hash": asset.content_hash if asset else "",
            "brand_asset_type": asset.asset_type if asset else "IMAGE",
            "brand_asset_has_alpha": bool(asset.has_alpha) if asset else False,
            "brand_asset_has_audio": bool(asset.has_audio) if asset else False,
            "brand_asset_width": int(asset.width) if asset else 0,
            "brand_asset_height": int(asset.height) if asset else 0,
            "brand_asset_duration": float(asset.duration) if asset else 0.0,
            "brand_asset_codec": str(asset.codec) if asset else "",
            "brand_asset_status": "READY",
            "show_channel_card": True,
            "banner_scale": profile.default_banner_scale,
            "banner_offset_x": profile.default_banner_offset_x,
            "banner_offset_y": profile.default_banner_offset_y,
            "banner_opacity": profile.default_banner_opacity,
            "banner_anchor": profile.default_banner_anchor,
            "banner_fit_mode": profile.default_banner_fit_mode,
            "banner_start_offset": profile.default_banner_start_offset,
            "banner_display_duration": profile.default_banner_display_duration,
            "banner_loop": profile.default_banner_loop,
            "banner_trim_to_short": profile.default_banner_trim_to_short,
            "banner_freeze_last_frame": profile.default_banner_freeze_last_frame,
            "banner_audio_enabled": profile.default_banner_audio_enabled,
            "banner_audio_volume": profile.default_banner_audio_volume,
        }
