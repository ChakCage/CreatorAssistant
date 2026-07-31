from __future__ import annotations

import json
from pathlib import Path

from creator_assistant.infrastructure.packaged_project_fixture import run_packaged_project_fixture
from creator_assistant.version import __version__


class Gate:
    def __init__(self) -> None:
        self.features = []

    def require(self, feature: str) -> None:
        self.features.append(feature)


def test_beta3_version_is_single_runtime_source():
    import creator_assistant

    assert __version__ == "0.3.1-beta.3"
    assert creator_assistant.__version__ == __version__


def test_short_packaged_project_fixture_is_resumable_and_safe(tmp_path: Path):
    gate = Gate()
    report_path = tmp_path / "report.json"
    report = run_packaged_project_fixture(gate, report_path)

    assert report["success"] is True
    assert report["manifest_status"] == "VALID"
    assert report["media_count_after_rerun"] == 1
    assert report["cancellation"] == "handled"
    assert report["missing_source"] == "missing"
    assert gate.features == ["project_preparation"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["success"] is True


def test_installer_supports_isolated_shortcut_without_touching_owner_desktop():
    root = Path(__file__).resolve().parents[2]
    installer = (root / "installer" / "CreatorAssistant.iss").read_text(encoding="utf-8")
    verifier = (root / "scripts" / "test-isolated-shortcut.ps1").read_text(encoding="utf-8-sig")

    assert "CREATOR_ASSISTANT_E2E_DESKTOP" in installer
    assert "{code:DesktopShortcutDirectory}" in installer
    assert "Creator Assistant Commercial Staging" in verifier
    assert "removed_after_uninstall" in verifier
