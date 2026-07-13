from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.shorts.models import Transcript


@dataclass(frozen=True)
class TranscriptionCapabilities:
    name: str
    available: bool
    version: str = ""
    executable: str = ""
    models: List[str] = field(default_factory=list)
    cuda: bool = False
    word_timestamps: bool = False
    details: str = ""


class TranscriptionBackend(ABC):
    @abstractmethod
    def capabilities(self) -> TranscriptionCapabilities:
        raise NotImplementedError

    @abstractmethod
    def transcribe(
        self,
        audio: Path,
        output_dir: Path,
        cancellation: CancellationToken,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> Transcript:
        raise NotImplementedError
