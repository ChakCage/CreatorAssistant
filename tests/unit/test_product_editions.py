from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from creator_assistant.domain.shorts.models import SourceInfo
from creator_assistant.infrastructure.settings_store import (
    SettingsStore,
    config_root,
    local_data_root,
    shared_data_root,
)
from creator_assistant.infrastructure.credential_store import WindowsCredentialStore
from creator_assistant.product import (
    AppEdition,
    DEVELOPER_AI_MODEL,
    Feature,
    FeatureRegistry,
    build_artifact,
    qsettings_application_name,
)
from creator_assistant.services.shorts.manifest import ShortsManifestStore


def test_developer_contains_all_product_tabs_and_strict_ai_model():
    assert FeatureRegistry.navigation_tabs(AppEdition.DEVELOPER) == (
        "Подготовка проекта",
        "Shorts",
        "Автопилот",
        "Очередь публикаций",
    )
    assert FeatureRegistry.is_available(Feature.YOUTUBE_PUBLISHING, AppEdition.DEVELOPER)
    assert FeatureRegistry.is_available(Feature.DIAGNOSTICS, AppEdition.DEVELOPER)
    assert not FeatureRegistry.is_available(Feature.LICENSING, AppEdition.DEVELOPER)
    assert DEVELOPER_AI_MODEL == "qwen3.6:35b-a3b"


def test_commercial_contains_only_product_tabs_and_licensing():
    assert FeatureRegistry.navigation_tabs(AppEdition.COMMERCIAL) == (
        "Подготовка проекта",
        "Shorts",
    )
    assert FeatureRegistry.is_available(Feature.LICENSING, AppEdition.COMMERCIAL)
    assert not FeatureRegistry.is_available(Feature.AUTOPILOT, AppEdition.COMMERCIAL)
    assert not FeatureRegistry.is_available(Feature.PUBLISHING_QUEUE, AppEdition.COMMERCIAL)
    assert not FeatureRegistry.is_available(Feature.YOUTUBE_PUBLISHING, AppEdition.COMMERCIAL)


def test_edition_settings_databases_logs_and_qsettings_are_separate(monkeypatch, tmp_path: Path):
    roaming = tmp_path / "roaming"
    local = tmp_path / "local"
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    developer_config = config_root(AppEdition.DEVELOPER)
    commercial_config = config_root(AppEdition.COMMERCIAL)
    developer_local = local_data_root(AppEdition.DEVELOPER)
    commercial_local = local_data_root(AppEdition.COMMERCIAL)
    assert developer_config == roaming / "CreatorAssistant" / "Developer"
    assert commercial_config == roaming / "CreatorAssistant" / "Commercial"
    assert developer_local == local / "CreatorAssistant" / "Developer"
    assert commercial_local == local / "CreatorAssistant" / "Commercial"
    assert shared_data_root() == local / "CreatorAssistant"
    assert qsettings_application_name(AppEdition.DEVELOPER) != qsettings_application_name(AppEdition.COMMERCIAL)

    developer = SettingsStore(developer_config / "settings.json")
    commercial = SettingsStore(commercial_config / "settings.json")
    developer_settings = developer.load()
    developer_settings["youtube_root"] = "D:/Developer"
    developer.save(developer_settings)
    assert commercial.load()["youtube_root"] != "D:/Developer"


def test_credential_manager_keys_are_edition_namespaced():
    developer = WindowsCredentialStore(allow_test_memory=True, namespace="CreatorAssistant/Developer/Publishing")
    commercial = WindowsCredentialStore(allow_test_memory=True, namespace="CreatorAssistant/Commercial/Publishing")
    developer.write_json("youtube", {"refresh_token": "developer"})
    commercial.write_json("youtube", {"refresh_token": "commercial"})
    assert developer.read_json("youtube")["refresh_token"] == "developer"
    assert commercial.read_json("youtube")["refresh_token"] == "commercial"
    assert developer.target("youtube") != commercial.target("youtube")


