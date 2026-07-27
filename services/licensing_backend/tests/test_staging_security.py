import pytest

from app.config import load_settings
from app.redaction import redact, redact_text


def test_log_redaction_hides_codes_tokens_and_named_secrets():
    text = redact_text("Authorization: Bearer abc.def CA-ABCD-EFGH-IJKL BETA-ABCD-EFGH")
    assert "abc.def" not in text
    assert "CA-ABCD" not in text
    assert "BETA-ABCD" not in text
    value = redact({"telegram_user_id": "42", "private_key": "secret", "activation_code": "CA-FOO"})
    assert value["telegram_user_id"] == "42"
    assert value["private_key"] == "[REDACTED]"
    assert value["activation_code"] == "[REDACTED]"


def test_invalid_content_length_is_rejected_without_traceback(client):
    response = client.get("/health", headers={"content-length": "invalid"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CONTENT_LENGTH"


def test_payments_can_be_disabled_in_staging_contract():
    from app.api import settings
    assert settings.environment == "test"
    assert settings.payments_enabled is True


def test_staging_refuses_payments_and_placeholder_secrets(monkeypatch):
    monkeypatch.setenv("LICENSE_ENV", "staging")
    monkeypatch.setenv("LICENSE_DATABASE_URL", "postgresql+psycopg://user:pass@postgres/db")
    monkeypatch.setenv("LICENSE_PUBLIC_BASE_URL", "https://api-staging.example.test")
    monkeypatch.setenv("LICENSE_ACTIVATION_PEPPER", "a" * 40)
    monkeypatch.setenv("LICENSE_BOT_SERVICE_SECRET", "b" * 40)
    monkeypatch.setenv("LICENSE_ADMIN_TOKEN_HASH", "c" * 64)
    monkeypatch.setenv("LICENSE_PAYMENTS_ENABLED", "false")
    assert load_settings().payments_enabled is False
    monkeypatch.setenv("LICENSE_PAYMENTS_ENABLED", "true")
    with pytest.raises(RuntimeError, match="Payments must remain disabled"):
        load_settings()
    monkeypatch.setenv("LICENSE_PAYMENTS_ENABLED", "false")
    monkeypatch.setenv("LICENSE_ACTIVATION_PEPPER", "change-me-" + "a" * 32)
    with pytest.raises(RuntimeError, match="placeholder"):
        load_settings()
