from __future__ import annotations

import httpx

from .auth import service_token
from .config import BotSettings


class BackendClient:
    def __init__(self, settings: BotSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.backend_url, timeout=20, transport=transport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + service_token(self.settings.service_secret)}

    async def _request(self, method: str, path: str, **kwargs):
        response = await self.client.request(method, path, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response.json()

    async def upsert_user(self, telegram_user_id: int, username: str | None, first_name: str | None, language_code: str | None):
        return await self._request("POST", "/v1/bot/users/upsert", json={
            "telegram_user_id": str(telegram_user_id), "username": username,
            "first_name": first_name, "language_code": language_code,
        })

    async def plans(self): return await self._request("GET", "/v1/bot/plans")
    async def ready(self): return await self._request("GET", "/ready")
    async def subscription(self, telegram_user_id: int): return await self._request("GET", f"/v1/bot/subscription/{telegram_user_id}")
    async def activation_code(self, telegram_user_id: int):
        return await self._request("POST", "/v1/bot/activation-code", json={"telegram_user_id": str(telegram_user_id)})
    async def redeem_beta(self, telegram_user_id: int, invite_code: str):
        return await self._request("POST", "/v1/bot/beta/redeem", json={
            "telegram_user_id": str(telegram_user_id), "invite_code": invite_code,
        })
    async def create_checkout(self, telegram_user_id: int, plan_id: str, price_id: str, idempotency_key: str):
        return await self._request("POST", "/v1/billing/checkout", json={
            "telegram_user_id": str(telegram_user_id), "plan_id": plan_id,
            "price_id": price_id, "idempotency_key": idempotency_key,
        })
    async def deactivate_device(self, telegram_user_id: int, device_id: str):
        return await self._request("POST", "/v1/bot/devices/deactivate", json={
            "telegram_user_id": str(telegram_user_id), "device_id": device_id, "confirmed": True,
        })
    async def release(self): return await self._request("GET", "/v1/bot/release")
    async def notifications(self): return await self._request("GET", "/v1/bot/notifications")
    async def notification_result(self, notification_id: str, success: bool, error: str = ""):
        return await self._request("POST", f"/v1/bot/notifications/{notification_id}/result", json={"success": success, "error": error})
    async def close(self): await self.client.aclose()
