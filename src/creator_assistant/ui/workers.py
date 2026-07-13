from __future__ import annotations

import traceback
import logging
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, Signal, Slot

from creator_assistant.domain.models import ProgressInfo
from creator_assistant.domain.errors import (
    AudioSeparatorOutputMissingError,
    AudioSeparatorRuntimeMissingError,
    DiskSpaceError,
    GpuOutOfMemoryError,
    JobCancelledError,
    ManualActionRequiredError,
    SystemMemoryError,
    YouTubeAuthenticationRequiredError,
    YouTubeCookiesUnavailableError,
    YouTubeMediaForbiddenError,
)
from creator_assistant.infrastructure.crash_logging import event


class FunctionWorker(QObject):
    finished = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()
    manual_action_required = Signal(object)
    runtime_install_required = Signal(object)
    authentication_required = Signal(object)
    cookies_unavailable = Signal(object)
    media_forbidden = Signal(object)
    waiting_for_disk_space = Signal(object)
    gpu_memory_required = Signal(object)
    system_memory_required = Signal(object)
    audio_output_missing = Signal(object)
    progress = Signal(object)

    def __init__(self, function: Callable[[Callable[[ProgressInfo], None]], Any]) -> None:
        super().__init__()
        self.function = function

    @Slot()
    def run(self) -> None:
        event(f"Worker run started: {self.objectName() or hex(id(self))}")
        try:
            result = self.function(self.progress.emit)
        except JobCancelledError:
            logging.getLogger("creator_assistant").info("Job cancelled by user.")
            self.cancelled.emit()
        except ManualActionRequiredError as exc:
            logging.getLogger("creator_assistant").info(
                "UVR manual action required. input=%s output=%s", exc.source, exc.expected_output
            )
            self.manual_action_required.emit(exc)
        except AudioSeparatorRuntimeMissingError as exc:
            self.runtime_install_required.emit(exc)
        except YouTubeAuthenticationRequiredError as exc:
            logging.getLogger("creator_assistant").info(
                "YouTube authentication required video_id=%s cookies_used=%s browser=%s",
                exc.video_id, exc.cookies_used, exc.browser or "none",
            )
            self.authentication_required.emit(exc)
        except YouTubeCookiesUnavailableError as exc:
            logging.getLogger("creator_assistant").warning(
                "YouTube browser cookies unavailable browser=%s reason=%s", exc.browser, exc.reason
            )
            self.cookies_unavailable.emit(exc)
        except YouTubeMediaForbiddenError as exc:
            logging.getLogger("creator_assistant").warning(
                "YouTube media 403 video_id=%s role=%s format=%s attempts=%s part=%s downloaded=%s",
                exc.video_id, exc.role, exc.format_id, exc.attempts, exc.part_path, exc.downloaded_bytes,
            )
            self.media_forbidden.emit(exc)
        except DiskSpaceError as exc:
            self.waiting_for_disk_space.emit(exc)
        except GpuOutOfMemoryError as exc:
            self.gpu_memory_required.emit(exc)
        except SystemMemoryError as exc:
            self.system_memory_required.emit(exc)
        except AudioSeparatorOutputMissingError as exc:
            self.audio_output_missing.emit(exc)
        except Exception as exc:
            details = traceback.format_exc()
            extra = getattr(exc, "details", "")
            if extra:
                details += "\n\nВывод внешней программы:\n" + str(extra)
            logging.getLogger("creator_assistant").error("background task failed\n%s", details)
            self.failed.emit(str(exc) or exc.__class__.__name__, details)
        else:
            self.finished.emit(result)
        finally:
            event(f"Worker run returned: {self.objectName() or hex(id(self))}")


class UiWorkerBridge(QObject):
    """Marshal worker signals to callbacks on the bridge's (UI) thread."""

    def __init__(
        self,
        callbacks: Dict[str, Callable[..., Any]],
        predicate: Callable[[], bool] = lambda: True,
        on_error: Optional[Callable[[BaseException, str], None]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.callbacks = callbacks
        self.predicate = predicate
        self.on_error = on_error

    def _dispatch(self, name: str, *args) -> None:
        if not self.predicate():
            event(f"Ignored stale worker signal: {name}")
            return
        callback = self.callbacks.get(name)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            details = traceback.format_exc()
            event(f"UI worker callback failed: {name}")
            logging.getLogger("creator_assistant").error("UI worker callback failed\n%s", details)
            if self.on_error:
                self.on_error(exc, details)

    @Slot(object)
    def finished(self, value) -> None:
        self._dispatch("finished", value)

    @Slot(str, str)
    def failed(self, message: str, details: str) -> None:
        self._dispatch("failed", message, details)

    @Slot()
    def cancelled(self) -> None:
        self._dispatch("cancelled")

    @Slot(object)
    def manual_action_required(self, value) -> None:
        self._dispatch("manual_action_required", value)

    @Slot(object)
    def runtime_install_required(self, value) -> None:
        self._dispatch("runtime_install_required", value)

    @Slot(object)
    def authentication_required(self, value) -> None:
        self._dispatch("authentication_required", value)

    @Slot(object)
    def cookies_unavailable(self, value) -> None:
        self._dispatch("cookies_unavailable", value)

    @Slot(object)
    def media_forbidden(self, value) -> None:
        self._dispatch("media_forbidden", value)

    @Slot(object)
    def waiting_for_disk_space(self, value) -> None:
        self._dispatch("waiting_for_disk_space", value)

    @Slot(object)
    def gpu_memory_required(self, value) -> None:
        self._dispatch("gpu_memory_required", value)

    @Slot(object)
    def system_memory_required(self, value) -> None:
        self._dispatch("system_memory_required", value)

    @Slot(object)
    def audio_output_missing(self, value) -> None:
        self._dispatch("audio_output_missing", value)

    @Slot(object)
    def progress(self, value) -> None:
        self._dispatch("progress", value)

    @Slot()
    def thread_finished(self) -> None:
        callback = self.callbacks.get("thread_finished")
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            details = traceback.format_exc()
            logging.getLogger("creator_assistant").error("Thread cleanup callback failed\n%s", details)
            if self.on_error:
                self.on_error(exc, details)
