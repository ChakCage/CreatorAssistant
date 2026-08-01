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
    async def free_config(self): return await self._request("GET", "/v1/bot/free/config")
    async def free_status(self, telegram_user_id: int):
        return await self._request("GET", f"/v1/bot/free/status/{telegram_user_id}")
    async def free_membership(self, telegram_user_id: int, status: str, is_member: bool | None = None):
        return await self._request("POST", "/v1/bot/free/membership", json={
            "telegram_user_id": str(telegram_user_id), "status": status, "is_member": is_member,
        })
    async def free_event(self, telegram_user_id: int, event: str, result: str = "SUCCESS", reason: str = ""):
        return await self._request("POST", "/v1/bot/free/events", json={
            "telegram_user_id": str(telegram_user_id), "event": event, "result": result, "reason": reason,
        })
    async def free_admin_entitlements(self, admin_id: int):
        return await self._request("GET", "/v1/bot/free/admin/entitlements", params={"telegram_user_id": str(admin_id)})
    async def free_admin_config(self, admin_id: int, values: dict):
        return await self._request("POST", "/v1/bot/free/admin/config", json={"telegram_user_id": str(admin_id), **values})
    async def free_admin_block(self, admin_id: int, entitlement_id: str, blocked: bool):
        return await self._request("POST", f"/v1/bot/free/admin/entitlements/{entitlement_id}/block", json={
            "telegram_user_id": str(admin_id), "blocked": blocked,
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
    async def create_support_ticket(self, telegram_user_id: int, category: str, message: str, attachment: dict | None = None):
        return await self._request("POST", "/v1/bot/support/tickets", json={
            "telegram_user_id": str(telegram_user_id), "category": category,
            "message": message, "attachment": attachment or {},
        })
    async def support_tickets(self, status: str = "OPEN"):
        return await self._request("GET", "/v1/bot/support/tickets", params={"status": status})
    async def support_ticket(self, ticket_id: str):
        return await self._request("GET", f"/v1/bot/support/tickets/{ticket_id}")
    async def reply_support_ticket(self, admin_id: int, ticket_id: str, message: str):
        return await self._request("POST", f"/v1/bot/support/tickets/{ticket_id}/reply", json={
            "telegram_user_id": str(admin_id), "message": message,
        })
    async def close_support_ticket(self, admin_id: int, ticket_id: str):
        return await self._request("POST", f"/v1/bot/support/tickets/{ticket_id}/close", json={
            "telegram_user_id": str(admin_id),
        })
    async def block_support_user(self, admin_id: int, ticket_id: str):
        return await self._request("POST", f"/v1/bot/support/tickets/{ticket_id}/block", json={
            "telegram_user_id": str(admin_id), "message": "spam",
        })
    async def close(self): await self.client.aclose()
