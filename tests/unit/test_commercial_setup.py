from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from creator_assistant.infrastructure.settings_store import DEFAULT_SETTINGS, SettingsStore
from creator_assistant.product import AppEdition, COMMERCIAL_AI_MODELS, DEVELOPER_AI_MODEL
from creator_assistant.services.commercial_setup import AI_PROFILES, CommercialSetupService
from creator_assistant.ui.commercial_setup_wizard import CommercialSetupWizard

_QT_APPS = []


def qt_app():
    from PySide6.QtWidgets import QApplication
    application = QApplication.instance() or QApplication([])
    _QT_APPS.append(application)
    return application


def settings() -> dict:
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def test_commercial_has_exactly_two_supported_profiles():
    assert tuple(item.model_id for item in AI_PROFILES.values()) == COMMERCIAL_AI_MODELS
    assert AI_PROFILES["compact"].model_id == "qwen3:14b"
    assert AI_PROFILES["maximum_quality"].model_id == "qwen3.6:35b-a3b"


def test_apply_profile_is_strict_and_disables_fallback():
    value = settings(); service = CommercialSetupService(value)
    service.apply_profile("compact")
    assert {"model": "qwen3:14b", "strict_model": True, "fallback": False}.items() <= value["shorts_ai"].items()
    assert value["commercial_setup"]["model_profile"] == "compact"


def test_unsupported_model_cannot_be_pulled():
    with pytest.raises(ValueError, match="Неподдерживаемая"):
        CommercialSetupService(settings()).pull_model("other:model", lambda _: None, lambda: False)


def test_model_installed_requires_exact_id(monkeypatch):
    service = CommercialSetupService(settings())
    monkeypatch.setattr(service, "installed_models", lambda: [{"name": "qwen3:14b-latest"}, {"name": "qwen3.6:35b-a3b"}])
    assert service.model_installed("qwen3.6:35b-a3b")
    assert not service.model_installed("qwen3:14b")


