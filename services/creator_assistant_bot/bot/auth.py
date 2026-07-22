from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


BOT_PERMISSIONS = [
    "users:write", "plans:read", "checkout:create", "subscription:read",
    "activation:create", "devices:write", "release:read",
    "notifications:read", "notifications:write",
]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def service_token(secret: str, ttl_seconds: int = 300) -> str:
    now = int(time.time())
    payload = {
        "identity": "creator-assistant-telegram-bot", "permissions": BOT_PERMISSIONS,
        "product_scope": "creator_assistant", "iat": now,
        "exp": now + min(max(ttl_seconds, 30), 600), "nonce": secrets.token_urlsafe(12),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    return f"bs1.{_b64(raw)}.{_b64(signature)}"
