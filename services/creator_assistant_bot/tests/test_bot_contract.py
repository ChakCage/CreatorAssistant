from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from bot.auth import BOT_PERMISSIONS
from bot.backend import BackendClient
from bot.config import BotSettings
from bot.handlers import is_admin_user, telegram_membership_snapshot
from bot.main import deliver_notification
from bot.ui import (
    STAGING_PAYMENT_DISABLED_TEXT,
    DOWNLOAD_WARNING_TEXT,
    PUBLIC_HELP_TEXT,
    devices_text,
    free_status_text,
    localized_status,
    main_menu,
    plans_text,
    russian_date,
    russian_days,
    subscription_text,
)


@pytest.mark.asyncio
async def test_bot_uses_scoped_short_lived_identity_and_backend_owned_price():
    seen = []
    async def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/v1/bot/plans":
            return httpx.Response(200, json={"plans": [{"name": "Beta", "amount_minor": 99000, "currency": "RUB", "duration_days": 30, "device_limit": 1, "features": []}]})
        return httpx.Response(404)
    settings = BotSettings(service_secret="x" * 32)
    client = BackendClient(settings, httpx.MockTransport(handler))
    result = await client.plans(); await client.close()
    assert result["plans"][0]["amount_minor"] == 99000
    token = seen[0].headers["authorization"].split()[1]
    assert token.startswith("bs1.")
    assert "admin" not in BOT_PERMISSIONS


def test_plan_copy_formats_minor_units_without_float_contract():
    text = plans_text([{"name": "Beta", "amount_minor": 99000, "currency": "RUB", "duration_days": 30, "device_limit": 1, "features": ["shorts"]}])
    assert "990.00 RUB" in text


def test_bot_release_metadata_comes_from_settings():
    settings = BotSettings(
        service_secret="x" * 32,
        release_version="0.3.1-beta.4",
        release_commit="test-release-commit",
    )
    assert settings.release_version == "0.3.1-beta.4"
    assert settings.release_commit == "test-release-commit"


def test_start_menu_exposes_every_required_action():
    menu = main_menu()
    assert [len(row) for row in menu.inline_keyboard] == [2, 2, 2, 2, 1]
    callbacks = {button.callback_data for row in menu.inline_keyboard for button in row}
    assert callbacks == {
        "free_offer", "beta_access", "subscription", "activation", "devices", "download",
        "help", "feedback", "support",
    }


@pytest.mark.parametrize(("status", "expected"), [
    ("ACTIVE", "✅ Подписка активна"),
    ("EXPIRED", "⛔ Подписка закончилась"),
    ("CANCELLED", "🚫 Подписка отменена"),
    ("OFFLINE_GRACE", "🟡 Временный офлайн-доступ"),
    ("unexpected", "⚪ Статус подписки неизвестен"),
])
def test_subscription_statuses_are_localized_without_exposing_enums(status, expected):
    assert localized_status(status) == expected


def test_russian_date_accepts_utc_timezone_legacy_and_missing_values():
    assert russian_date("2026-08-11T14:34:53.260379Z") == "11 августа 2026 года"
    assert russian_date("2026-08-11T17:34:53+03:00") == "11 августа 2026 года"
    assert russian_date("legacy-invalid") == ""
    assert russian_date(None) == ""


@pytest.mark.parametrize(("days", "expected"), [
    (1, "1 день"), (2, "2 дня"), (5, "5 дней"), (11, "11 дней"), (21, "21 день"),
])
def test_russian_days_uses_correct_declension(days, expected):
    assert russian_days(days) == expected


def test_subscription_copy_formats_date_remaining_expired_and_no_expiration():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    active = subscription_text({"status": "ACTIVE", "expires_at": "2026-08-12T00:00:00Z"}, now=now)
    assert active == "✅ Подписка активна\nДействует до: 12 августа 2026 года\nОсталось: 11 дней"
    expired = subscription_text({"status": "EXPIRED", "expires_at": "2026-07-31T00:00:00Z"}, now=now)
    assert "Срок истёк" in expired
    assert subscription_text({"status": "ACTIVE"}, now=now).endswith("Срок: бессрочно")
    assert subscription_text({"status": "ACTIVE", "expires_at": "legacy"}, now=now).endswith("Дата окончания: не указана")


