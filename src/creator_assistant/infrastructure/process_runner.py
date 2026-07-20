from __future__ import annotations

import logging
import locale
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from creator_assistant.domain.errors import JobCancelledError, ProcessExecutionError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.services.storage_service import classify_resource_failure


LineCallback = Callable[[str], None]


@dataclass(frozen=True)
class ProcessResult:
    command: List[str]
    returncode: int
    output: str
    stdout: str = ""
    stderr: str = ""
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""


class ProcessRunner:
    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger("creator_assistant")
        self._lock = threading.Lock()
        self._active: Optional[subprocess.Popen] = None

    @property
    def active_pid(self) -> Optional[int]:
        with self._lock:
            return self._active.pid if self._active else None

    def run(
        self,
        command: Iterable[str],
        cancellation: Optional[CancellationToken] = None,
        on_line: Optional[LineCallback] = None,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
        no_output_timeout: Optional[float] = None,
        check: bool = True,
        environment: Optional[Dict[str, str]] = None,
    ) -> ProcessResult:
        args = [str(arg) for arg in command]
        safe_args = self._redact(args)
        self.logger.info("command=%s", subprocess.list2cmdline(safe_args))
        startupinfo, creationflags = self._hidden_process_options()
        process_environment = os.environ.copy()
        process_environment.setdefault("PYTHONUTF8", "1")
        process_environment.setdefault("PYTHONIOENCODING", "utf-8")
        if environment:
            process_environment.update({str(key): str(value) for key, value in environment.items()})
        try:
            process = subprocess.Popen(
                args,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=False,
                env=process_environment,
                shell=False,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except OSError as exc:
            classified = classify_resource_failure(exc)
            if classified:
                raise classified from exc
            raise
        with self._lock:
            self._active = process
        lines: List[str] = []
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        output_queue: "queue.Queue[tuple[str, Optional[bytes]]]" = queue.Queue()
        stdout_chunks: List[bytes] = []
        stderr_chunks: List[bytes] = []

        def reader(name: str, stream) -> None:
            for line in iter(stream.readline, b""):
                output_queue.put((name, line.rstrip(b"\r\n")))
            output_queue.put((name, None))

        assert process.stdout is not None and process.stderr is not None
        threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True).start()
        threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True).start()
        started = time.monotonic()
        last_activity = started
        streams_done = 0
        try:
            while process.poll() is None or streams_done < 2:
                if cancellation and cancellation.is_cancelled:
                    self._terminate_tree(process)
                    raise JobCancelledError("Операция отменена пользователем.")
                if timeout and time.monotonic() - started > timeout:
                    self._terminate_tree(process)
                    name = Path(args[0]).name if args else "unknown"
                    raise ProcessExecutionError(
                        f"Процесс {name} превысил timeout {timeout:g} сек.",
                        details="stderr:\n" + "\n".join(stderr_lines)[-8000:],
                    )
                if no_output_timeout and time.monotonic() - last_activity > no_output_timeout:
                    self._terminate_tree(process)
                    name = Path(args[0]).name if args else "unknown"
                    raise ProcessExecutionError(
                        f"Процесс {name} запущен, но обработка не началась или перестала сообщать о прогрессе.",
                        details="stderr:\n" + "\n".join(stderr_lines)[-8000:],
                    )
                try:
                    stream_name, item = output_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    streams_done += 1
                    continue
                decoded = self._decode_bytes(item, stream_name)
                last_activity = time.monotonic()
                lines.append(decoded)
                if stream_name == "stdout":
                    stdout_chunks.append(item)
                    stdout_lines.append(decoded)
                else:
                    stderr_chunks.append(item)
                    stderr_lines.append(decoded)
                if on_line:
                    on_line(decoded)
            returncode = process.wait()
        finally:
            with self._lock:
                if self._active is process:
                    self._active = None
        output = "\n".join(lines)
        self.logger.info("returncode=%s output=%s", returncode, output[-12000:])
        result = ProcessResult(
            args,
            returncode,
            output,
            "\n".join(stdout_lines),
            "\n".join(stderr_lines),
            b"\n".join(stdout_chunks),
            b"\n".join(stderr_chunks),
        )
        if check and returncode != 0:
            classified = classify_resource_failure(output)
            if classified:
                raise classified
            raise ProcessExecutionError(
                f"Процесс {Path(args[0]).name if args else 'unknown'} завершился с кодом {returncode}.",
                details=("stderr:\n" + result.stderr + "\n\nstdout:\n" + result.stdout)[-8000:],
            )
        return result

    def _decode_bytes(self, value: bytes, stream_name: str) -> str:
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            fallback = locale.getpreferredencoding(False) or "cp1251"
            try:
                decoded = value.decode(fallback, errors="strict")
            except (UnicodeDecodeError, LookupError):
                decoded = value.decode("utf-8", errors="backslashreplace")
            self.logger.warning(
                "Не-UTF-8 вывод %s; кодировка fallback=%s; raw=%s",
                stream_name,
                fallback,
                value[:512].hex(),
            )
            return decoded

    def cancel_active(self) -> None:
        with self._lock:
            process = self._active
        if process:
            self._terminate_tree(process)

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                startupinfo, creationflags = ProcessRunner._hidden_process_options()
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    shell=False,
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                )
            else:
                process.terminate()
        except OSError:
            process.kill()

    @staticmethod
    def _redact(args: List[str]) -> List[str]:
        redacted: List[str] = []
        hide_next = False
        for arg in args:
            lower = arg.lower()
            if hide_next:
                redacted.append("<скрыто>")
                hide_next = False
            elif lower in ("--cookies", "--cookies-from-browser", "--username", "--password"):
                redacted.append(arg)
                hide_next = True
            else:
                redacted.append(arg)
        return redacted

    @staticmethod
    def _hidden_process_options():
        """Return Windows flags that keep every background CLI process invisible."""
        if os.name != "nt":
            return None, 0
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return startupinfo, creationflags
