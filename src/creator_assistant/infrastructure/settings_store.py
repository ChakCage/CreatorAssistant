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
    "auto_shorts_project_folder": True,
    "shorts_default_video_folder": "",
    "shorts_last_video_folder": "",
    "shorts_channel_profile_links": {},
    "shorts_branding_defaults": {
        "preset": "clean",
        "show_subtitles": True,
        "show_title": False,
        "final_title_text": "",
        "title_size": 78,
        "title_bold": True,
        "title_color": "#ffffff",
        "title_outline": 4,
        "title_background": False,
        "title_y": 180,
        "title_alignment": "center",
        "title_offset_x": 0,
        "title_font_family": "Segoe UI",
        "use_subtitle_font_for_title": True,
        "title_max_lines": 2,
        "show_channel_card": False,
        "channel_profile_id": "",
        "banner_scale": 100,
        "banner_anchor": "bottom_center",
        "banner_fit_mode": "contain",
        "banner_offset_x": 0,
        "banner_offset_y": 0,
        "banner_opacity": 100,
        "safe_margin": 80,
        "use_defaults": True,
    },
    "shorts_subtitle_defaults": {
        "style": "clean",
        "position": "lower",
        "alignment": "center",
        "horizontal_offset": 0,
        "font_family": "Segoe UI",
        "vertical_offset": 0,
        "size": 58,
        "maximum": 36,
        "lines": 2,
        "outline": 3,
        "shadow": 1,
        "auto_above_banner": True,
        "line_anchor_mode": "first_line_fixed",
        "banner_gap": 15,
        "layout_mode": "center_crop",
        "foreground_scale": 100,
        "crop_center": 50,
    },
    "shorts_subtitle_presets": {
        "clean": {
            "style": "clean", "font_family": "Segoe UI", "size": 58,
            "position": "lower", "horizontal_offset": 0, "vertical_offset": 0,
            "alignment": "center", "outline": 3, "shadow": 1,
            "maximum": 36, "lines": 2, "banner_gap": 15,
        },
        "large": {
            "style": "large", "font_family": "Segoe UI", "size": 80,
            "position": "lower", "horizontal_offset": 0, "vertical_offset": 0,
            "alignment": "center", "outline": 5, "shadow": 2,
            "maximum": 24, "lines": 2, "banner_gap": 15,
        },
        "gaming": {
            "style": "gaming", "font_family": "Arial Black", "size": 70,
            "position": "lower", "horizontal_offset": 0, "vertical_offset": 0,
            "alignment": "center", "outline": 5, "shadow": 3,
            "maximum": 28, "lines": 2, "banner_gap": 15,
        },
    },
    "window_geometry": "",
    "publishing": {
        "mode": "DRY_RUN",
        "start_agent_with_windows": False,
        "google_client_config_id": "",
        "tiktok_client_config_id": "",
        "real_publishing_enabled": False,
    },
    "shorts_ai": {
        "enabled": True,
        "backend": "ollama",
        "endpoint": "http://127.0.0.1:11434",
        "model": "qwen3:14b",
        "model_digest": "",
        "model_quantization": "",
        "mode": "balanced",
        "context_length": 16384,
        "preliminary_count": 40,
        "final_count": 5,
        "timeout": 180,
        "fallback": True,
        "cache": True,
        "show_reasons": True,
        "global_comparison": True,
        "weights": {"semantic": 0.55, "heuristic": 0.25, "activity": 0.15, "uniqueness": 0.05},
    },
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
        self._migrate_shorts_ai(settings)
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
    def _migrate_shorts_ai(settings: Dict[str, Any]) -> None:
        ai = settings.setdefault("shorts_ai", {})
        if ai.get("mode") == "quality":
            ai["mode"] = "deep"
        ai.setdefault("model_digest", "")
        ai.setdefault("model_quantization", "")
        ai.setdefault("context_length", {"fast": 8192, "balanced": 16384, "deep": 32768}.get(ai.get("mode"), 16384))

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
