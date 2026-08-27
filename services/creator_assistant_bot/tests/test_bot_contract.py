from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from bot.auth import BOT_PERMISSIONS
from bot.backend import BackendClient
from bot.config import BotSettings
from bot.handlers import (
    MAIN_MENU_CALLBACK,
    MAIN_MENU_PRESERVE_CALLBACK,
    edit_menu_or_answer,
    handle_menu_message,
    handle_main_menu_callback,
    is_admin_user,
    support_category_keyboard,
    support_user_keyboard,
    telegram_membership_snapshot,
    with_main_menu,
)
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


@pytest.mark.asyncio
async def test_free_config_is_requested_for_the_current_telegram_user():
    seen = []
    async def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"enabled": True})
    client = BackendClient(BotSettings(service_secret="x" * 32), httpx.MockTransport(handler))
    result = await client.free_config(70031)
    await client.close()
    assert result["enabled"] is True
    assert seen[0].url.params["telegram_user_id"] == "70031"


def test_plan_copy_formats_minor_units_without_float_contract():
    text = plans_text([{"name": "Beta", "amount_minor": 99000, "currency": "RUB", "duration_days": 30, "device_limit": 1, "features": ["shorts"]}])
    assert "990.00 RUB" in text


def test_bot_release_metadata_comes_from_settings():
    settings = BotSettings(
        service_secret="x" * 32,
        release_version="0.3.1-beta.5",
        release_commit="test-release-commit",
    )
    assert settings.release_version == "0.3.1-beta.5"
    assert settings.release_commit == "test-release-commit"


def test_start_menu_exposes_every_required_action():
    menu = main_menu()
    assert [len(row) for row in menu.inline_keyboard] == [2, 2, 2, 2]
    callbacks = {button.callback_data for row in menu.inline_keyboard for button in row}
    assert callbacks == {
        "beta_access", "subscription", "activation", "devices", "download",
        "help", "feedback", "support",
    }
    assert all("Получить тестовый доступ" not in button.text for row in menu.inline_keyboard for button in row)


def test_free_and_admin_actions_are_exposed_only_when_explicitly_enabled():
    ordinary = {button.callback_data for row in main_menu().inline_keyboard for button in row}
    admin = {button.callback_data for row in main_menu(is_admin=True).inline_keyboard for button in row}
    free = {button.callback_data for row in main_menu(free_enabled=True).inline_keyboard for button in row}
    assert "free_offer" not in ordinary and "admin_panel" not in ordinary
    assert "admin_panel" in admin and "free_offer" in free


def test_nested_user_screens_offer_main_menu_and_historical_flows_preserve_messages():
    ordinary = with_main_menu()
    support = support_user_keyboard()
    categories = support_category_keyboard()
    assert ordinary.inline_keyboard[-1][0].callback_data == MAIN_MENU_CALLBACK
    assert support.inline_keyboard[-1][0].callback_data == MAIN_MENU_PRESERVE_CALLBACK
    assert categories.inline_keyboard[-1][0].callback_data == MAIN_MENU_PRESERVE_CALLBACK
    assert all(markup.inline_keyboard[-1][0].text == "🏠 Главное меню"
               for markup in (ordinary, support, categories))


class _MenuMessage:
    def __init__(self, *, edit_fails=False):
        self.edited = []; self.sent = []; self.edit_fails = edit_fails

    async def edit_text(self, text, **kwargs):
        if self.edit_fails:
            raise AttributeError("message cannot be edited")
        self.edited.append((text, kwargs))

    async def answer(self, text, **kwargs): self.sent.append((text, kwargs))


class _MenuState:
    def __init__(self): self.cleared = 0
    async def clear(self): self.cleared += 1


class _MenuBackend:
    def __init__(self, *, free=True): self.free = free; self.users = []
    async def upsert_user(self, *args): self.users.append(args)
    async def free_config(self, _user_id): return {"enabled": self.free}


class _MenuCallback:
    def __init__(self, data=MAIN_MENU_CALLBACK, *, edit_fails=False, user_id=101):
        self.data = data; self.message = _MenuMessage(edit_fails=edit_fails); self.answers = []
        self.from_user = type("User", (), {
            "id": user_id, "username": "user", "first_name": "User", "language_code": "ru",
        })()
    async def answer(self, *args, **kwargs): self.answers.append((args, kwargs))


@pytest.mark.asyncio
async def test_main_menu_callback_clears_fsm_for_free_user_and_edits_existing_menu():
    callback = _MenuCallback(); state = _MenuState()
    outcome = await handle_main_menu_callback(
        callback, state, _MenuBackend(free=True), BotSettings(service_secret="x" * 32),
    )
    assert outcome == "edited" and state.cleared == 1 and len(callback.answers) == 1
    callbacks = {button.callback_data for row in callback.message.edited[0][1]["reply_markup"].inline_keyboard
                 for button in row}
    assert "free_offer" in callbacks


@pytest.mark.asyncio
async def test_menu_command_handler_clears_fsm_and_returns_main_menu():
    message = _MenuMessage()
    message.from_user = type("User", (), {
        "id": 101, "username": "user", "first_name": "User", "language_code": "ru",
    })()
    state = _MenuState()
    await handle_menu_message(
        message, state, _MenuBackend(free=True), BotSettings(service_secret="x" * 32),
    )
    assert state.cleared == 1 and len(message.sent) == 1
    assert message.sent[0][0].startswith("Creator Assistant")


@pytest.mark.asyncio
async def test_main_menu_callback_supports_admin_and_falls_back_when_edit_fails():
    settings = BotSettings(service_secret="x" * 32, admin_telegram_id=424403653)
    callback = _MenuCallback(edit_fails=True, user_id=424403653); state = _MenuState()
    outcome = await handle_main_menu_callback(callback, state, _MenuBackend(free=False), settings)
    assert outcome == "sent" and not callback.message.edited and len(callback.message.sent) == 1
    callbacks = {button.callback_data for row in callback.message.sent[0][1]["reply_markup"].inline_keyboard
                 for button in row}
    assert "admin_panel" in callbacks


@pytest.mark.asyncio
async def test_preserving_main_menu_callback_does_not_edit_support_history():
    callback = _MenuCallback(data=MAIN_MENU_PRESERVE_CALLBACK); state = _MenuState()
    outcome = await handle_main_menu_callback(
        callback, state, _MenuBackend(), BotSettings(service_secret="x" * 32),
    )
    assert outcome == "sent" and state.cleared == 1
    assert callback.message.edited == [] and len(callback.message.sent) == 1


@pytest.mark.asyncio
async def test_edit_menu_helper_falls_back_to_new_message():
    message = _MenuMessage(edit_fails=True)
    assert await edit_menu_or_answer(message, "Меню") == "sent"
    assert message.sent[0][0] == "Меню"


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

    async def support_dashboard(self):
        return {"new": 1, "waiting_admin": 2, "answered": 3, "message_id": 0}

    async def save_support_dashboard_message(self, admin_id: int, message_id: int, **_kwargs):
        self.saved = (admin_id, message_id)


class _FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text, kwargs))
        return type("Sent", (), {"message_id": 77})()


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
    assert "Центр поддержки" in text
    assert "Ждут моего ответа: 2" in text
    assert "file_id" not in text
    callbacks = [button.callback_data for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert callbacks == [
        "support_admin_list:NEW:1", "support_admin_list:WAITING_ADMIN:1",
        "support_admin_list:WAITING_USER:1", "support_admin_list:CLOSED:1",
        "support_admin_list:BLOCKED:1", "support_admin_list:ALL:1", "support_dashboard_open",
    ]


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
