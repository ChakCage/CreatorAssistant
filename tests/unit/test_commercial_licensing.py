from __future__ import annotations

import base64
import json
import socket
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from creator_assistant.infrastructure.secure_credential_store import WindowsSecureCredentialStore
from creator_assistant.product import AppEdition
from creator_assistant.services.licensing import FeatureGate, LicenseClientError, LicenseService, LicenseState, SecureLicenseStorage


def b64(value: bytes) -> str: return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def token(private, key_id, installation_id, now, *, grace_hours=72, minimum="0.1.0", features=None):
    payload = {"schema_version": 1, "token_id": "token", "key_id": key_id, "product_code": "creator_assistant",
               "user_id": "user", "subscription_id": "subscription", "device_id": "device", "installation_id": installation_id,
               "plan_code": "beta", "features": features or ["shorts_analysis", "batch_render", "project_preparation"],
               "issued_at": now.isoformat(), "not_before": now.isoformat(), "expires_at": (now + timedelta(hours=48)).isoformat(),
               "subscription_until": (now + timedelta(days=30)).isoformat(), "offline_grace_until": (now + timedelta(hours=grace_hours)).isoformat(),
               "minimum_app_version": minimum}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"ca1.{key_id}.{b64(raw)}.{b64(private.sign(raw))}", payload


def service_at(now):
    credentials = WindowsSecureCredentialStore(allow_test_memory=True, namespace="Test/Commercial/License/" + str(uuid.uuid4()))
    storage = SecureLicenseStorage(credentials); installation = storage.installation_id()
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    service = LicenseService(storage, endpoint="http://127.0.0.1:18080", public_keys={"test": b64(public)}, wall_clock=lambda: now)
    return service, private, installation


def save_entitlement(service, private, installation, now, **kwargs):
    signed, payload = token(private, "test", installation, now, **kwargs)
    service.storage.save({"entitlement_token": signed, "refresh_credential": "r" * 64, "payload": payload,
                          "last_known_server_time": now.isoformat(), "last_refresh": now.isoformat(), "last_wall_time": now.isoformat()})
    return signed


def test_commercial_starts_unactivated_and_paid_feature_is_blocked():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, _, _ = service_at(now)
    assert service.status().state is LicenseState.NOT_ACTIVATED
    with pytest.raises(LicenseClientError): FeatureGate(AppEdition.COMMERCIAL, service).require("shorts_analysis")


def test_developer_feature_gate_never_calls_backend():
    class Exploding:
        def status(self): raise AssertionError("backend called")
    FeatureGate(AppEdition.DEVELOPER, Exploding()).require("anything")


def test_valid_signed_entitlement_unlocks_only_listed_features():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, private, installation = service_at(now)
    save_entitlement(service, private, installation, now)
    gate = FeatureGate(AppEdition.COMMERCIAL, service); gate.require("shorts_analysis")
    with pytest.raises(LicenseClientError): gate.require("autopilot")


def test_damaged_signature_and_wrong_installation_are_rejected():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, private, installation = service_at(now)
    signed = save_entitlement(service, private, installation, now)
    stored = service.storage.load(); stored["entitlement_token"] = signed[:-2] + "AA"; service.storage.save(stored)
    assert not service.status().active
    other, _, _ = service_at(now); other.storage.save({"entitlement_token": signed, "refresh_credential": "r" * 64})
    assert not other.status().active


def test_offline_grace_and_end_of_grace():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc); current = [start]
    service, private, installation = service_at(start); service.wall_clock = lambda: current[0]
    save_entitlement(service, private, installation, start)
    current[0] = start + timedelta(hours=49)
    assert service.status(server_available=False).state is LicenseState.OFFLINE_GRACE
    current[0] = start + timedelta(hours=73)
    assert service.status(server_available=False).state is LicenseState.SERVER_UNAVAILABLE
    assert not service.status(server_available=False).active


def test_clock_rollback_requires_online_check():
    start = datetime(2026, 1, 2, tzinfo=timezone.utc); service, private, installation = service_at(start)
    save_entitlement(service, private, installation, start)
    service.wall_clock = lambda: start - timedelta(hours=2)
    assert service.status().state is LicenseState.SERVER_UNAVAILABLE


def test_minimum_version_blocks_only_paid_work(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, private, installation = service_at(now)
    save_entitlement(service, private, installation, now, minimum="99.0.0")
    assert service.status().state is LicenseState.UPDATE_REQUIRED


def test_secure_storage_keeps_tokens_out_of_settings():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, private, installation = service_at(now)
    signed = save_entitlement(service, private, installation, now)
    settings = {"commercial_setup": {}, "shorts_ai": {}}
    assert signed not in json.dumps(settings) and "refresh_credential" not in json.dumps(settings)


class Response:
    def __init__(self, value, status=200, headers=None):
        self.value = value; self.status = status; self.headers = headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.value).encode()


