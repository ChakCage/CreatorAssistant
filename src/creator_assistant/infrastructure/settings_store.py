from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_SETTINGS: Dict[str, Any] = {
    "youtube_root": r"E:\YouTube",
    "author_paths": [],
    "author_presets": [],
    "selected_author_path": "",
    "suggest_remember_author": True,
    "yt_dlp_path": "",
    "ffmpeg_path": "",
    "ffprobe_path": "",
    "uvr_path": str(Path.home() / "AppData" / "Local" / "Programs" / "Ultimate Vocal Remover" / "UVR_Launcher.exe"),
    "reaper_path": "",
    "vegas_path": "",
    "create_vegas_project_default": True,
    "auto_open_vegas_project": False,
    "uvr_model": "UVR-MDX-NET Inst HQ 3",
    "use_gpu": True,
    "prefer_nvenc": True,
    "open_folder_after_completion": True,
    "reaper_initial_audio": "original",
    "temp_root": "",
    "reaper_proxy_height": 720,
    "disk_reserve_gb": 5,
    "separator_temp_safety_factor": 4.0,
    "whisper_backend": "auto",
    "whisper_python": "",
    "whisper_model_dir": "",
    "whisper_model": "large-v3-turbo",
    "whisper_language": "ru",
    "whisper_device": "auto",
    "whisper_profile": "balanced",
    "whisper_use_gpu": True,
    "whisper_fp16": True,
    "whisper_word_timestamps": True,
    "whisper_use_dictionary": True,
    "whisper_dictionary": [
        "Minecraft", "GTA 6", "PS5 Pro", "OLED", "Beppo", "MylesMC",
        "редстоун", "хардкор", "YouTube", "Shorts", "REAPER", "Vegas",
    ],
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
        self._migrate_author_presets(settings)
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

    @staticmethod
    def _migrate_author_presets(settings: Dict[str, Any]) -> None:
        """Upgrade the legacy path list without losing names or paths."""
        import hashlib

        raw_presets = settings.get("author_presets")
        presets = []
        seen_paths = set()
        if isinstance(raw_presets, list):
            for raw in raw_presets:
                if not isinstance(raw, dict):
                    continue
                root_path = str(raw.get("root_path") or "").strip()
                if not root_path:
                    continue
                key = root_path.casefold()
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                display_name = str(raw.get("display_name") or Path(root_path).parent.name or Path(root_path).name)
                preset_id = str(raw.get("preset_id") or "preset-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12])
                presets.append({
                    "preset_id": preset_id,
                    "display_name": display_name,
                    "root_path": root_path,
                    "youtube_channel_ids": SettingsStore._string_list(raw.get("youtube_channel_ids")),
                    "youtube_handles": SettingsStore._string_list(raw.get("youtube_handles")),
                    "aliases": SettingsStore._string_list(raw.get("aliases")),
                })
        paths = settings.get("author_paths")
        if not isinstance(paths, list):
            paths = []
        for raw_path in paths:
            root_path = str(raw_path or "").strip()
            key = root_path.casefold()
            if not root_path or key in seen_paths:
                continue
            seen_paths.add(key)
            presets.append({
                "preset_id": "preset-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12],
                "display_name": Path(root_path).parent.name or Path(root_path).name,
                "root_path": root_path,
                "youtube_channel_ids": [],
                "youtube_handles": [],
                "aliases": [],
            })
        settings["author_presets"] = presets
        settings["suggest_remember_author"] = bool(settings.get("suggest_remember_author", True))

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
