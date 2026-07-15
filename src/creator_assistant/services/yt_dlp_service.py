from __future__ import annotations

import re
import time
import uuid
import random
import logging
from pathlib import Path
from typing import Callable, Optional

from creator_assistant.domain.errors import ProcessExecutionError, YouTubeMediaForbiddenError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import ProgressInfo
from creator_assistant.domain.youtube_auth import YtDlpAuthContext
from creator_assistant.domain.progress import format_bytes, format_duration
from creator_assistant.infrastructure.process_runner import ProcessRunner
from creator_assistant.infrastructure.settings_store import local_data_root
from creator_assistant.services.metadata_service import youtube_video_id
from creator_assistant.services.yt_dlp_errors import classify_youtube_failure, classify_yt_dlp_error


ProgressCallback = Callable[[ProgressInfo], None]
PROGRESS_MARKER = "CREATOR_PROGRESS|"


class YtDlpService:
    MEDIA_TRANSIENT_MARKERS = (
        "read timed out",
        "connection reset",
        "connection aborted",
        "remote end closed connection",
        "temporarily unavailable",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
    )

    def __init__(self, runner: ProcessRunner, executable: str, ffmpeg_path: str = "", result_root: Optional[Path] = None, auth: Optional[YtDlpAuthContext] = None) -> None:
        self.runner = runner
        self.executable = executable
        self.ffmpeg_path = ffmpeg_path
        self.result_root = result_root or (local_data_root() / "jobs" / "yt-dlp-results")
        self.auth = auth or YtDlpAuthContext()

    def download(
        self,
        url: str,
        selector: str,
        output_template: Path,
        cancellation: CancellationToken,
        stage: str,
        merge_container: Optional[str] = None,
        on_progress: Optional[ProgressCallback] = None,
        role: str = "media",
        alternative_selectors: Optional[list[str]] = None,
        environment: Optional[dict[str, str]] = None,
        cwd: Optional[Path] = None,
    ) -> Path:
        output_template.parent.mkdir(parents=True, exist_ok=True)
        before = self._snapshot(output_template.parent)
        started_ns = time.time_ns()
        self.result_root.mkdir(parents=True, exist_ok=True)
        result_file = self.result_root / f"{uuid.uuid4().hex}.txt"
        base_command = [
            self.executable,
            "--ignore-config",
            "--verbose",
            "--no-playlist",
            "--no-write-subs",
            "--no-write-auto-subs",
            "--no-write-comments",
            "--no-write-info-json",
            "--no-write-thumbnail",
            "--continue",
            "--retries", "3",
            "--fragment-retries", "1",
            "--retry-sleep", "http:linear=1::2",
            "--encoding",
            "utf-8",
            "--newline",
            "--progress-template",
            "download:CREATOR_PROGRESS|%(progress._percent_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress.fragment_index)s|%(progress.fragment_count)s|%(info.vcodec)s|%(info.acodec)s",
            "--print-to-file",
            "after_move:%(filepath)s",
            str(result_file),
        ]
        if self.ffmpeg_path:
            base_command.extend(["--ffmpeg-location", str(Path(self.ffmpeg_path).parent)])
        if merge_container:
            base_command.extend(["--merge-output-format", merge_container])
        base_command.extend(self.auth.arguments())
        multi_stream = "+" in selector
        last_progress: Optional[ProgressInfo] = None

        def parse(line: str) -> None:
            nonlocal last_progress
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
            marker_index = clean.find(PROGRESS_MARKER)
            if marker_index >= 0 and on_progress:
                info = self.parse_progress_line(clean[marker_index:], stage, multi_stream)
                if info:
                    if last_progress and info.percent is not None and last_progress.percent is not None:
                        info.percent = max(info.percent, last_progress.percent)
                    last_progress = info
                    on_progress(info)

        try:
            alternatives = [value for value in (alternative_selectors or []) if value and value != selector]
            selectors = [selector, selector, selector] + alternatives[:1]
            selectors = selectors[:4]
            delays = [0.0, random.uniform(2.0, 3.0), 5.0, random.uniform(8.0, 10.0)]
            result = None
            for attempt, current_selector in enumerate(selectors, 1):
                cancellation.raise_if_cancelled()
                if attempt > 1:
                    delay = delays[attempt - 1]
                    if on_progress:
                        on_progress(ProgressInfo(stage, f"{role} — восстановление загрузки. Повторная попытка через {int(round(delay))} с; попытка {attempt} из {len(selectors)}", last_progress.percent if last_progress else None))
                    cancellation.wait(delay)
                command = list(base_command)
                command.extend(["-f", current_selector, "-o", str(output_template), url])
                getattr(self.runner, "logger", logging.getLogger("creator_assistant")).info(
                    "YouTube operation=%s auth_mode=%s cookies_argument_present=%s po_token_provider=%s "
                    "player_client=%s attempt=%s/%s video_id=%s selected_format=%s part_preserved=true",
                    role,
                    self.auth.effective_mode,
                    "yes" if self.auth.arguments(validate=False) else "no",
                    "unknown",
                    "unknown",
                    attempt,
                    len(selectors),
                    youtube_video_id(url),
                    current_selector,
                )
                try:
                    result = self.runner.run(
                        command, cancellation=cancellation, on_line=parse,
                        environment=environment, cwd=cwd,
                    )
                    selector = current_selector
                    break
                except ProcessExecutionError as exc:
                    lower_details = str(exc.details).casefold()
                    is_media_403 = "http error 403" in lower_details or "403: forbidden" in lower_details
                    is_media_transient = any(marker in lower_details for marker in self.MEDIA_TRANSIENT_MARKERS)
                    failure_category = classify_youtube_failure(str(exc.details), self.auth, media_operation=True)
                    getattr(self.runner, "logger", logging.getLogger("creator_assistant")).info(
                        "YouTube operation=%s category=%s auth_mode=%s cookies_argument_present=%s "
                        "po_token_provider=%s player_client=%s attempt=%s/%s selected_format=%s",
                        role,
                        failure_category.value,
                        self.auth.effective_mode,
                        "yes" if self.auth.arguments(validate=False) else "no",
                        self._extract_detail(exc.details, "PO Token Providers"),
                        self._extract_detail(exc.details, "player client"),
                        attempt,
                        len(selectors),
                        current_selector,
                    )
                    if not is_media_403 and not is_media_transient:
                        classified = classify_yt_dlp_error(exc, url, youtube_video_id(url), self.auth)
                        if classified is not exc:
                            raise classified
                        raise
                    part_path = self.find_partial_file(output_template)
                    stat_size = part_path.stat().st_size if part_path and part_path.is_file() else 0
                    downloaded = max(stat_size, last_progress.downloaded_bytes or 0) if last_progress else stat_size
                    if attempt >= len(selectors) and is_media_403:
                        raise YouTubeMediaForbiddenError(
                            youtube_video_id(url), url, role, current_selector,
                            downloaded_bytes=downloaded,
                            total_bytes=last_progress.total_bytes if last_progress else 0,
                            percent=last_progress.percent if last_progress else None,
                            part_path=part_path,
                            auth_context=self.auth.summary,
                            player_client=self._extract_detail(exc.details, "player client"),
                            po_token_provider=self._extract_detail(exc.details, "PO Token Providers"),
                            stderr=str(exc.details),
                            attempts=attempt,
                        )
                    if attempt >= len(selectors):
                        raise
                    if on_progress:
                        on_progress(ProgressInfo(stage, "YouTube прервал передачу видео. Обновляю ссылку на медиапоток и продолжаю загрузку…", last_progress.percent if last_progress else None, downloaded=format_bytes(downloaded)))
            assert result is not None
            produced = self._read_result_file(result_file)
            if produced and self._usable_result(produced, output_template.parent):
                if on_progress:
                    on_progress(ProgressInfo(stage, "Загрузка и объединение завершены", 100.0, substage_name="Готово"))
                return produced
            fallback = self.find_created_file(output_template, before, started_ns)
            if fallback:
                if on_progress:
                    on_progress(ProgressInfo(stage, "Готовый файл найден проверкой папки", 100.0, substage_name="Готово"))
                return fallback
            raise ProcessExecutionError(
                "Видео было скачано, но приложению не удалось определить созданный файл.",
                result.output[-4000:],
            )
        finally:
            try:
                result_file.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def find_partial_file(output_template: Path) -> Optional[Path]:
        expected_base = output_template.name.replace(".%(ext)s", "").replace("%(ext)s", "").rstrip(".").casefold()
        candidates = []
        try:
            for path in output_template.parent.iterdir():
                if path.is_file() and (path.name.casefold().endswith((".part", ".ytdl")) or ".part-frag" in path.name.casefold()):
                    if path.name.casefold().startswith(expected_base):
                        candidates.append(path)
        except OSError:
            return None
        return max(candidates, key=lambda item: item.stat().st_size, default=None)

    @staticmethod
    def _extract_detail(details: str, label: str) -> str:
        match = re.search(rf"{re.escape(label)}\s*[:=]\s*([^\r\n]+)", str(details), re.IGNORECASE)
        return match.group(1).strip() if match else "unknown"

    def version(self) -> str:
        return self.runner.run([self.executable, "--version"], timeout=15).output.strip().splitlines()[0]

    @staticmethod
    def _read_result_file(path: Path) -> Optional[Path]:
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        for line in reversed(lines):
            value = line.strip().strip('"')
            if value:
                return Path(value)
        return None

    @staticmethod
    def _usable_result(path: Path, expected_parent: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0 and path.parent.resolve() == expected_parent.resolve()
        except OSError:
            return False

    @staticmethod
    def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        try:
            for path in directory.iterdir():
                if path.is_file():
                    stat = path.stat()
                    snapshot[str(path.resolve()).casefold()] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            pass
        return snapshot

    @classmethod
    def find_created_file(
        cls,
        output_template: Path,
        before: Optional[dict[str, tuple[int, int]]] = None,
        started_ns: int = 0,
    ) -> Optional[Path]:
        before = before or {}
        expected_base = output_template.name.replace(".%(ext)s", "").replace("%(ext)s", "").rstrip(".")
        ignored_suffixes = (".part", ".ytdl", ".tmp")
        candidates = []
        try:
            paths = list(output_template.parent.iterdir())
        except OSError:
            return None
        for path in paths:
            try:
                if not path.is_file() or path.stat().st_size <= 0:
                    continue
                lower_name = path.name.casefold()
                if lower_name.endswith(ignored_suffixes) or ".part-frag" in lower_name:
                    continue
                if not path.stem.casefold().startswith(expected_base.casefold()):
                    continue
                # Separate yt-dlp streams such as name.f401.webm are not final results.
                if re.search(r"\.f\d+(?:\.|$)", lower_name):
                    continue
                stat = path.stat()
                old = before.get(str(path.resolve()).casefold())
                changed = old is None or old != (stat.st_size, stat.st_mtime_ns) or stat.st_mtime_ns >= started_ns
                exact = path.stem.casefold() == expected_base.casefold()
                candidates.append((exact, changed, stat.st_mtime_ns, stat.st_size, path))
            except OSError:
                continue
        return max(candidates, default=None, key=lambda item: item[:4])[-1] if candidates else None

    @staticmethod
    def parse_progress_line(line: str, stage: str, multi_stream: bool = False) -> Optional[ProgressInfo]:
        marker_index = line.find(PROGRESS_MARKER)
        if marker_index < 0:
            return None
        parts = line[marker_index + len(PROGRESS_MARKER) :].split("|")
        parts += [""] * (10 - len(parts))

        def number(value: str, integer: bool = False):
            clean = value.strip().replace("%", "")
            if not clean or clean.casefold() in {"na", "n/a", "none", "null", "unknown"}:
                return None
            try:
                parsed = float(clean)
                return int(parsed) if integer else parsed
            except ValueError:
                return None

        percent = number(parts[0])
        downloaded = number(parts[1], True)
        total = number(parts[2], True) or number(parts[3], True)
        speed = number(parts[4])
        eta = number(parts[5], True)
        fragment_index = number(parts[6], True)
        fragment_count = number(parts[7], True)
        vcodec = parts[8].strip().casefold()
        acodec = parts[9].strip().casefold()
        substage = "Скачивание файла"
        adjusted_percent = percent
        if multi_stream and vcodec and vcodec != "none" and acodec == "none":
            substage = "Скачивание видеопотока"
            adjusted_percent = None if percent is None else percent * 0.70
        elif multi_stream and acodec and acodec != "none" and (not vcodec or vcodec == "none"):
            substage = "Скачивание аудиопотока"
            adjusted_percent = None if percent is None else 70.0 + percent * 0.20
        if fragment_index and fragment_count:
            substage += f" — фрагмент {fragment_index} из {fragment_count}"
        downloaded_text = format_bytes(downloaded)
        total_text = format_bytes(total) if total is not None else "Размер пока неизвестен"
        speed_text = (format_bytes(speed) + "/с") if speed is not None else ""
        eta_text = format_duration(eta) if eta is not None else "Оставшееся время рассчитывается"
        return ProgressInfo(
            stage=stage,
            message=substage,
            percent=adjusted_percent,
            speed=speed_text,
            downloaded=downloaded_text,
            total=total_text,
            eta=eta_text,
            downloaded_bytes=downloaded,
            total_bytes=total,
            speed_bytes_per_second=speed,
            eta_seconds=eta,
            substage_name=substage,
        )
