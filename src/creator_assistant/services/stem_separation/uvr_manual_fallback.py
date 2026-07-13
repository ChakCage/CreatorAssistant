from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from creator_assistant.domain.errors import ManualActionRequiredError, StemSeparationUnavailableError, ValidationError
from creator_assistant.domain.job import CancellationToken

from .base import StemSeparatorBackend


class UvrManualFallbackBackend(StemSeparatorBackend):
    def __init__(self, launcher_path: Path) -> None:
        self.launcher_path = launcher_path

    def available(self) -> bool:
        return self.launcher_path.is_file()

    def separate(
        self,
        source: Path,
        expected_output: Path,
        cancellation: CancellationToken,
        on_message: Optional[Callable[[str], None]] = None,
    ) -> Path:
        if not self.available():
            raise StemSeparationUnavailableError("Ultimate Vocal Remover не найден.")
        try:
            source_parent = source.resolve().parent
            output_parent = expected_output.resolve().parent
        except OSError as exc:
            raise ValidationError("Не удалось проверить пути текущего UVR-задания.") from exc
        if source_parent != output_parent:
            raise ValidationError("UVR input и expected result должны принадлежать текущей папке «Материалы».")
        if on_message:
            on_message(f"Ручной режим UVR. Вход: {source}")
            on_message(f"Выход: {expected_output.parent}")
            on_message("Автоматическая обработка не запущена. Требуется действие пользователя.")
        cancellation.raise_if_cancelled()
        raise ManualActionRequiredError(source, expected_output, self.launcher_path)
