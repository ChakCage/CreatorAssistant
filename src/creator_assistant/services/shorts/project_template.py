from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from creator_assistant.domain.shorts.models import Candidate


TRANSLATED_SOURCE_TITLE = "TRANSLATED_SOURCE_TITLE"


def normalized_scale_percent(value: Any) -> int:
    """Accept legacy ratio values and current integer percentages exactly once."""
    raw = float(value or 100)
    return round(raw * 100) if 0 < raw <= 5.5 else round(raw)


@dataclass(frozen=True)
class ProjectShortsTemplate:
    subtitle: dict[str, Any] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)
    branding: dict[str, Any] = field(default_factory=dict)
    render: dict[str, Any] = field(default_factory=lambda: {
        "width": 1080,
        "height": 1920,
        "fps_policy": "source",
        "encoder": "h264_nvenc",
        "audio_codec": "aac",
    })
    title_mode: str = TRANSLATED_SOURCE_TITLE
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ProjectShortsTemplate | None":
        if not isinstance(raw, dict) or not raw:
            return None
        return cls(
            subtitle=dict(raw.get("subtitle") or {}),
            layout=dict(raw.get("layout") or {}),
            branding=dict(raw.get("branding") or {}),
            render=dict(raw.get("render") or {}),
            title_mode=str(raw.get("title_mode") or TRANSLATED_SOURCE_TITLE),
            schema_version=int(raw.get("schema_version", 1) or 1),
        )

    @classmethod
    def from_candidate(cls, candidate: Candidate, render: dict[str, Any] | None = None) -> "ProjectShortsTemplate":
        subtitle = {key: value for key, value in candidate.subtitle_settings.items() if key != "cues"}
        branding = {
            key: value for key, value in candidate.branding_settings.items()
            if key not in {
                "source_author", "channel_profile_id", "channel_banner_path",
                "original_video_title", "original_video_title_source", "translated_video_title",
                "short_hook_title", "short_hook_suggestions", "title_suggestions", "final_title_text",
            }
        }
        return cls(
            subtitle=subtitle,
            layout=dict(candidate.layout_settings),
            branding=branding,
            render=dict(render or cls().render),
        )


def composition_snapshot_hash(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_identity(
    source_fingerprint: str,
    candidate_id: str,
    start: float,
    end: float,
    snapshot_hash: str,
) -> str:
    value = {
        "source_fingerprint": source_fingerprint,
        "candidate_id": candidate_id,
        "start_ms": round(start * 1000),
        "end_ms": round(end * 1000),
        "composition_snapshot_hash": snapshot_hash,
    }
    return composition_snapshot_hash(value)
