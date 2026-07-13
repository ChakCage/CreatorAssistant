import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from creator_assistant.domain.errors import (
    ProcessExecutionError,
    YouTubeAuthenticationRequiredError,
    YouTubeCookiesUnavailableError,
)
from creator_assistant.domain.job import CancellationToken
from creator_assistant.domain.youtube_auth import AUTH_BROWSER, AUTH_COOKIES_FILE, YtDlpAuthContext, validate_cookies_file
from creator_assistant.app import ServiceContainer
from creator_assistant.infrastructure.settings_store import SettingsStore
from creator_assistant.infrastructure.process_runner import ProcessResult, ProcessRunner
from creator_assistant.services.metadata_service import MetadataService, validate_youtube_url, youtube_video_id
from creator_assistant.services.yt_dlp_errors import YouTubeAccessCategory, classify_youtube_failure, classify_yt_dlp_error
from creator_assistant.services.yt_dlp_service import YtDlpService
from creator_assistant.ui.workers import FunctionWorker


def test_urls_are_normalized_and_deduplicated_by_video_id():
    assert validate_youtube_url("  https://youtu.be/abcdefghijk?si=123  ") == "https://www.youtube.com/watch?v=abcdefghijk"
    assert youtube_video_id("https://www.youtube.com/watch?v=abcdefghijk&t=12") == "abcdefghijk"


def test_browser_cookie_arguments_support_profile_without_splitting_spaces():
    auth = YtDlpAuthContext(AUTH_BROWSER, "chrome", "Profile 1", enabled=True)
    assert auth.arguments(validate=False) == ["--cookies-from-browser", "chrome:Profile 1"]
    auth.browser = "edge"
    assert auth.arguments(validate=False) == ["--cookies-from-browser", "edge:Profile 1"]
    auth.browser = "firefox"
    assert auth.arguments(validate=False) == ["--cookies-from-browser", "firefox:Profile 1"]


