from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuildInfo:
    commit: str
    build_date: str
    executable: str
    edition: str = "developer"
    version: str = "0.1.0"
    license_backend_profile: str = "production"
    license_backend_url: str = "https://licensing.creatorassistant.app"
    license_public_keys: dict[str, str] = None

    def __post_init__(self):
        if self.license_public_keys is None:
            object.__setattr__(self, "license_public_keys", {})


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
        version=str(payload.get("version") or "0.1.0"),
        license_backend_profile=str(payload.get("license_backend_profile") or "production"),
        license_backend_url=str(payload.get("license_backend_url") or "https://licensing.creatorassistant.app"),
        license_public_keys=dict(payload.get("license_public_keys") or {}),
    )
