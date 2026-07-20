from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def startup_shortcut_path() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Creator Assistant Publishing Agent.lnk"


def set_publishing_startup(enabled: bool, executable: str | None = None) -> Path:
    path = startup_shortcut_path()
    if not enabled:
        if path.exists(): path.unlink()
        return path
    if os.name != "nt":
        raise RuntimeError("Windows startup shortcut is supported only on Windows")
    target = str(Path(executable or sys.executable).resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = lambda value: value.replace("'", "''")
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + escaped(str(path)) + "');"
        "$s.TargetPath='" + escaped(target) + "';$s.Arguments='--publishing-agent';"
        "$s.WorkingDirectory='" + escaped(str(Path(target).parent)) + "';$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return path