def test_cookies_txt_requires_netscape_header(tmp_path: Path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("not cookies", encoding="utf-8")
    with pytest.raises(Exception, match="Netscape"):
        validate_cookies_file(cookies)
    cookies.write_text("# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tname\tsecret", encoding="utf-8")
    validate_cookies_file(cookies)
    auth = YtDlpAuthContext(AUTH_COOKIES_FILE, cookies_file=str(cookies), enabled=True)
    assert auth.arguments() == ["--cookies", str(cookies)]


def test_antibot_is_a_typed_expected_error_without_null():
    auth = YtDlpAuthContext()
    original = ProcessExecutionError(
        "failed",
        "ERROR: Sign in to confirm you’re not a bot. Use --cookies-from-browser\nnull",
    )
    error = classify_yt_dlp_error(original, "https://youtu.be/abcdefghijk", "abcdefghijk", auth)
    assert isinstance(error, YouTubeAuthenticationRequiredError)
    assert error.video_id == "abcdefghijk"
    assert "null" not in error.stderr.splitlines()
    assert not error.cookies_used


def test_locked_cookie_database_has_a_typed_friendly_error():
    auth = YtDlpAuthContext(AUTH_BROWSER, "chrome", enabled=True)
    error = classify_yt_dlp_error(
        ProcessExecutionError("failed", "ERROR: could not copy cookie database: database is locked"),
        "https://youtu.be/abcdefghijk",
        "abcdefghijk",
        auth,
    )
    assert isinstance(error, YouTubeCookiesUnavailableError)
    assert "Полностью закройте" in error.reason


def test_cookie_read_marker_is_not_a_video_error_during_anonymous_access():
    auth = YtDlpAuthContext()
    original = ProcessExecutionError("failed", "ERROR: could not copy chrome cookie database: database is locked")
    assert classify_yt_dlp_error(original, "https://youtu.be/abcdefghijk", "abcdefghijk", auth) is original


def test_chrome_dpapi_failure_recommends_firefox_or_cookies_file():
    auth = YtDlpAuthContext(AUTH_BROWSER, "chrome", enabled=True)
    error = classify_yt_dlp_error(
        ProcessExecutionError("failed", "ERROR: failed to decrypt with DPAPI / App-Bound Encryption"),
        "https://youtu.be/abcdefghijk",
        "abcdefghijk",
        auth,
    )
    assert isinstance(error, YouTubeCookiesUnavailableError)
    assert "Firefox" in error.reason and "cookies.txt" in error.reason


def test_auth_and_media_403_are_distinct_categories():
    anonymous = YtDlpAuthContext()
    assert classify_youtube_failure(
        "Sign in to confirm you're not a bot", anonymous, media_operation=False
    ) == YouTubeAccessCategory.AUTH_REQUIRED
    assert classify_youtube_failure(
        "unable to download video data: HTTP Error 403: Forbidden", anonymous, media_operation=True
    ) == YouTubeAccessCategory.MEDIA_HTTP_403
    assert classify_youtube_failure(
        "could not copy chrome cookie database: database is locked", anonymous, media_operation=True
    ) == YouTubeAccessCategory.TOOL_ERROR


def test_metadata_uses_one_explicit_command_and_shared_auth(tmp_path: Path, monkeypatch):
    executable = tmp_path / "yt-dlp.exe"
    executable.write_bytes(b"exe")
    monkeypatch.setattr("creator_assistant.domain.youtube_auth.find_browser", lambda _browser: executable)
    auth = YtDlpAuthContext(AUTH_BROWSER, "chrome", enabled=True)

    class Runner:
        command = None

        def run(self, command, **kwargs):
            self.command = list(command)
            payload = {"id": "abcdefghijk", "title": "Видео", "duration": 10, "formats": [{"format_id": "1", "ext": "mp4"}], "thumbnails": []}
            return ProcessResult(self.command, 0, json.dumps(payload), json.dumps(payload), "")

    runner = Runner()
    metadata = MetadataService(runner, str(executable), auth)
    downloader = YtDlpService(runner, str(executable), auth=auth)
    assert metadata.auth is downloader.auth
    metadata.fetch("https://youtu.be/abcdefghijk", CancellationToken())
    assert runner.command.count("--dump-single-json") == 1
    assert runner.command.count("--cookies-from-browser") == 1
    assert runner.command[runner.command.index("--cookies-from-browser") + 1] == "chrome"
    assert "--ignore-config" in runner.command


def test_cookie_file_argument_is_redacted_from_process_log():
    redacted = ProcessRunner._redact(["yt-dlp", "--cookies", r"C:\Secret\cookies.txt", "url"])
    assert r"C:\Secret\cookies.txt" not in redacted


def test_anonymous_metadata_keeps_max_download_anonymous(tmp_path: Path):
    executable = tmp_path / "yt-dlp.exe"
    executable.write_bytes(b"exe")
    auth = YtDlpAuthContext()

    class Runner:
        def __init__(self):
            self.commands = []

        def run(self, command, **kwargs):
            command = list(command)
            self.commands.append(command)
            if "--dump-single-json" in command:
                payload = {"id": "abcdefghijk", "title": "Видео", "duration": 10, "formats": [{"format_id": "1", "ext": "mp4"}], "thumbnails": []}
                return ProcessResult(command, 0, json.dumps(payload), json.dumps(payload), "")
            final = tmp_path / "Video.mp4"
            final.write_bytes(b"media")
            result_path = Path(command[command.index("--print-to-file") + 2])
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(str(final), encoding="utf-8")
            return ProcessResult(command, 0, "ok", "ok", "")

    runner = Runner()
    MetadataService(runner, str(executable), auth).fetch("https://youtu.be/abcdefghijk", CancellationToken())
    YtDlpService(runner, str(executable), result_root=tmp_path / "results", auth=auth).download(
        "https://youtu.be/abcdefghijk",
        "1",
        tmp_path / "Video.%(ext)s",
        CancellationToken(),
        "maximum",
        role="MAX_VIDEO",
    )
    assert all("--cookies-from-browser" not in command and "--cookies" not in command for command in runner.commands)
    assert auth.effective_mode == "anonymous"


def test_one_time_cookie_consent_is_not_written_to_global_settings():
    saved = []
    fake = SimpleNamespace(
        youtube_auth=YtDlpAuthContext(),
        settings={"youtube_access": {"mode": "automatic", "always_use": False}},
        settings_store=SimpleNamespace(save=lambda value: saved.append(value)),
    )
    ServiceContainer.enable_youtube_auth(fake, AUTH_BROWSER, "firefox", persist=False)
    assert fake.youtube_auth.effective_mode == "firefox"
    assert saved == []
    assert fake.settings["youtube_access"]["mode"] == "automatic"


def test_settings_migration_clears_nonpersistent_stale_chrome_context(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "youtube_access": {
            "mode": "browser",
            "browser": "chrome",
            "browser_profile": "Default",
            "cookies_file": "secret.txt",
            "always_use": False,
        }
    }), encoding="utf-8")
    access = SettingsStore(path).load()["youtube_access"]
    assert access == {
        "mode": "automatic",
        "browser": "",
        "browser_profile": "",
        "cookies_file": "",
        "always_use": False,
        "schema_version": 2,
    }


def test_worker_emits_authentication_signal_instead_of_failed_traceback():
    error = YouTubeAuthenticationRequiredError("url", "abcdefghijk", "auth", "technical")
    worker = FunctionWorker(lambda progress: (_ for _ in ()).throw(error))
    auth_events = []
    failed_events = []
    worker.authentication_required.connect(auth_events.append)
    worker.failed.connect(lambda *args: failed_events.append(args))
    worker.run()
    assert auth_events == [error]
    assert failed_events == []


def test_project_tab_has_no_automatic_metadata_timer():
    source = (Path(__file__).resolve().parents[2] / "src" / "creator_assistant" / "ui" / "project_prep_tab.py").read_text(encoding="utf-8")
    assert "auto_fetch" not in source
    assert 'textChanged.connect(self._safe_ui_action("url_changed", self._url_changed))' in source
    assert 'returnPressed.connect(self._safe_ui_action("metadata_enter"' in source
    assert 'clicked.connect(self._safe_ui_action("metadata_button"' in source
