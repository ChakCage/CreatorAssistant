from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Optional

from creator_assistant.domain.errors import StemSeparationUnavailableError, ValidationError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessRunner

from .base import StemSeparatorBackend


class UvrDirectBackend(StemSeparatorBackend):
    """Прямой backend для явно установленного совместимого CLI UVR.

    UVR GUI 5.6.1 не публикует стабильный CLI. Поэтому backend не пытается
    менять data.pkl или управлять мышью: он активируется только для отдельного
    uvr-cli, путь к которому можно передать через CREATOR_ASSISTANT_UVR_CLI.
    """

    def __init__(self, runner: ProcessRunner, model_path: Path, use_gpu: bool = True, cli_path: str = "") -> None:
        self.runner = runner
        self.model_path = model_path
        self.use_gpu = use_gpu
        self.cli_path = cli_path or os.environ.get("CREATOR_ASSISTANT_UVR_CLI", "")

    def available(self) -> bool:
        resolved = shutil.which(self.cli_path) if self.cli_path else None
        return self.model_path.is_file() and bool((self.cli_path and Path(self.cli_path).is_file()) or resolved)

    def separate(
        self,
        source: Path,
        expected_output: Path,
        cancellation: CancellationToken,
        on_message: Optional[Callable[[str], None]] = None,
    ) -> Path:
        if not self.available():
            raise StemSeparationUnavailableError("Совместимый прямой CLI UVR не найден.")
        if source.resolve().parent != expected_output.resolve().parent:
            raise ValidationError("UVR input не принадлежит текущей папке проекта.")
        self.runner.logger.info("UVR input: %s", source)
        self.runner.logger.info("UVR output directory: %s", expected_output.parent)
        self.runner.logger.info("UVR expected result: %s", expected_output)
        self.runner.logger.info("UVR model: UVR-MDX-NET Inst HQ 3 (%s)", self.model_path)
        self.runner.logger.info("Backend: UvrDirectBackend")
        if on_message:
            on_message("UVR: MDX-Net / UVR-MDX-NET Inst HQ 3 / Instrumental Only / FLAC")
        command = [
            self.cli_path,
            "--input",
            str(source),
            "--output",
            str(expected_output),
            "--model",
            str(self.model_path),
            "--format",
            "FLAC",
            "--instrumental-only",
        ]
        command.append("--gpu" if self.use_gpu else "--cpu")
        self.runner.run(command, cancellation=cancellation, on_line=on_message, no_output_timeout=90)
        if not expected_output.is_file() or expected_output.stat().st_size == 0:
            raise ValidationError("UVR завершился без корректного Instrumental FLAC.")
        return expected_output
