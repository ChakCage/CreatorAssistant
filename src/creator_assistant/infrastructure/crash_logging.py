from __future__ import annotations

import datetime as dt
import faulthandler
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TextIO

from creator_assistant import __version__
from creator_assistant.infrastructure.settings_store import local_data_root


_lock = threading.RLock()
_stream: Optional[TextIO] = None
_previous_sys_hook = sys.excepthook
_previous_thread_hook = getattr(threading, "excepthook", None)
_context: Dict[str, Any] = {
    "job_id": "",
    "generation_id": 0,
    "video_id": "",
    "project_path": "",
    "job_state": "IDLE",
    "metadata_worker": "absent",
    "project_worker": "absent",
    "last_ui_action": "startup",
}


def crash_log_path() -> Path:
    return local_data_root() / "logs" / "crash.log"


def _write(kind: str, message: str, *, exception: str = "") -> None:
    with _lock:
        if _stream is None:
            return
        record = {
            "timestamp": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "kind": kind,
            "version": __version__,
            "pid": os.getpid(),
            **_context,
            "message": message,
        }
        if exception:
            record["traceback"] = exception
        _stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        _stream.flush()
        try:
            os.fsync(_stream.fileno())
        except OSError:
            pass


def initialize_early() -> Path:
    """Install native/Python crash capture before QApplication is constructed."""
    global _stream
    with _lock:
        if _stream is not None:
            return crash_log_path()
        path = crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _stream = path.open("a", encoding="utf-8", buffering=1)
        faulthandler.enable(_stream, all_threads=True)

        def sys_hook(exc_type, exc_value, exc_tb):
            details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            _write("UNHANDLED_PYTHON", str(exc_value), exception=details)
            if _previous_sys_hook:
                _previous_sys_hook(exc_type, exc_value, exc_tb)

        def thread_hook(args):
            details = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            _write("UNHANDLED_THREAD", f"thread={args.thread.name}: {args.exc_value}", exception=details)
            if _previous_thread_hook:
                _previous_thread_hook(args)

        sys.excepthook = sys_hook
        if hasattr(threading, "excepthook"):
            threading.excepthook = thread_hook
        _write("STARTUP", "Early crash logging initialized")
        return path


def install_qt_message_handler() -> None:
    from PySide6.QtCore import qInstallMessageHandler

    def handler(message_type, context, message):
        location = ""
        if context is not None:
            location = f" file={getattr(context, 'file', '')} line={getattr(context, 'line', 0)} function={getattr(context, 'function', '')}"
        _write("QT_MESSAGE", f"type={message_type!s} {message}{location}")

    qInstallMessageHandler(handler)
    _write("QT_HANDLER", "Qt message handler installed")


def update_context(**values: Any) -> None:
    with _lock:
        _context.update(values)


def event(message: str, **values: Any) -> None:
    if values:
        update_context(**values)
    _write("EVENT", message)


def exception(message: str, exc: BaseException) -> str:
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _write("CAUGHT_EXCEPTION", message, exception=details)
    return details


def safe_call(action: str, callback: Callable[..., Any], on_error: Optional[Callable[[BaseException, str], None]] = None):
    def invoke(*args, **kwargs):
        event(f"UI action started: {action}", last_ui_action=action)
        try:
            return callback(*args, **kwargs)
        except Exception as exc:
            details = exception(f"UI action failed: {action}", exc)
            if on_error:
                on_error(exc, details)
            return None
    return invoke
