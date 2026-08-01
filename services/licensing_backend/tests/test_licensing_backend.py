from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.api import manager, settings
from app.config import load_settings
from app.db import SessionLocal
from app.models import ActivationCode, ActivationCodeStatus, Device, DeviceStatus, LicenseEvent, LicenseSession, Plan, SessionStatus, Subscription, SubscriptionStatus, User, UserStatus, utcnow
from app.schemas import ActivateRequest, RefreshRequest
from app.security import EntitlementSigner, verify_token
from app.service import LicenseError


def provision(days=30, ttl=30):
    with SessionLocal() as db:
        user = User(email=f"friend-{uuid.uuid4().hex}@example.test"); db.add(user); db.flush()
        plan = db.scalar(select(Plan).where(Plan.code == "beta")); subscription = manager.grant(db, user, plan, days)
        code = manager.new_code(db, subscription, ttl); ids = user.id, subscription.id; db.commit(); return (*ids, code)


def activate(code, installation="11111111-1111-4111-8111-111111111111"):
    with SessionLocal() as db:
        result = manager.activate(db, ActivateRequest(activation_code=code, installation_id=installation,
            device_name="Test PC", os_version="Windows", app_version="0.1.0", edition="commercial")); db.commit(); return result


def test_successful_subscription_code_and_activation():
    user_id, subscription_id, code = provision()
    result = activate(code)
    payload = verify_token(result["entitlement_token"], {settings.signing_key_id: manager.signer.public_key_b64()})
    assert payload["subscription_id"] == subscription_id
    assert payload["features"] == ["project_preparation", "shorts_analysis", "vertical_editor", "batch_render", "local_ai_profiles", "templates", "brand_assets"]
    with SessionLocal() as db:
        row = db.scalar(select(ActivationCode)); assert row.status is ActivationCodeStatus.USED
        assert row.code_hash != code and code not in row.code_hash


def test_activation_code_cannot_be_reused():
    *_, code = provision(); activate(code)
    with pytest.raises(LicenseError) as error: activate(code, "22222222-2222-4222-8222-222222222222")
    assert error.value.code == "ACTIVATION_CODE_USED"


def test_expired_and_revoked_codes_are_rejected():
    *_, code = provision()
    with SessionLocal() as db:
        row = db.scalar(select(ActivationCode)); row.expires_at = utcnow() - timedelta(seconds=1); db.commit()
    with pytest.raises(LicenseError) as error: activate(code)
    assert error.value.code == "ACTIVATION_CODE_EXPIRED"
    *_, code2 = provision()
    with SessionLocal() as db:
        rows = db.scalars(select(ActivationCode).order_by(ActivationCode.created_at.desc())).all(); rows[0].status = ActivationCodeStatus.REVOKED; db.commit()
    with pytest.raises(LicenseError) as error: activate(code2)
    assert error.value.code == "ACTIVATION_CODE_REVOKED"


def test_device_limit_is_enforced():
    user_id, subscription_id, code = provision(); activate(code)
    with SessionLocal() as db:
        subscription = db.get(Subscription, subscription_id); code2 = manager.new_code(db, subscription); db.commit()
    with pytest.raises(LicenseError) as error: activate(code2, "22222222-2222-4222-8222-222222222222")
    assert error.value.code == "DEVICE_LIMIT_REACHED"
    assert len(error.value.details["devices"]) == 1


def test_matching_code_locks_after_too_many_failed_attempts():
    _, subscription_id, first_code = provision(); activate(first_code)
    with SessionLocal() as db:
        code = manager.new_code(db, db.get(Subscription, subscription_id)); db.commit()
    request = ActivateRequest(activation_code=code, installation_id="33333333-3333-4333-8333-333333333333",
                              device_name="Second PC", os_version="Windows", app_version="0.1.0", edition="commercial")
    for _ in range(settings.max_code_attempts):
        with SessionLocal() as db:
            with pytest.raises(LicenseError): manager.activate(db, request)
            db.commit()
    with SessionLocal() as db, pytest.raises(LicenseError) as error:
        manager.activate(db, request)
    assert error.value.code == "TOO_MANY_ATTEMPTS"


def test_refresh_rotates_session_and_rejects_old_credential():
    *_, code = provision(); result = activate(code); credential = result["refresh_credential"]
    with SessionLocal() as db:
        refreshed = manager.refresh(db, RefreshRequest(refresh_credential=credential,
            installation_id="11111111-1111-4111-8111-111111111111", app_version="0.1.0")); db.commit()
    assert refreshed["refresh_credential"] != credential
    with SessionLocal() as db, pytest.raises(LicenseError) as error:
        manager.refresh(db, RefreshRequest(refresh_credential=credential,
            installation_id="11111111-1111-4111-8111-111111111111", app_version="0.1.0"))
    assert error.value.code == "REFRESH_SESSION_INVALID"


