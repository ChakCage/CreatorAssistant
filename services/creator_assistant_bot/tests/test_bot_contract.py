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
    assert callbacks == {"plans", "subscription", "activation", "devices", "download", "help", "support"}


@pytest.mark.asyncio
async def test_backend_unavailable_is_reported_as_transport_failure():
    async def handler(_request: httpx.Request): raise httpx.ConnectError("offline")
    client = BackendClient(BotSettings(service_secret="x" * 32), httpx.MockTransport(handler))
    with pytest.raises(httpx.ConnectError): await client.plans()
    await client.close()