def test_inspect_survives_missing_ollama(monkeypatch):
    service = CommercialSetupService(settings())
    monkeypatch.setattr(service, "api_version", lambda: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    report = service.inspect()
    assert report.ollama_api is False
    assert any(item.key == "ollama" and item.status == "action" for item in report.components)


def test_inspect_recommends_compact_when_resources_are_unknown(monkeypatch):
    import creator_assistant.services.commercial_setup as module
    monkeypatch.setattr(module, "_memory_status", lambda: (0, 0))
    monkeypatch.setattr(module, "_free_bytes", lambda _: 0)
    service = CommercialSetupService(settings())
    monkeypatch.setattr(service, "api_version", lambda: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    assert service.inspect().recommendation == "compact"


def test_inspect_recommends_quality_from_ram_vram_and_space(monkeypatch):
    import creator_assistant.services.commercial_setup as module
    monkeypatch.setattr(module, "_memory_status", lambda: (64 * 1024**3, 40 * 1024**3))
    monkeypatch.setattr(module, "_free_bytes", lambda _: 100 * 1024**3)
    monkeypatch.setattr(module.shutil, "which", lambda name: "nvidia-smi.exe" if "nvidia" in name else None)
    responses = iter([
        SimpleNamespace(returncode=0, stdout="NVIDIA RTX 5080, 16384, 600.00\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="CUDA Version: 13.0", stderr=""),
    ])
    monkeypatch.setattr(module, "_run", lambda *a, **k: next(responses))
    service = CommercialSetupService(settings())
    monkeypatch.setattr(service, "api_version", lambda: "1.0")
    monkeypatch.setattr(service, "installed_models", lambda: [])
    report = service.inspect()
    assert report.recommendation == "maximum_quality"
    assert report.vram_total == 16384 * 1024**2


def test_structured_preflight_rejects_bad_json(monkeypatch):
    service = CommercialSetupService(settings())
    monkeypatch.setattr(service, "_api", lambda *a, **k: {"message": {"content": "not-json"}})
    with pytest.raises(RuntimeError, match="некорректный JSON"):
        service.structured_preflight("qwen3:14b")


def test_structured_preflight_records_runtime(monkeypatch):
    service = CommercialSetupService(settings())
    def api(path, payload=None, timeout=0):
        if path == "/api/chat":
            return {"message": {"content": '{"ready": true, "language": "ru"}'}, "eval_count": 3}
        return {"models": [{"name": "qwen3:14b", "size_vram": 8}]}
    monkeypatch.setattr(service, "_api", api)
    result = service.structured_preflight("qwen3:14b")
    assert result["response"]["ready"] is True
    assert result["runtime"]["name"] == "qwen3:14b"


def test_settings_migration_preserves_commercial_14b(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"shorts_ai": {"model": "qwen3:14b"}}), encoding="utf-8")
    assert SettingsStore(path).load()["shorts_ai"]["model"] == "qwen3:14b"


def test_diagnostic_report_has_no_secret_fields(tmp_path: Path):
    report = {"edition": "commercial", "model": "qwen3:14b", "status": "ready"}
    json_path, text_path = CommercialSetupService.write_report(report, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8")) == report
    assert "token" not in text_path.read_text(encoding="utf-8").casefold()


def test_developer_model_constant_remains_35b():
    assert DEVELOPER_AI_MODEL == "qwen3.6:35b-a3b"


def test_existing_project_is_not_mutated_when_profile_changes():
    value = settings(); manifest = {"ai_analysis": {"model_id": "qwen3:14b"}}
    CommercialSetupService(value).apply_profile("maximum_quality")
    assert manifest["ai_analysis"]["model_id"] == "qwen3:14b"


def test_report_is_safe_serializable(monkeypatch):
    service = CommercialSetupService(settings())
    monkeypatch.setattr(service, "api_version", lambda: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    encoded = json.dumps(service.inspect().safe_dict(), ensure_ascii=False)
    assert "oauth" not in encoded.casefold()
    assert "transcript" not in encoded.casefold()


def test_commercial_wizard_exposes_all_required_steps(tmp_path: Path):
    application = qt_app()
    value = settings()
    container = SimpleNamespace(
        edition=AppEdition.COMMERCIAL,
        settings=value,
        settings_store=SettingsStore(tmp_path / "settings.json"),
        commercial_setup=CommercialSetupService(value),
        first_run=True,
    )
    wizard = CommercialSetupWizard(container)
    titles = [wizard.page(page_id).title() for page_id in wizard.pageIds()]
    assert titles == [
        "Добро пожаловать", "Активация лицензии", "Проверка компьютера",
        "Выбор AI-профиля", "Ollama", "Загрузка модели", "Проверка AI",
        "Whisper, FFmpeg и рендер", "Папки", "Короткое обучение", "Готово",
    ]
    assert set(wizard.profile_radios) == {"compact", "maximum_quality"}
    wizard.close()


def test_wizard_is_rejected_for_developer():
    application = qt_app()
    container = SimpleNamespace(edition=AppEdition.DEVELOPER)
    with pytest.raises(RuntimeError, match="Commercial Edition"):
        CommercialSetupWizard(container)


def test_commercial_ai_success_hides_raw_runtime_dictionary(tmp_path: Path):
    application = qt_app()
    value = settings()
    service = CommercialSetupService(value)
    container = SimpleNamespace(
        edition=AppEdition.COMMERCIAL,
        settings=value,
        settings_store=SettingsStore(tmp_path / "settings.json"),
        commercial_setup=service,
        first_run=True,
    )
    wizard = CommercialSetupWizard(container)
    wizard._preflight_ready({
        "model": "qwen3.6:35b-a3b",
        "duration_seconds": 46.7,
        "runtime": {"digest": "secret-looking-runtime-value", "quantization": "Q4_K_M"},
    })
    text = wizard.ai_test_status.text()
    assert "qwen3.6:35b-a3b" in text
    assert "46.7" in text
    assert "Structured JSON" in text
    assert "digest" not in text
    assert "Q4_K_M" not in text
    wizard.close()
