from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from creator_assistant.product import AppEdition, DEVELOPER_AI_MODEL
from creator_assistant.services.developer_setup import DeveloperSetupService
from creator_assistant.ui.developer_setup_wizard import DeveloperSetupWizard


def _app():
    return QApplication.instance() or QApplication([])


def test_developer_wizard_has_standalone_five_step_flow(tmp_path: Path):
    app = _app()
    service = DeveloperSetupService({"developer_setup": {}, "shorts_ai": {}}, {})
    container = SimpleNamespace(
        edition=AppEdition.DEVELOPER,
        developer_setup=service,
        settings=service.settings,
        settings_store=SimpleNamespace(save=lambda _value: None),
        first_run=True,
    )
    wizard = DeveloperSetupWizard(container)
    assert app is QApplication.instance()
    assert [wizard.page(page_id).title() for page_id in wizard.pageIds()] == [
        "Creator Assistant готов к настройке",
        "Проверка компонентов",
        "Локальный AI",
        "Проверка AI",
        "Готово",
    ]
    assert DEVELOPER_AI_MODEL in wizard.page(2).subTitle()
    assert "лиценз" not in wizard.windowTitle().casefold()


def test_yt_dlp_install_uses_official_checksum(monkeypatch, tmp_path: Path):
    service = DeveloperSetupService({}, {})
    service.tools_root = tmp_path / "tools"
    payload = b"official-yt-dlp"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(service, "_json_url", lambda _url: {
        "assets": [
            {"name": "yt-dlp.exe", "browser_download_url": "https://github.test/yt-dlp.exe"},
            {"name": "SHA2-256SUMS", "browser_download_url": "https://github.test/SHA2-256SUMS"},
        ]
    })
    monkeypatch.setattr(service, "_read_url", lambda _url: f"{digest}  yt-dlp.exe\n".encode())

    def download(url, destination, expected, progress, cancelled):
        assert url == "https://github.test/yt-dlp.exe"
        assert expected == digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    monkeypatch.setattr(service, "_download_verified", download)
    result = service.install_yt_dlp(lambda _value: None, lambda: False)
    assert result.read_bytes() == payload


def test_ffmpeg_install_extracts_only_managed_tools(monkeypatch, tmp_path: Path):
    service = DeveloperSetupService({}, {})
    service.tools_root = tmp_path / "tools"
    service.downloads_root = tmp_path / "downloads"
    archive = service.downloads_root / "ffmpeg-release-essentials.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("ffmpeg-9/bin/ffmpeg.exe", b"ffmpeg")
        package.writestr("ffmpeg-9/bin/ffprobe.exe", b"ffprobe")
        package.writestr("ffmpeg-9/bin/ffplay.exe", b"ffplay")
    monkeypatch.setattr(service, "_read_url", lambda _url: b"a" * 64)
    monkeypatch.setattr(service, "_download_verified", lambda _url, _dest, _sha, _progress, _cancelled: None)
    ffmpeg, ffprobe = service.install_ffmpeg(lambda _value: None, lambda: False)
    assert ffmpeg.read_bytes() == b"ffmpeg"
    assert ffprobe.read_bytes() == b"ffprobe"
    assert str(ffmpeg).startswith(str(service.tools_root))


def test_ai_smoke_requires_exact_developer_model(monkeypatch):
    service = DeveloperSetupService({}, {})
    captured = {}

    def api(path, payload=None, timeout=0):
        captured.update({"path": path, "payload": payload, "timeout": timeout})
        return {"message": {"content": json.dumps({"ready": True, "language": "ru"})}}

    monkeypatch.setattr(service, "_api", api)
    result = service.ai_smoke_test()
    assert result["model"] == DEVELOPER_AI_MODEL
    assert captured["payload"]["model"] == "qwen3.6:35b-a3b"
    assert captured["payload"]["keep_alive"] == "60m"


def test_developer_setup_has_no_creator_backend_or_payment_endpoint():
    service = DeveloperSetupService({}, {})
    values = " ".join(str(value) for value in service.__dict__.values())
    assert "kazakovapps" not in values
    assert "yookassa" not in values.casefold()
    assert service.endpoint == "http://127.0.0.1:11434"

