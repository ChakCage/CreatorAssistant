from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from creator_assistant.domain.errors import ValidationError


AUTH_AUTO = "automatic"
AUTH_NONE = "none"
AUTH_BROWSER = "browser"
AUTH_COOKIES_FILE = "cookies_file"

BROWSERS = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "firefox": "Mozilla Firefox",
    "brave": "Brave",
    "chromium": "Chromium",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
}


@dataclass
class YtDlpAuthContext:
    mode: str = AUTH_AUTO
    browser: str = ""
    browser_profile: str = ""
    cookies_file: str = ""
    enabled: bool = False
    user_consented: bool = False

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "YtDlpAuthContext":
        data = settings.get("youtube_access", {})
        configured_mode = str(data.get("mode", AUTH_AUTO))
        always_use = bool(data.get("always_use", False))
        browser = str(data.get("browser", ""))
        if configured_mode in BROWSERS:
            browser = configured_mode
            configured_mode = AUTH_BROWSER
        if configured_mode not in {AUTH_AUTO, AUTH_NONE, AUTH_BROWSER, AUTH_COOKIES_FILE}:
            configured_mode = AUTH_AUTO
        return cls(
            mode=configured_mode,
            browser=browser,
            browser_profile=str(data.get("browser_profile", "")).strip(),
            cookies_file=str(data.get("cookies_file", "")).strip(),
            enabled=always_use and configured_mode in {AUTH_BROWSER, AUTH_COOKIES_FILE},
            user_consented=always_use,
        )

    @property
    def effective_mode(self) -> str:
        if not self.enabled or self.mode in {AUTH_AUTO, AUTH_NONE}:
            return "anonymous"
        if self.mode == AUTH_BROWSER:
            return self.browser or "browser"
        return AUTH_COOKIES_FILE

    def disable_for_anonymous_job(self) -> None:
        self.enabled = False
        self.user_consented = False

    def enable_one_time(self, mode: str, browser: str = "", profile: str = "", cookies_file: str = "") -> None:
        self.mode = mode
        self.browser = browser
        self.browser_profile = profile.strip()
        self.cookies_file = cookies_file.strip()
        self.enabled = mode in {AUTH_BROWSER, AUTH_COOKIES_FILE}
        self.user_consented = self.enabled

    def arguments(self, validate: bool = True) -> list[str]:
        if not self.enabled or self.mode in {AUTH_AUTO, AUTH_NONE}:
            return []
        if self.mode == AUTH_BROWSER:
            if self.browser not in BROWSERS:
                raise ValidationError("Выбран неподдерживаемый браузер для cookies YouTube.")
            if validate:
                installation = find_browser(self.browser)
                if not installation:
                    raise ValidationError(f"{BROWSERS[self.browser]} не найден. Выберите другой браузер.")
                if self.browser_profile and not browser_profile_exists(self.browser, self.browser_profile):
                    raise ValidationError(f"Профиль {BROWSERS[self.browser]} «{self.browser_profile}» не найден.")
            value = self.browser + (f":{self.browser_profile}" if self.browser_profile else "")
            return ["--cookies-from-browser", value]
        if self.mode == AUTH_COOKIES_FILE:
            if validate:
                validate_cookies_file(Path(self.cookies_file))
            return ["--cookies", self.cookies_file]
        raise ValidationError("Неизвестный режим доступа к YouTube.")

    @property
    def summary(self) -> str:
        if not self.enabled or self.mode in {AUTH_AUTO, AUTH_NONE}:
            return "без авторизации"
        if self.mode == AUTH_BROWSER:
            profile = f", профиль {self.browser_profile}" if self.browser_profile else ""
            return f"cookies из {BROWSERS.get(self.browser, self.browser)}{profile}"
        return "файл cookies.txt"


def _local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))


def _app_data() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))


def browser_user_data_root(browser: str) -> Optional[Path]:
    local = _local_app_data()
    roaming = _app_data()
    roots = {
        "chrome": local / "Google" / "Chrome" / "User Data",
        "edge": local / "Microsoft" / "Edge" / "User Data",
        "firefox": roaming / "Mozilla" / "Firefox" / "Profiles",
        "brave": local / "BraveSoftware" / "Brave-Browser" / "User Data",
        "chromium": local / "Chromium" / "User Data",
        "opera": roaming / "Opera Software" / "Opera Stable",
        "vivaldi": local / "Vivaldi" / "User Data",
    }
    return roots.get(browser)


def find_browser(browser: str) -> Optional[Path]:
    local = _local_app_data()
    candidates = {
        "chrome": [local / "Google/Chrome/Application/chrome.exe"],
        "edge": [Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"],
        "firefox": [Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Mozilla Firefox/firefox.exe"],
        "brave": [local / "BraveSoftware/Brave-Browser/Application/brave.exe"],
        "chromium": [local / "Chromium/Application/chrome.exe"],
        "opera": [local / "Programs/Opera/opera.exe"],
        "vivaldi": [local / "Vivaldi/Application/vivaldi.exe"],
    }.get(browser, [])
    for path in candidates:
        if path.is_file():
            return path
    try:
        import winreg

        executable_names = {
            "chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe",
            "brave": "brave.exe", "chromium": "chromium.exe", "opera": "opera.exe", "vivaldi": "vivaldi.exe",
        }
        name = executable_names.get(browser)
        if name:
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}") as key:
                        value, _ = winreg.QueryValueEx(key, None)
                        path = Path(value)
                        if path.is_file():
                            return path
                except OSError:
                    continue
    except ImportError:
        pass
    return None


def browser_profile_exists(browser: str, profile: str) -> bool:
    root = browser_user_data_root(browser)
    if not root or not root.exists():
        return False
    if browser == "firefox":
        return any(path.is_dir() and (path.name == profile or path.name.endswith("." + profile)) for path in root.iterdir())
    if browser == "opera":
        return profile in {"Default", "Opera Stable"} and root.is_dir()
    return (root / profile).is_dir()


def validate_cookies_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValidationError("Файл cookies.txt не найден или пуст.")
    try:
        first_lines = path.read_text(encoding="utf-8", errors="strict").splitlines()[:5]
    except (OSError, UnicodeError) as exc:
        raise ValidationError("Файл cookies.txt недоступен для чтения.") from exc
    first_nonempty = next((line.strip() for line in first_lines if line.strip()), "")
    if not first_nonempty.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise ValidationError("Файл cookies.txt не похож на Netscape/Mozilla cookies.")