def test_refresh_rejects_expired_subscription_and_wrong_device():
    _, subscription_id, code = provision(); result = activate(code)
    with SessionLocal() as db:
        subscription = db.get(Subscription, subscription_id); subscription.expires_at = utcnow() - timedelta(seconds=1); db.commit()
    with SessionLocal() as db, pytest.raises(LicenseError) as error:
        manager.refresh(db, RefreshRequest(refresh_credential=result["refresh_credential"],
            installation_id="11111111-1111-4111-8111-111111111111", app_version="0.1.0"))
    assert error.value.code == "SUBSCRIPTION_INACTIVE"


def test_deactivation_revokes_device_sessions():
    *_, code = provision(); result = activate(code)
    with SessionLocal() as db:
        session = manager.session_from_refresh(db, result["refresh_credential"]); manager.deactivate(db, session, session.device_id); db.commit()
    with SessionLocal() as db:
        assert db.scalar(select(Device)).status is DeviceStatus.DEACTIVATED
        assert db.scalar(select(LicenseSession)).status is SessionStatus.REVOKED


def test_blocked_user_and_device_are_rejected():
    user_id, subscription_id, code = provision()
    with SessionLocal() as db: db.get(User, user_id).status = UserStatus.BLOCKED; db.commit()
    with pytest.raises(LicenseError) as error: activate(code)
    assert error.value.code == "SUBSCRIPTION_INACTIVE"
    with SessionLocal() as db: db.get(User, user_id).status = UserStatus.ACTIVE; db.commit()
    result = activate(code)
    with SessionLocal() as db:
        db.scalar(select(Device)).status = DeviceStatus.BLOCKED; db.commit()
    with SessionLocal() as db, pytest.raises(LicenseError) as error:
        manager.refresh(db, RefreshRequest(refresh_credential=result["refresh_credential"], installation_id="11111111-1111-4111-8111-111111111111", app_version="0.1.0"))
    assert error.value.code == "DEVICE_REVOKED"


def test_signing_key_rotation_and_tampering():
    payload = {"schema_version": 1, "value": "ok"}; old = EntitlementSigner("old", bytes(range(32))); new = EntitlementSigner("new", bytes(range(1, 33)))
    token = old.sign(payload)
    assert verify_token(token, {"old": old.public_key_b64(), "new": new.public_key_b64()})["value"] == "ok"
    with pytest.raises(ValueError): verify_token(token[:-2] + "AA", {"old": old.public_key_b64()})


def test_audit_log_contains_no_code_or_token():
    *_, code = provision(); activate(code)
    with SessionLocal() as db:
        event = db.scalar(select(LicenseEvent)); encoded = str(event.event_metadata)
        assert code not in encoded and "token" not in encoded.casefold()


def test_admin_api_requires_auth_and_issues_code_once(client, admin_headers):
    assert client.post("/v1/admin/users", json={"email": "x@example.test"}).status_code == 401
    user_id = client.post("/v1/admin/users", headers=admin_headers, json={"email": "x@example.test"}).json()["id"]
    assert client.post("/v1/admin/subscriptions/grant", headers=admin_headers, json={"user_id": user_id, "plan_code": "beta", "days": 30}).status_code == 200
    response = client.post("/v1/admin/activation-codes", headers=admin_headers, json={"user_id": user_id, "ttl_minutes": 30})
    assert response.status_code == 200 and response.json()["activation_code"].startswith("CA-")


def test_admin_grant_is_idempotent(client, admin_headers):
    user_id = client.post("/v1/admin/users", headers=admin_headers, json={"email": "idempotent@example.test"}).json()["id"]
    payload = {"user_id": user_id, "plan_code": "beta", "days": 30, "idempotency_key": "grant-once"}
    first = client.post("/v1/admin/subscriptions/grant", headers=admin_headers, json=payload)
    second = client.post("/v1/admin/subscriptions/grant", headers=admin_headers, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["idempotent_replay"] is True


def test_public_api_rate_limit_and_health(client):
    assert client.get("/health").json() == {
        "status": "ok", "version": "0.3.1-beta.5", "commit": "test-release-commit",
    }
    assert client.get("/ready").json() == {
        "status": "ready", "version": "0.3.1-beta.5", "commit": "test-release-commit",
    }
    assert client.get("/version").json() == {
        "version": "0.3.1-beta.5", "commit": "test-release-commit", "environment": "test",
    }
    payload = {"activation_code": "CA-AAAA-BBBB-CCCC", "installation_id": "11111111-1111-4111-8111-111111111111",
               "device_name": "PC", "os_version": "Windows", "app_version": "0.1.0", "edition": "commercial"}
    statuses = [client.post("/v1/licenses/activate", json=payload).status_code for _ in range(13)]
    assert 429 in statuses


def test_production_profile_rejects_sqlite_and_missing_secrets(monkeypatch):
    monkeypatch.setenv("LICENSE_ENV", "production")
    monkeypatch.setenv("LICENSE_DATABASE_URL", "sqlite:///forbidden.db")
    monkeypatch.setenv("LICENSE_ACTIVATION_PEPPER", "short")
    monkeypatch.setenv("LICENSE_SIGNING_PRIVATE_KEY", "")
    with pytest.raises(RuntimeError):
        load_settings()
