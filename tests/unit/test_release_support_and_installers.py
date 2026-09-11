from __future__ import annotations

import json
import zipfile
from pathlib import Path

from creator_assistant.infrastructure import support_bundle as support
from creator_assistant.infrastructure.settings_store import SettingsStore


def test_settings_migration_creates_backup_and_is_atomic(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text('{"youtube_root":"E:/Existing"}', encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded["_schema_version"] == 2
    assert loaded["youtube_root"] == "E:/Existing"
    assert json.loads(path.read_text(encoding="utf-8"))["_schema_version"] == 2
    backups = list(tmp_path.glob("settings.backup-v0-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["youtube_root"] == "E:/Existing"


def test_support_zip_is_local_previewed_and_sanitized(monkeypatch, tmp_path: Path):
    local = tmp_path / "local"
    config = tmp_path / "config"
    logs = local / "logs"
    logs.mkdir(parents=True)
    (logs / "creator_assistant.log").write_text(
        f"home={Path.home()} authorization=SECRET token: abc123\n", encoding="utf-8"
    )
    monkeypatch.setattr(support, "local_data_root", lambda: local)
    monkeypatch.setattr(support, "config_root", lambda: config)
    builder = support.SupportBundleBuilder({"oauth": "must-not-leak"})
    preview = builder.preview()
    assert "diagnostic.json" in preview.entries
    destination = tmp_path / "support.zip"
    builder.create(destination)
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        log = archive.read("logs/creator_assistant.log").decode("utf-8")
        diagnostic = archive.read("diagnostic.json").decode("utf-8")
    assert names == ["diagnostic.json", "logs/creator_assistant.log"]
    assert "SECRET" not in log and "abc123" not in log
    assert str(Path.home()) not in log
    assert "must-not-leak" not in diagnostic


def test_inno_installer_defines_three_isolated_products_and_preserves_data():
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "CreatorAssistant.iss").read_text(encoding="utf-8")
    assert script.count("#define AppIdValue") == 3
    assert 'InstallLeaf "DeveloperPreview"' in script
    assert 'InstallLeaf "Commercial"' in script
    assert 'InstallLeaf "Commercial-Staging"' in script
    assert "PrivilegesRequired=lowest" in script
    assert "DisableDirPage=no" in script
    assert "Путь во временной папке используется только установщиком" in script
    assert "IsTemporaryInstallPath(WizardForm.DirEdit.Text)" in script
    assert "ExpandConstant('{localappdata}\\Programs\\CreatorAssistant\\{#InstallLeaf}')" in script
    assert "SetupMutex=CreatorAssistant-Installer-Global" in script
    assert "Также удалить настройки и данные Creator Assistant" in script
    assert "Общие модели Ollama/Whisper" in script


def test_build_uses_one_version_source_and_all_three_packages():
    root = Path(__file__).resolve().parents[2]
    build = (root / "scripts" / "build.ps1").read_text(encoding="utf-8-sig")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "creator_assistant.version import __version__" in build
    assert "@('developer', 'commercial', 'commercial-staging')" in build
    assert 'dynamic = ["version"]' in pyproject