def test_legacy_settings_are_safely_copied_to_developer_only(monkeypatch, tmp_path: Path):
    roaming = tmp_path / "roaming"
    local = tmp_path / "local"
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("CREATOR_ASSISTANT_EDITION", "developer")
    legacy = roaming / "CreatorAssistant"
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text('{"youtube_root":"E:/Legacy"}', encoding="utf-8")
    (legacy / "user_assets").mkdir()
    (legacy / "user_assets" / "banner.png").write_bytes(b"banner")
    shared_models = local / "CreatorAssistant" / "models"
    shared_models.mkdir(parents=True)
    (shared_models / "model.bin").write_bytes(b"shared")

    store = SettingsStore()
    assert store.path == legacy / "Developer" / "settings.json"
    assert store.load()["youtube_root"] == "E:/Legacy"
    assert (legacy / "Developer" / "user_assets" / "banner.png").read_bytes() == b"banner"
    assert not (legacy / "Commercial" / "settings.json").exists()
    assert (shared_models / "model.bin").read_bytes() == b"shared"
    assert not (local / "CreatorAssistant" / "Developer" / "models").exists()


def test_both_editions_open_the_same_existing_shorts_project(tmp_path: Path):
    manifest_path = tmp_path / "Existing Shorts" / "shorts_manifest.json"
    source = SourceInfo(
        path=str(tmp_path / "source.mp4"), name="source.mp4", size=123,
        mtime=1.0, duration=60.0, width=1920, height=1080, fps=59.94,
        video_codec="h264", audio_codec="aac", audio_channels=2, sample_rate=48000,
        fingerprint="shared-project",
    )
    ShortsManifestStore(manifest_path).create("shared-id", source)
    for edition in (AppEdition.DEVELOPER, AppEdition.COMMERCIAL):
        assert FeatureRegistry.is_available(Feature.SHORTS, edition)
        loaded = ShortsManifestStore(manifest_path).load()
        assert loaded is not None
        assert loaded.shorts_project_id == "shared-id"
        assert loaded.source_fingerprint == "shared-project"


def test_build_artifacts_and_shortcuts_do_not_overlap():
    developer = build_artifact(AppEdition.DEVELOPER)
    commercial = build_artifact(AppEdition.COMMERCIAL)
    assert developer == (
        "CreatorAssistant-Developer",
        "CreatorAssistant-Developer.exe",
        "Creator Assistant Developer",
    )
    assert commercial == ("CreatorAssistant", "CreatorAssistant.exe", "Creator Assistant")
    assert set(developer).isdisjoint(set(commercial))


def test_commercial_import_graph_does_not_load_developer_modules(tmp_path: Path):
    code = r'''import json, sys
import creator_assistant.app
import creator_assistant.ui.settings_dialog
import creator_assistant.ui.main_window
forbidden = (
    "creator_assistant.ui.autopilot_tab",
    "creator_assistant.ui.publishing_queue",
    "creator_assistant.ui.publishing_accounts",
    "creator_assistant.services.automation",
    "creator_assistant.services.publishing",
    "creator_assistant.infrastructure.automation_job_store",
    "creator_assistant.infrastructure.publishing_store",
    "creator_assistant.infrastructure.credential_store",
)
print(json.dumps(sorted(name for name in sys.modules if name.startswith(forbidden))))
'''
    environment = dict(os.environ)
    environment["CREATOR_ASSISTANT_EDITION"] = "commercial"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=environment, timeout=60, check=True,
    )
    assert json.loads(result.stdout.strip()) == []


def test_build_scripts_define_two_independent_outputs_and_package_boundary():
    root = Path(__file__).resolve().parents[2]
    build_script = (root / "scripts" / "build.ps1").read_text(encoding="utf-8-sig")
    shortcut_script = (root / "scripts" / "create_shortcut.ps1").read_text(encoding="utf-8-sig")
    verifier = (root / "scripts" / "verify-package-edition.ps1").read_text(encoding="utf-8-sig")
    assert "CreatorAssistant-Developer" in build_script
    assert "CreatorAssistant-Developer.exe" in build_script
    assert "Creator Assistant Developer" in build_script
    assert "CreatorAssistant.exe" in build_script
    assert "verify-package-edition.ps1" in build_script
    assert "Creator Assistant Developer" in shortcut_script
    assert "Commercial package contains Developer-only modules" in verifier


def test_package_excludes_ambient_optional_runtime_dlls():
    root = Path(__file__).resolve().parents[2]
    spec = (root / "CreatorAssistant.spec").read_text(encoding="utf-8-sig")

    for dll_name in ("icudt78.dll", "icuuc.dll", "libcrypto-3-x64.dll", "libssl-3-x64.dll"):
        assert dll_name in spec
    assert "AMBIENT_OPTIONAL_DLLS" in spec
    assert "a.binaries =" in spec
