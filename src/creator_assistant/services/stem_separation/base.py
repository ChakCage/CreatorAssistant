from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from creator_assistant.domain.job import CancellationToken


class StemSeparatorBackend(ABC):
    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def separate(
        self,
        source: Path,
        expected_output: Path,
        cancellation: CancellationToken,
        on_message: Optional[Callable[[str], None]] = None,
    ) -> Path:
        raise NotImplementedError
