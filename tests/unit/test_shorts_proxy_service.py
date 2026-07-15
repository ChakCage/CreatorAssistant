from pathlib import Path

import pytest

from creator_assistant.domain.job import CancellationToken
from creator_assistant.infrastructure.process_runner import ProcessResult
from creator_assistant.services.shorts.proxy_service import AnalysisProxyService


class Runner:
    def run(self, command, **_kwargs):
        Path(command[-1]).write_bytes(b"proxy")
        return ProcessResult(list(command), 0, "")


def test_proxy_uses_unique_tmp_file_and_replaces_target(tmp_path):
    service = AnalysisProxyService(Runner(), "ffmpeg.exe", False)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    target = tmp_path / "analysis_proxy.mp4"
    service.create(source, target, CancellationToken())
    assert target.read_bytes() == b"proxy"
    assert not list(tmp_path.glob("analysis_proxy.tmp.mp4"))


def test_proxy_replace_retries_permission_error(monkeypatch, tmp_path):
    temporary = tmp_path / "analysis_proxy.1.abc.tmp.mp4"
    target = tmp_path / "analysis_proxy.mp4"
    temporary.write_bytes(b"proxy")
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError("busy")
        Path(dst).write_bytes(Path(src).read_bytes())
        Path(src).unlink()

    monkeypatch.setattr("creator_assistant.services.shorts.proxy_service.os.replace", flaky_replace)
    monkeypatch.setattr("creator_assistant.services.shorts.proxy_service.time.sleep", lambda _seconds: None)
    AnalysisProxyService._replace_with_retry(temporary, target)
    assert calls["count"] == 3
    assert target.read_bytes() == b"proxy"


def test_proxy_replace_reports_busy_file(monkeypatch, tmp_path):
    temporary = tmp_path / "analysis_proxy.1.abc.tmp.mp4"
    target = tmp_path / "analysis_proxy.mp4"
    temporary.write_bytes(b"proxy")
    monkeypatch.setattr("creator_assistant.services.shorts.proxy_service.os.replace", lambda *_args: (_ for _ in ()).throw(PermissionError("busy")))
    monkeypatch.setattr("creator_assistant.services.shorts.proxy_service.time.sleep", lambda _seconds: None)
    with pytest.raises(PermissionError, match="файл занят"):
        AnalysisProxyService._replace_with_retry(temporary, target)
