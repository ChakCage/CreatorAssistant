from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict
from urllib.request import Request, urlopen

from creator_assistant.domain.job import CancellationToken

from .process_runner import ProcessRunner


class YtDlpUpdater:
    def __init__(self, runner: ProcessRunner) -> None:
        self.runner = runner

    @staticmethod
    def check_due(settings: Dict[str, Any]) -> bool:
        raw = settings.get("last_update_check", "")
        try:
            last = dt.date.fromisoformat(raw)
        except (TypeError, ValueError):
            return True
        return (dt.date.today() - last).days >= 7

    def update(self, executable: str, cancellation: CancellationToken) -> str:
        path = Path(executable)
        python_next_to_scripts = path.parent.parent / "python.exe"
        if path.parent.name.casefold() == "scripts" and python_next_to_scripts.is_file():
            result = self.runner.run(
                [str(python_next_to_scripts), "-m", "pip", "install", "--upgrade", "yt-dlp"],
                cancellation=cancellation,
            )
        elif path.suffix.casefold() == ".exe":
            result = self.runner.run([str(path), "-U"], cancellation=cancellation)
        else:
            result = self.runner.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                cancellation=cancellation,
            )
        return result.output

    @staticmethod
    def latest_version() -> str:
        request = Request(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "CreatorAssistant/0.1"},
        )
        with urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str(data.get("tag_name") or data.get("name") or "").removeprefix("yt-dlp ").strip()
