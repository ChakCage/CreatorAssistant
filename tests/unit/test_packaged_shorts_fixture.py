import json
from pathlib import Path
from types import SimpleNamespace

from creator_assistant.domain.shorts.models import SourceInfo
from creator_assistant.infrastructure.packaged_shorts_fixture import run_live_free_shorts_fixture
from creator_assistant.services.licensing import LicenseClientError


class FixtureContainer:
    def __init__(self, outcome="ok"):
        self.outcome = outcome
        self.runner = object()
        self.paths = {"ffprobe": "ffprobe"}
        self.finished = []

    def require_entitlement(self, feature):
        assert feature == "shorts_analysis"

    def acquire_free_quota(self, kind, operation_key):
        assert kind == "SHORTS_SOURCE"
        if self.outcome == "blocked":
            raise LicenseClientError(
                "FREE_QUOTA_EXHAUSTED", "Бесплатный лимит исходных видео для Shorts исчерпан: 2 из 2.",
                details={"kind": kind, "used": 2, "limit": 2},
            )
        return "reservation-1"

    def finish_free_quota(self, reservation, success):
        self.finished.append((reservation, success))


def source(path: Path) -> SourceInfo:
    return SourceInfo(
        path=str(path), name=path.name, size=path.stat().st_size, mtime=path.stat().st_mtime,
        duration=9.0, width=1280, height=720, fps=30.0, video_codec="h264",
        audio_codec="aac", audio_channels=2, sample_rate=48000,
        fingerprint="legacy-path-fingerprint", content_fingerprint="a" * 64,
    )


def test_packaged_free_shorts_success_retry_failure_and_block(monkeypatch, tmp_path):
    workspace = tmp_path.resolve()
    media = workspace / "SOURCE1.mp4"
    media.write_bytes(b"fixture")
    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_LIVE_FREE", "1")
    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_WORKSPACE", str(workspace))
    monkeypatch.setattr(
        "creator_assistant.infrastructure.packaged_shorts_fixture.ShortsSourceService.probe",
        lambda _self, path: source(path),
    )

    container = FixtureContainer()
    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_SHORTS_MODE", "success")
    first = run_live_free_shorts_fixture(container, media, workspace / "first.json")
    assert first["success"] and first["candidate_count"] == 3
    assert container.finished == [("reservation-1", True)]

    renamed = workspace / "renamed.mp4"
    renamed.write_bytes(media.read_bytes())
    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_SHORTS_MODE", "retry")
    retry = run_live_free_shorts_fixture(container, renamed, workspace / "retry.json")
    assert retry["success"] and retry["project_path"] == first["project_path"]

    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_SHORTS_MODE", "fail")
    failed = run_live_free_shorts_fixture(container, media, workspace / "failed.json")
    assert not failed["success"] and failed["reservation_released"]

    blocked_container = FixtureContainer("blocked")
    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_SHORTS_MODE", "blocked")
    blocked = run_live_free_shorts_fixture(blocked_container, media, workspace / "blocked.json")
    assert blocked["success"] and blocked["blocked_as_expected"]
    assert blocked["quota_details"] == {"kind": "SHORTS_SOURCE", "used": 2, "limit": 2}
    assert blocked_container.finished == []


def test_live_shorts_fixture_rejects_paths_outside_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"fixture")
    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_LIVE_FREE", "1")
    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_WORKSPACE", str(workspace))
    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_SHORTS_MODE", "success")

    try:
        run_live_free_shorts_fixture(FixtureContainer(), outside, workspace / "report.json")
    except RuntimeError as exc:
        assert "isolated workspace" in str(exc)
    else:
        raise AssertionError("unsafe source path was accepted")