def test_successful_activation_persists_verified_token():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, private, installation = service_at(now)
    signed, payload = token(private, "test", installation, now)
    service.opener = lambda request, timeout=0: Response({"entitlement_token": signed, "refresh_credential": "x" * 64,
        "subscription": {"plan": "beta"}, "device": {"id": "device"}, "server_time": now.isoformat()})
    assert service.activate("CA-AAAA-BBBB-CCCC").active
    assert service.storage.load()["refresh_credential"] == "x" * 64


def test_logout_clears_secure_storage_when_server_unavailable():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, private, installation = service_at(now)
    save_entitlement(service, private, installation, now)
    service.opener = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
    service.logout(); assert service.status().state is LicenseState.NOT_ACTIVATED


def test_license_diagnostics_are_safe_and_capture_success():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, _, _ = service_at(now)
    service.opener = lambda request, timeout=0: Response(
        {"status": "ok"}, headers={"X-Request-ID": "request-123"},
    )
    assert service._request("GET", "/health", timeout=8)["status"] == "ok"
    diagnostic = service.last_diagnostics
    assert diagnostic.hostname == "127.0.0.1"
    assert diagnostic.path == "/health"
    assert diagnostic.timeout_seconds == 8
    assert diagnostic.http_status == 200
    assert diagnostic.request_id == "request-123"
    assert "credential" not in diagnostic.safe_text().casefold()


def test_http_error_is_not_reported_as_server_unavailable():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, _, _ = service_at(now)
    body = json.dumps({"detail": "validation failed", "request_id": "request-422"}).encode()
    service.opener = lambda *args, **kwargs: (_ for _ in ()).throw(
        urllib.error.HTTPError(
            "http://127.0.0.1/v1/licenses/activate", 422, "bad", {},
            __import__("io").BytesIO(body),
        )
    )
    with pytest.raises(LicenseClientError) as caught:
        service._request("POST", "/v1/licenses/activate", {})
    assert caught.value.code == "HTTP_422"
    assert caught.value.request_id == "request-422"
    assert service.last_diagnostics.http_status == 422


def test_timeout_is_distinct_from_other_transport_failures():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, _, _ = service_at(now)
    service.opener = lambda *args, **kwargs: (_ for _ in ()).throw(
        urllib.error.URLError(socket.timeout("timed out"))
    )
    with pytest.raises(LicenseClientError) as caught:
        service._request("GET", "/health", timeout=8)
    assert caught.value.code == "REQUEST_TIMEOUT"
    assert service.last_diagnostics.timeout_seconds == 8


def test_free_quota_client_uses_server_reservation_and_finish(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, _, _ = service_at(now)
    service.storage.save({"refresh_credential": "r" * 64})
    service.status = lambda: type("Status", (), {"plan": "free_channel"})()
    calls = []
    def request(method, path, payload=None, credential="", timeout=20):
        calls.append((method, path, payload, credential))
        return {"reservation_id": "reservation-1", "status": "RESERVED"}
    monkeypatch.setattr(service, "_request", request)
    reservation = service.acquire_free_quota("PROJECT", "operation-key-123")
    service.finish_free_quota(reservation, True)
    assert reservation == "reservation-1"
    assert calls[0][1] == "/v1/licenses/free/quotas/acquire"
    assert calls[1][1] == "/v1/licenses/free/quotas/reservation-1/finish"
    assert calls[1][2] == {"success": True}


def test_paid_client_does_not_contact_free_quota_endpoint(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc); service, _, _ = service_at(now)
    service.status = lambda: type("Status", (), {"plan": "beta"})()
    monkeypatch.setattr(service, "_request", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")))
    assert service.acquire_free_quota("PROJECT", "operation-key-123") == ""


def test_live_free_ui_verifier_captures_server_quota_text(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    from creator_assistant.main import _free_status_ui_verification

    QApplication.instance() or QApplication([])
    monkeypatch.setenv("CREATOR_ASSISTANT_E2E_LIVE_FREE", "1")
    status = SimpleNamespace(
        state=LicenseState.ACTIVE,
        plan="free_channel",
        active=True,
        expires_at="2030-01-01T00:00:00Z",
        offline_grace_until="2030-01-02T00:00:00Z",
        last_refresh="2026-08-10T00:00:00Z",
        message="",
    )
    service = SimpleNamespace(
        status=lambda: status,
        free_status=lambda: {
            "projects": {"used": 2, "limit": 2},
            "shorts_sources": {"used": 0, "limit": 2},
            "devices": {"used": 1, "limit": 1},
        },
        last_diagnostics=SimpleNamespace(safe_text=lambda: "HTTP 200"),
    )
    report_path = tmp_path / "free-ui.json"

    _free_status_ui_verification(SimpleNamespace(licensing=service), report_path)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["success"] is True
    assert "проекты 2 из 2" in report["status_text"]
    assert "Shorts-источники 0 из 2" in report["status_text"]
    assert "устройства 1 из 1" in report["status_text"]
