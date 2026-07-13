from __future__ import annotations

import logging
import time
from copy import deepcopy
from typing import Any, Dict, Iterable, Set

from PySide6.QtCore import QObject, Signal

from creator_assistant.infrastructure.settings_store import SettingsStore


VALID_PROXY_HEIGHTS = {480, 720, 1080}


class SettingsService(QObject):
    """Own the live settings snapshot and publish each committed change once."""

    settings_changed = Signal(object)

    def __init__(
        self,
        store: SettingsStore,
        initial_settings: Dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        super().__init__()
        self.store = store
        self.settings: Dict[str, Any] = deepcopy(initial_settings)
        self._persisted_settings: Dict[str, Any] = deepcopy(initial_settings)
        self.logger = logger
        self.last_timing: Dict[str, Any] = {}

    def changed_top_level_keys(self, settings: Dict[str, Any]) -> Set[str]:
        keys: Iterable[str] = set(self._persisted_settings) | set(settings)
        return {
            key
            for key in keys
            if self._persisted_settings.get(key) != settings.get(key)
        }

    def persisted_snapshot(self) -> Dict[str, Any]:
        return deepcopy(self._persisted_settings)

    def update(self, settings: Dict[str, Any], started_at: float | None = None) -> Dict[str, Any]:
        """Update memory, atomically write JSON, then synchronously notify Qt receivers."""
        started = started_at if started_at is not None else time.monotonic()
        snapshot = deepcopy(settings)
        try:
            proxy_height = int(snapshot.get("reaper_proxy_height", 720))
        except (TypeError, ValueError) as exc:
            raise ValueError("reaper_proxy_height must be 480, 720 or 1080") from exc
        if proxy_height not in VALID_PROXY_HEIGHTS:
            raise ValueError("reaper_proxy_height must be 480, 720 or 1080")
        snapshot["reaper_proxy_height"] = proxy_height

        self.settings = snapshot
        memory_updated = time.monotonic()
        self.store.save(self.settings)
        json_written = time.monotonic()
        self._persisted_settings = deepcopy(self.settings)
        self.last_timing = {
            "save_clicked_at": started,
            "memory_updated_at": memory_updated,
            "json_written_at": json_written,
            "signal_emitted_at": None,
            "ui_refreshed_at": None,
            "ui_latency_ms": None,
        }
        self.logger.debug(
            "settings timing: clicked=%.6f memory=%.6f json=%.6f",
            started,
            memory_updated,
            json_written,
        )
        self.settings_changed.emit(deepcopy(self.settings))
        signal_emitted = time.monotonic()
        self.last_timing["signal_emitted_at"] = signal_emitted
        self.logger.debug("settings timing: signal emitted=%.6f", signal_emitted)
        return self.settings

    def mark_ui_refreshed(self) -> float | None:
        if not self.last_timing:
            return None
        refreshed = time.monotonic()
        started = float(self.last_timing["save_clicked_at"])
        latency_ms = (refreshed - started) * 1000.0
        self.last_timing["ui_refreshed_at"] = refreshed
        self.last_timing["ui_latency_ms"] = latency_ms
        self.logger.info(
            "settings timing: ProjectPrepTab refreshed=%.6f latency_ms=%.3f",
            refreshed,
            latency_ms,
        )
        return latency_ms

    def mark_current_as_persisted(self) -> None:
        """Synchronize the baseline after an explicit dependency discovery mutates settings."""
        self._persisted_settings = deepcopy(self.settings)
