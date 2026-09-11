from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from creator_assistant.version import __version__


@dataclass(frozen=True)
class BuildInfo:
    commit: str
    build_date: str
    executable: str
    edition: str = "developer"
    version: str = __version__
    channel: str = "developer"
    build_number: int = 0
    packaging_format: str = "source"
    architecture: str = "x86_64"
    license_backend_profile: str = "production"
    license_backend_url: str = "https://licensing.creatorassistant.app"
    license_public_keys: dict[str, str] = None
    build_variant: str = "production"
    telegram_bot_url: str = ""
    update_backend_url: str = ""
    update_public_keys: dict[str, str] = None
    update_key_id: str = ""
    installer_signed: bool = False

    def __post_init__(self):
        if self.license_public_keys is None:
            object.__setattr__(self, "license_public_keys", {})
        if self.update_public_keys is None:
            object.__setattr__(self, "update_public_keys", {})


def current_build_info() -> BuildInfo:
    executable = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve()
    candidates = []
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        candidates.append(Path(bundle) / "creator_assistant" / "build_info.json")
    candidates.append(Path(__file__).resolve().parents[3] / "build" / "generated" / "build_info.json")
    payload = {}
    for path in candidates:
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            payload = value
            break
    return BuildInfo(
        commit=str(payload.get("commit") or "development"),
        build_date=str(payload.get("build_date") or "неизвестно"),
        executable=str(executable),
        edition=str(payload.get("edition") or "developer"),
        version=str(payload.get("version") or __version__),
        channel=str(payload.get("channel") or ("developer" if payload.get("edition", "developer") == "developer" else "stable")),
        build_number=int(payload.get("build_number") or 0),
        packaging_format=str(payload.get("packaging_format") or ("pyinstaller" if getattr(sys, "frozen", False) else "source")),
        architecture=str(payload.get("architecture") or "x86_64"),
        license_backend_profile=str(payload.get("license_backend_profile") or "production"),
        # An explicitly empty URL is a security boundary for standalone builds;
        # only legacy metadata with no key at all receives the old default.
        license_backend_url=str(
            payload["license_backend_url"]
            if "license_backend_url" in payload
            else "https://licensing.creatorassistant.app"
        ),
        license_public_keys=dict(payload.get("license_public_keys") or {}),
        build_variant=str(payload.get("build_variant") or "production"),
        telegram_bot_url=str(payload.get("telegram_bot_url") or ""),
        update_backend_url=str(payload.get("update_backend_url") or ""),
        update_public_keys=dict(payload.get("update_public_keys") or {}),
        update_key_id=str(payload.get("update_key_id") or ""),
        installer_signed=bool(payload.get("installer_signed", False)),
    )
