from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from creator_assistant.infrastructure.settings_store import config_root


@dataclass(frozen=True)
class LicenseStatus:
    active: bool
    activated_at: str = ""
    fingerprint: str = ""


class LicenseService:
    """Edition-local license state. Raw license keys are never persisted."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config_root() / "license.json")

    def status(self) -> LicenseStatus:
        if not self.path.is_file():
            return LicenseStatus(False)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return LicenseStatus(bool(value.get("active")), str(value.get("activated_at", "")), str(value.get("fingerprint", "")))
        except (OSError, ValueError, TypeError):
            return LicenseStatus(False)

    def activate(self, key: str) -> LicenseStatus:
        normalized = "".join(str(key).strip().split())
        if len(normalized) < 12:
            raise ValueError("Лицензионный ключ слишком короткий.")
        value = LicenseStatus(
            True,
            datetime.now(timezone.utc).isoformat(),
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(self.path))
        return value