@pytest.mark.parametrize(("payload", "expected"), [
    ({"devices": [], "device_limit": 1}, "Использовано устройств: 0 из 1"),
    ({"devices": [{"name": "PC", "status": "ACTIVE"}], "device_limit": 1}, "Использовано устройств: 1 из 1"),
    ({"devices": [{"name": "PC 1", "status": "ACTIVE"}, {"name": "PC 2", "status": "ACTIVE"}], "device_limit": 5}, "Использовано устройств: 2 из 5"),
    ({"devices": []}, "Использовано устройств: 0"),
])
def test_device_counter_handles_limits_and_missing_limit(payload, expected):
    assert devices_text(payload).splitlines()[0] == expected


def test_device_copy_localizes_state_and_never_exposes_id():
    text = devices_text({"device_limit": 2, "devices": [
        {"id": "internal-secret-id", "name": "Рабочий ПК", "status": "ACTIVE"},
        {"id": "old-id", "name": "Ноутбук", "status": "DEACTIVATED"},
    ]})
    assert "Рабочий ПК — активно" in text
    assert "Ноутбук — отключено" in text
    assert "internal-secret-id" not in text and "old-id" not in text


def test_free_status_uses_backend_owned_counters():
    text = free_status_text({
        "state": "ACTIVE", "projects": {"used": 1, "limit": 2},
        "shorts_sources": {"used": 0, "limit": 2}, "devices": {"used": 1, "limit": 1},
        "membership": {"status": "member"},
    })
    assert "Подготовка проектов: 1 из 2" in text
    assert "Исходные видео для Shorts: 0 из 2" in text
    assert "Подписка на канал: подтверждена" in text


class _Member:
    def __init__(self, status, **rights):
        self.status = status
        self.is_member = rights.pop("is_member", None)
        for key, value in rights.items(): setattr(self, key, value)


class _MembershipBot:
    async def get_me(self): return type("Me", (), {"id": 101})()
    async def get_chat_member(self, _chat_id, user_id):
        if user_id == 101: return _Member("administrator", can_post_messages=True, can_invite_users=True)
        return _Member("restricted", is_member=True)


@pytest.mark.asyncio
async def test_membership_snapshot_checks_bot_admin_and_user_status():
    status, is_member, rights = await telegram_membership_snapshot(_MembershipBot(), -100123456, 42)
    assert status == "restricted" and is_member is True
    assert rights["can_post_messages"] is True and rights["can_invite_users"] is True


@pytest.mark.asyncio
async def test_membership_snapshot_refuses_non_admin_bot():
    bot = _MembershipBot()
    async def not_admin(_chat_id, user_id): return _Member("member")
    bot.get_chat_member = not_admin
    with pytest.raises(RuntimeError, match="FREE_CHANNEL_BOT_NOT_ADMIN"):
        await telegram_membership_snapshot(bot, -100123456, 42)


def test_staging_purchase_copy_disables_payments_without_provider_link():
    assert "Оплата в тестовой версии пока отключена" in STAGING_PAYMENT_DISABLED_TEXT
    assert "приглашение администратора" in STAGING_PAYMENT_DISABLED_TEXT
    assert "http" not in STAGING_PAYMENT_DISABLED_TEXT


def test_download_warning_is_fully_localized_and_help_stays_inside_bot():
    assert DOWNLOAD_WARNING_TEXT == (
        "⚠️ Тестовая сборка пока не имеет цифровой подписи.\n"
        "Windows SmartScreen может показать предупреждение."
    )
    assert "UNSIGNED BETA" not in DOWNLOAD_WARNING_TEXT
    assert "http" not in PUBLIC_HELP_TEXT and "t.me/" not in PUBLIC_HELP_TEXT


