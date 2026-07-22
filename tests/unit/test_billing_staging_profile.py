from __future__ import annotations

from types import SimpleNamespace

from creator_assistant.infrastructure import build_info as build_info_module
from creator_assistant.infrastructure.settings_store import config_root, local_data_root
from creator_assistant.product import AppEdition, qsettings_application_name


def test_commercial_staging_uses_separate_appdata_qsettings_and_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(build_info_module, "current_build_info", lambda: SimpleNamespace(license_backend_profile="staging"))
    assert config_root(AppEdition.COMMERCIAL).name == "CommercialStaging"
    assert local_data_root(AppEdition.COMMERCIAL).name == "CommercialStaging"
    assert qsettings_application_name(AppEdition.COMMERCIAL) == "CreatorAssistantCommercialStaging"


def test_build_defines_three_non_overlapping_packages_and_bot_deep_links():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts" / "build.ps1").read_text(encoding="utf-8-sig")
    spec = (root / "CreatorAssistant.spec").read_text(encoding="utf-8-sig")
    dialog = (root / "src" / "creator_assistant" / "ui" / "license_dialog.py").read_text(encoding="utf-8-sig")
    assert "CreatorAssistant-Commercial-Staging" in script and "CreatorAssistant-Commercial-Staging.exe" in script
    assert "Creator Assistant Commercial Staging" in script
    assert "CREATOR_ASSISTANT_BUILD_VARIANT" in spec
    assert "telegram_bot_url" in dialog and "?start=buy" in dialog
