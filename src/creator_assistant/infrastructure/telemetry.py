from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TelemetryEvent:
    name: str
    properties: dict[str, str | int | float | bool]


class TelemetrySink(Protocol):
    def capture(self, event: TelemetryEvent) -> None: ...


class DisabledTelemetrySink:
    """Stage 6A default: no network traffic and no automatic submission."""

    def capture(self, event: TelemetryEvent) -> None:
        return
