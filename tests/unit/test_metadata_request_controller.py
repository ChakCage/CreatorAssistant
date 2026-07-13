import json
import threading
import time
from pathlib import Path

import pytest

from creator_assistant.domain.errors import (
    JobCancelledError,
    MetadataRequestFailedError,
    TransientMetadataError,
    YouTubeAuthenticationRequiredError,
)
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.models import VideoMetadata
from creator_assistant.domain.youtube_auth import YtDlpAuthContext
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.metadata_request_controller import (
    MetadataRequestController,
    MetadataResultCategory,
    RetryPolicy,
)
from creator_assistant.services.metadata_service import MetadataService


URL = "https://www.youtube.com/watch?v=abcdefghijk"


def metadata():
    return VideoMetadata("abcdefghijk", "Видео", 10, URL, formats=[object()])


def auth_error():
    return YouTubeAuthenticationRequiredError(URL, "abcdefghijk", "auth", "technical")


class SequenceService:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.auth = YtDlpAuthContext()
        self.yt_dlp_path = "yt-dlp.exe"

    def fetch(self, url, cancellation):
        self.calls += 1
        value = self.outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def policy(delay=0):
    return RetryPolicy(retry_delay_min=delay, retry_delay_max=delay, manual_delay_min=delay, manual_delay_max=delay)


def test_success_uses_one_attempt_and_then_cache():
    service = SequenceService([metadata()])
    controller = MetadataRequestController(service, policy())
    first = controller.request(URL, CancellationToken(), "one")
    second = controller.request(URL, CancellationToken(), "two")
    assert first.attempts == 1
    assert second.from_cache
    assert second.attempts == 0
    assert service.calls == 1


def test_first_auth_challenge_then_success_does_not_require_cookies():
    service = SequenceService([auth_error(), metadata()])
    outcome = MetadataRequestController(service, policy()).request(URL, CancellationToken(), "request")
    assert outcome.attempts == 2
    assert outcome.categories == (MetadataResultCategory.AUTH_CHALLENGE, MetadataResultCategory.SUCCESS)
    assert not service.auth.enabled


def test_two_auth_challenges_surface_auth_required_after_exactly_two_attempts():
    service = SequenceService([auth_error(), auth_error()])
    with pytest.raises(YouTubeAuthenticationRequiredError):
        MetadataRequestController(service, policy()).request(URL, CancellationToken(), "request")
    assert service.calls == 2


def test_transient_then_success_and_two_transient_failures():
    transient = TransientMetadataError("temporary")
    service = SequenceService([transient, metadata()])
    outcome = MetadataRequestController(service, policy()).request(URL, CancellationToken(), "request")
    assert outcome.categories == (MetadataResultCategory.TRANSIENT_ERROR, MetadataResultCategory.SUCCESS)
    failed = SequenceService([TransientMetadataError("one"), TransientMetadataError("two")])
    with pytest.raises(MetadataRequestFailedError, match="дважды"):
        MetadataRequestController(failed, policy()).request(URL, CancellationToken(), "request")
    assert failed.calls == 2


def test_retry_wait_is_cancellable():
    service = SequenceService([auth_error(), metadata()])
    token = CancellationToken()
    controller = MetadataRequestController(service, policy(1.0))
    result = []

    def run():
        try:
            controller.request(URL, token, "request")
        except Exception as exc:
            result.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1
    while service.calls < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    token.cancel()
    thread.join(1)
    assert len(result) == 1 and isinstance(result[0], JobCancelledError)
    assert service.calls == 1


def test_partial_success_json_is_transient_and_not_cached(tmp_path: Path):
    executable = tmp_path / "yt-dlp.exe"
    executable.write_bytes(b"exe")

    class Runner:
        def run(self, command, **kwargs):
            raw = json.dumps({"id": "abcdefghijk", "title": "", "duration": None, "formats": []})
            return ProcessResult(list(command), 0, raw, raw, "WARNING: No title found in player responses")

    service = MetadataService(Runner(), str(executable))
    with pytest.raises(TransientMetadataError):
        service.fetch(URL, CancellationToken())
