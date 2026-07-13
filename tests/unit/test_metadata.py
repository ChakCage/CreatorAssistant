import pytest
import json

from creator_assistant.domain.errors import InvalidVideoUrlError, PlaylistNotSupportedError
from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.metadata_service import MetadataService, validate_youtube_url


def test_single_youtube_urls_are_accepted():
    assert validate_youtube_url("https://youtu.be/abcdefghijk")
    assert validate_youtube_url("https://www.youtube.com/watch?v=abcdefghijk")


def test_playlist_is_rejected():
    with pytest.raises(PlaylistNotSupportedError):
        validate_youtube_url("https://www.youtube.com/watch?v=abcdefghijk&list=PL123")


def test_non_youtube_url_is_rejected():
    with pytest.raises(InvalidVideoUrlError):
        validate_youtube_url("https://example.com/video")


def test_metadata_json_is_read_from_stdout_when_stderr_contains_warning(tmp_path):
    executable = tmp_path / "yt-dlp.exe"
    executable.write_bytes(b"exe")
    payload = {"id": "abcdefghijk", "title": "Видео", "duration": 10, "webpage_url": "https://youtu.be/abcdefghijk", "formats": [{"format_id": "1", "ext": "mp4"}], "thumbnails": []}

    class Runner:
        def run(self, command, **kwargs):
            stdout = json.dumps(payload)
            warning = "WARNING: JavaScript runtime not found"
            return ProcessResult(list(command), 0, warning + "\n" + stdout, stdout, warning)

    metadata = MetadataService(Runner(), str(executable)).fetch("https://youtu.be/abcdefghijk", CancellationToken())
    assert metadata.title == "Видео"
