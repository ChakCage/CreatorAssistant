from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from creator_assistant.infrastructure.build_info import current_build_info
from creator_assistant.infrastructure.settings_store import config_root, local_data_root


SECRET_KEYS = {
    "token", "entitlement_token", "refresh_credential", "activation_code",
    "oauth", "client_secret", "private_key", "telegram_token", "password", "cookies",
}
SECRET_PATTERN = re.compile(
    r"(?i)(authorization|token|secret|password|cookie|activation[_ -]?code)\s*[:=]\s*([^\s,;]+)"
)


@dataclass(frozen=True)
class SupportBundlePreview:
    request_id: str
    entries: tuple[str, ...]
    excluded: tuple[str, ...]
    destination: str = ""


class SupportBundleBuilder:
    def __init__(self, settings: dict, dependency_resolutions: dict | None = None) -> None:
        self.settings = settings
        self.dependencies = dependency_resolutions or {}
        self.request_id = str(uuid.uuid4())

    def preview(self) -> SupportBundlePreview:
        entries = ["diagnostic.json", "logs/creator_assistant.log"]
        crash = local_data_root() / "logs" / "crash.log"
        if crash.is_file():
            entries.append("logs/crash.log")
        return SupportBundlePreview(
            request_id=self.request_id,
            entries=tuple(entries),
            excluded=(
                "видео и аудио", "полный transcript", "OAuth/cookies",
                "лицензионные токены и коды", "private keys", "платёжные и Telegram secrets",
            ),
        )

    def create(self, destination: Path) -> SupportBundlePreview:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "diagnostic.json",
                json.dumps(self._diagnostic(), ensure_ascii=False, indent=2),
            )
            for source, name in self._safe_logs():
                archive.writestr(name, _sanitize_log(source.read_text(encoding="utf-8", errors="replace"))[-500_000:])
        os.replace(temporary, destination)
        preview = self.preview()
        return SupportBundlePreview(preview.request_id, preview.entries, preview.excluded, str(destination))

    def _diagnostic(self) -> dict:
        build = current_build_info()
        license_state = {}
        license_path = config_root() / "license_state.json"
        if license_path.is_file():
            try:
                raw = json.loads(license_path.read_text(encoding="utf-8"))
                license_state = {
                    key: raw.get(key) for key in ("state", "plan", "expires_at", "last_refresh", "request_id")
                    if key in raw
                }
            except (OSError, ValueError, TypeError):
                license_state = {"state": "unreadable"}
        return {
            "schema_version": 1,
            "diagnostic_request_id": self.request_id,
            "app": {
                "version": build.version, "edition": build.edition, "variant": build.build_variant,
                "channel": build.channel, "commit": build.commit, "build_date": build.build_date,
                "packaging": build.packaging_format,
            },
            "system": {
                "windows": platform.platform(), "architecture": platform.machine(),
                "cpu": platform.processor(), "ram_bytes": _ram_bytes(), "gpu": _nvidia_summary(),
            },
            "dependencies": {
                str(name): {
                    key: _safe_path(value) if key == "path" else value
                    for key, value in _to_dict(item).items()
                    if key in {"status", "version", "source", "path", "details"}
                }
                for name, item in self.dependencies.items()
            },
            "license": license_state,
            "project": {"stages": [], "proxy_metadata": {}, "render_metadata": {}},
            "privacy": {"automatic_upload": False, "transcript_included": False, "media_included": False},
        }

    @staticmethod
    def _safe_logs() -> Iterable[tuple[Path, str]]:
        root = local_data_root() / "logs"
        for filename in ("creator_assistant.log", "crash.log"):
            path = root / filename
            if path.is_file():
                yield path, f"logs/{filename}"


def _to_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return asdict(value)
    except (TypeError, ValueError):
        return vars(value) if hasattr(value, "__dict__") else {}


def _safe_path(value) -> str:
    path = Path(str(value or ""))
    if not str(path):
        return ""
    return f"<{path.drive or 'disk'}>/{path.name}"


def _sanitize_log(value: str) -> str:
    value = SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    home = str(Path.home())
    return value.replace(home, "<USER>")


def _ram_bytes() -> int:
    try:
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                        ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                        ("total_page_file", ctypes.c_ulonglong), ("available_page_file", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                        ("available_extended_virtual", ctypes.c_ulonglong)]
        status = MemoryStatus(); status.length = ctypes.sizeof(status)
        return status.total_physical if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else 0
    except Exception:
        return 0


def _nvidia_summary() -> dict:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {}
    try:
        output = subprocess.run(
            [executable, "--query-gpu=name,driver_version,memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        name, driver, total, used = [item.strip() for item in output.splitlines()[0].split(",", 3)]
        return {"name": name, "driver": driver, "vram_total_mib": int(total), "vram_used_mib": int(used)}
    except Exception:
        return {}
