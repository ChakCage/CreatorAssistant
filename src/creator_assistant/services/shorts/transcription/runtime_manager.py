from __future__ import annotations

import os
from pathlib import Path

from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.infrastructure.settings_store import local_data_root


class WhisperRuntimeManager:
    """Owns an isolated optional runtime; never installs ML packages into system Python."""

    def __init__(self, runner: ProcessRunner) -> None:
        self.runner = runner
        self.root = local_data_root() / "runtimes" / "whisper"
        self.models = local_data_root() / "models" / "whisper"

    @property
    def python(self) -> Path:
        return self.root / "Scripts" / "python.exe" if os.name == "nt" else self.root / "bin" / "python"

    def is_ready(self) -> bool:
        return self.python.is_file()

    def prepare_directories(self) -> None:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.models.mkdir(parents=True, exist_ok=True)

    def install_commands(self, bootstrap_python: str) -> list[list[str]]:
        self.prepare_directories()
        return [
            [bootstrap_python, "-m", "venv", str(self.root)],
            [str(self.python), "-m", "pip", "install", "--upgrade", "pip"],
            [str(self.python), "-m", "pip", "install", "openai-whisper"],
        ]
