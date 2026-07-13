from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_SETTINGS: Dict[str, Any] = {
    "youtube_root": r"E:\YouTube",
    "author_paths": [],
    "selected_author_path": "",
    "yt_dlp_path": "",
    "ffmpeg_path": "",
    "ffprobe_path": "",
    "uvr_path": str(Path.home() / "AppData" / "Local" / "Programs" / "Ultimate Vocal Remover" / "UVR_Launcher.exe"),
    "reaper_path": "",
    "uvr_model": "UVR-MDX-NET Inst HQ 3",
    "use_gpu": True,
    "prefer_nvenc": True,
    "open_folder_after_completion": True,
    "reaper_initial_audio": "original",
    "temp_root": "",
    "reaper_proxy_height": 720,
    "disk_reserve_gb": 5,
    "separator_temp_safety_factor": 4.0,
    "last_update_check": "",
    "latest_yt_dlp_version": "",
    "dependency_sources": {},
    "youtube_access": {
        "mode": "automatic",
        "browser": "",
        "browser_profile": "",
        "cookies_file": "",
        "always_use": False,
        "schema_version": 2,
    },
    "naming": {
        "maximum": "{title} [MAX {height}p]",
        "proxy": "{title} [{proxy_height}p]",
        "audio": "{title} [Audio]",
        "instrumental": "{title} [Instrumental]",
        "preview": "Preview",
    },
}


def config_root() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "CreatorAssistant"


def local_data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CreatorAssistant"


class SettingsStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (config_root() / "settings.json")

    def load(self) -> Dict[str, Any]:
        settings = deepcopy(DEFAULT_SETTINGS)
        if not self.path.exists():
            return settings
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return settings
        if isinstance(raw, dict):
            self._merge(settings, raw)
        self._migrate_youtube_access(settings)
        self._migrate_processing(settings)
        return settings

    def save(self, settings: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(self.path))

    @staticmethod
    def _merge(target: Dict[str, Any], source: Dict[str, Any]) -> None:
        for key, value in source.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                SettingsStore._merge(target[key], value)
            elif key in target:
                target[key] = value

    @staticmethod
    def _migrate_youtube_access(settings: Dict[str, Any]) -> None:
        access = settings.setdefault("youtube_access", {})
        always_use = bool(access.get("always_use", False))
        if not always_use:
            access.update({
                "mode": "automatic",
                "browser": "",
                "browser_profile": "",
                "cookies_file": "",
                "always_use": False,
                "schema_version": 2,
            })
            return
        mode = str(access.get("mode", "automatic"))
        if mode == "browser" and not access.get("browser"):
            access["browser"] = "firefox"
        access["schema_version"] = 2

    @staticmethod
    def _migrate_processing(settings: Dict[str, Any]) -> None:
        try:
            height = int(settings.get("reaper_proxy_height", 720))
        except (TypeError, ValueError):
            height = 720
        settings["reaper_proxy_height"] = height if height in {480, 720, 1080} else 720
        settings["temp_root"] = str(settings.get("temp_root") or "")
        naming = settings.setdefault("naming", {})
        if naming.get("proxy") == "{title} [720p]":
            naming["proxy"] = "{title} [{proxy_height}p]"