class _FakeNotificationBackend:
    def __init__(self):
        self.ticket = {
            "id": "ticket-id", "number": "CA-12345678", "telegram_user_id": "424403653",
            "category": "application", "message": "Не работает",
            "attachment": {"type": "photo", "name": "screen.jpg", "size": 2048},
        }

    async def support_ticket(self, ticket_id: str):
        assert ticket_id == "ticket-id"
        return self.ticket


class _FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text, kwargs))


@pytest.mark.asyncio
async def test_support_notification_targets_only_configured_admin_with_actions():
    bot = _FakeBot()
    await deliver_notification(
        bot, _FakeNotificationBackend(),
        {"type": "SUPPORT_TICKET_CREATED", "payload": {"ticket_id": "ticket-id"}},
        mock=False, admin_telegram_id=424403653,
    )
    assert len(bot.calls) == 1
    chat_id, text, kwargs = bot.calls[0]
    assert chat_id == 424403653
    assert "CA-12345678" in text
    assert "Работа приложения" in text
    assert "Telegram ID 424403653" in text
    assert "Не работает" in text
    assert "file_id" not in text
    callbacks = [button.callback_data for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert callbacks == ["support_reply:ticket-id", "support_close:ticket-id", "support_block:ticket-id"]


@pytest.mark.asyncio
async def test_support_notification_refuses_missing_admin_id():
    with pytest.raises(RuntimeError, match="ADMIN_TELEGRAM_ID"):
        await deliver_notification(
            _FakeBot(), _FakeNotificationBackend(),
            {"type": "SUPPORT_TICKET_CREATED", "payload": {"ticket_id": "ticket-id"}},
            mock=False, admin_telegram_id=0,
        )


def test_production_bot_accepts_only_explicit_internal_http_and_rejects_placeholders():
    valid = BotSettings(
        environment="production", token="123456:" + "A" * 35,
        backend_url="http://api:8080", allow_internal_backend_http=True,
        service_secret="s" * 40, public_url="https://bot-staging.example.test",
        webhook_secret="w" * 32,
    )
    valid.validate_runtime()
    with pytest.raises(RuntimeError, match="HTTPS"):
        valid.model_copy(update={"allow_internal_backend_http": False}).validate_runtime()
    with pytest.raises(RuntimeError, match="placeholder"):
        valid.model_copy(update={"service_secret": "change-me-" + "x" * 32}).validate_runtime()


def test_staging_admin_section_is_explicit_and_support_has_no_personal_username():
    settings = BotSettings(service_secret="x" * 32, admin_telegram_id=424403653)
    assert settings.admin_telegram_id == 424403653
    assert is_admin_user(settings, 424403653)
    assert not is_admin_user(settings, 421403653)
    assert not is_admin_user(settings, 999999999)
    assert "Chak" + "_74" not in settings.support_url


@pytest.mark.asyncio
async def test_beta_invite_is_redeemed_through_scoped_backend_route():
    seen = []
    async def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"status": "ACTIVE", "expires_at": "2026-08-10T00:00:00Z"})
    client = BackendClient(BotSettings(service_secret="x" * 32), httpx.MockTransport(handler))
    result = await client.redeem_beta(42, "BETA-ABCD-EFGH")
    await client.close()
    assert result["status"] == "ACTIVE"
    assert seen[0].url.path == "/v1/bot/beta/redeem"
    assert json.loads(seen[0].content)["invite_code"] == "BETA-ABCD-EFGH"


@pytest.mark.asyncio
async def test_backend_unavailable_is_reported_as_transport_failure():
    async def handler(_request: httpx.Request): raise httpx.ConnectError("offline")
    client = BackendClient(BotSettings(service_secret="x" * 32), httpx.MockTransport(handler))
    with pytest.raises(httpx.ConnectError): await client.plans()
    await client.close()
