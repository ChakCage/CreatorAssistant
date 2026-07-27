from __future__ import annotations

import json

import httpx
import pytest

from bot.auth import BOT_PERMISSIONS
from bot.backend import BackendClient
from bot.config import BotSettings
from bot.ui import main_menu, plans_text


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


def test_start_menu_exposes_every_required_action():
    callbacks = {button.callback_data for row in main_menu().inline_keyboard for button in row}
    assert callbacks == {
        "beta_access", "subscription", "activation", "devices", "download",
        "help", "feedback", "support",
    }


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
