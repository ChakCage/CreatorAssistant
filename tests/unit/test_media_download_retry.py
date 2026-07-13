import logging
from pathlib import Path

import pytest

from creator_assistant.domain.errors import JobCancelledError, ProcessExecutionError, YouTubeMediaForbiddenError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import FormatPlan, VideoFormat
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.project_service import ProjectService
from creator_assistant.services.yt_dlp_service import YtDlpService


URL = "https://www.youtube.com/watch?v=abcdefghijk"


class RetryRunner:
    def __init__(self, directory: Path, failures: int):
        self.directory = directory
        self.failures = failures
        self.calls = []
        self.logger = logging.getLogger("media-retry-test")
        self.part = directory / "Video [MAX 1080p].f299.mp4.part"
        self.final = directory / "Video [MAX 1080p].mp4"

    def run(self, command, **kwargs):
        command = list(command)
        self.calls.append(command)
        callback = kwargs.get("on_line")
        if callback:
            callback("CREATOR_PROGRESS|  7.7%|20600000|267000000|NA|1000000|240|NA|NA|avc1|none")
        if len(self.calls) <= self.failures:
            self.part.write_bytes(b"partial bytes must survive")
            raise ProcessExecutionError("failed", "ERROR: unable to download video data: HTTP Error 403: Forbidden\nnull")
        self.final.write_bytes(b"complete media")
        result_path = Path(command[command.index("--print-to-file") + 2])
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(str(self.final), encoding="utf-8")
        return ProcessResult(command, 0, "ok", "ok", "")


class TimeoutThenSuccessRunner(RetryRunner):
    def run(self, command, **kwargs):
        if not self.calls:
            original_failures = self.failures
            self.failures = 1
            try:
                return super().run(command, **kwargs)
            except ProcessExecutionError as exc:
                exc.details = "HTTPSConnectionPool: Read timed out. Giving up after 1 retries"
                raise
            finally:
                self.failures = original_failures
        return super().run(command, **kwargs)


def service_and_token(tmp_path: Path, failures: int):
    runner = RetryRunner(tmp_path, failures)
    service = YtDlpService(runner, "yt-dlp.exe", result_root=tmp_path / "results")
    token = CancellationToken()
    delays = []
    token.wait = lambda seconds: delays.append(seconds)
    return service, runner, token, delays


def test_first_403_preserves_part_refreshes_extractor_and_continues_same_job(tmp_path: Path):
    service, runner, token, delays = service_and_token(tmp_path, 1)
    events = []
    template = tmp_path / "Video [MAX 1080p].%(ext)s"
    result = service.download(URL, "299+140", template, token, "maximum", on_progress=events.append, role="MAX_VIDEO")
    assert result == runner.final
    assert runner.part.is_file() and runner.part.read_bytes().startswith(b"partial")
    assert len(runner.calls) == 2
    assert all(call[call.index("-o") + 1] == str(template) for call in runner.calls)
    assert all(call[call.index("-f") + 1] == "299+140" for call in runner.calls)
    assert "--continue" in runner.calls[1]
    assert runner.calls[1][runner.calls[1].index("--retries") + 1] == "3"
    assert len(delays) == 1 and 2 <= delays[0] <= 3
    assert any("Обновляю ссылку" in event.message for event in events)
    percentages = [event.percent for event in events if event.percent is not None]
    assert percentages == sorted(percentages)


def test_repeated_403_uses_dynamic_same_quality_alternative(tmp_path: Path):
    service, runner, token, _delays = service_and_token(tmp_path, 3)
    template = tmp_path / "Video [MAX 1080p].%(ext)s"
    service.download(
        URL, "299+140", template, token, "maximum", role="MAX_VIDEO",
        alternative_selectors=["303+140"],
    )
    selectors = [call[call.index("-f") + 1] for call in runner.calls]
    assert selectors == ["299+140", "299+140", "299+140", "303+140"]
    assert runner.part.is_file()


def test_retry_limit_returns_typed_403_with_partial_details(tmp_path: Path):
    service, runner, token, _delays = service_and_token(tmp_path, 9)
    with pytest.raises(YouTubeMediaForbiddenError) as caught:
        service.download(URL, "299+140", tmp_path / "Video [MAX 1080p].%(ext)s", token, "maximum", role="MAX_VIDEO")
    error = caught.value
    assert error.attempts == 3
    assert error.part_path == runner.part
    assert error.downloaded_bytes >= runner.part.stat().st_size
    assert "null" in error.stderr  # retained only for technical log, never the primary UI message
    assert "traceback" not in str(error).casefold()


def test_cancel_during_media_backoff_starts_no_second_process(tmp_path: Path):
    service, runner, token, _delays = service_and_token(tmp_path, 1)

    def cancel_wait(_seconds):
        token.cancel()
        token.raise_if_cancelled()

    token.wait = cancel_wait
    with pytest.raises(JobCancelledError):
        service.download(URL, "299+140", tmp_path / "Video [MAX 1080p].%(ext)s", token, "maximum")
    assert len(runner.calls) == 1
    assert runner.part.is_file()


def test_media_read_timeout_preserves_part_and_restarts_fresh_process(tmp_path: Path):
    runner = TimeoutThenSuccessRunner(tmp_path, 0)
    service = YtDlpService(runner, "yt-dlp.exe", result_root=tmp_path / "results")
    token = CancellationToken()
    delays = []
    token.wait = lambda seconds: delays.append(seconds)
    template = tmp_path / "Video [MAX 1080p].%(ext)s"

    result = service.download(URL, "299+140", template, token, "maximum")

    assert result == runner.final
    assert runner.part.is_file()
    assert len(runner.calls) == 2
    assert len(delays) == 1


def test_alternative_selector_never_silently_lowers_resolution():
    selected = VideoFormat("299", "mp4", height=1080, fps=60, vcodec="avc1", filesize=100)
    audio = VideoFormat("140", "m4a", acodec="mp4a.40.2")
    plan = FormatPlan(selected, audio, "mp4", selected, audio, False, audio)
    formats = [
        selected,
        audio,
        VideoFormat("303", "webm", height=1080, fps=60, vcodec="vp9", filesize=90),
        VideoFormat("399", "mp4", height=1080, fps=60, vcodec="av01", filesize=80),
        VideoFormat("298", "mp4", height=720, fps=60, vcodec="avc1", filesize=70),
        VideoFormat("hdr", "webm", height=1080, fps=60, vcodec="vp9", dynamic_range="HDR", filesize=95),
    ]
    alternatives = ProjectService._maximum_alternative_selectors(formats, plan)
    assert alternatives == ["303+140", "399+140"]
    assert all("298" not in value and "hdr" not in value for value in alternatives)
