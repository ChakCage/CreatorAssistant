"""Local/VPS smoke for invite -> subscription -> code -> activation -> refresh.

The script never prints invite, activation, refresh or entitlement secrets.
Run only inside the protected API container.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import beta_access, settings
from app.db import SessionLocal
from app.security import mint_service_token


def main() -> int:
    if settings.environment not in {"staging", "test"}:
        raise SystemExit("Refusing beta E2E outside staging/test")
    telegram_id = "e2e-" + secrets.token_hex(6)
    with SessionLocal() as db:
        _, invite_code = beta_access.create_invite(
            db, valid_days=1, max_uses=1, subscription_days=1,
            device_limit=1, description="automated staging E2E", admin_id="e2e",
        )
        db.commit()
    token = mint_service_token(
        "staging-e2e", ["beta:redeem", "activation:create"],
        "creator_assistant", settings.bot_service_secret,
    )
    headers = {"Authorization": "Bearer " + token}
    base_url = os.getenv("STAGING_E2E_API_URL", "http://127.0.0.1:8080")
    installation_id = "staging-e2e-installation-" + secrets.token_hex(12)
    with httpx.Client(base_url=base_url, timeout=20) as client:
        redeem = client.post("/v1/bot/beta/redeem", headers=headers, json={
            "telegram_user_id": telegram_id, "invite_code": invite_code,
        })
        redeem.raise_for_status()
        code_response = client.post("/v1/bot/activation-code", headers=headers, json={
            "telegram_user_id": telegram_id,
        })
        code_response.raise_for_status()
        activation = client.post("/v1/licenses/activate", json={
            "activation_code": code_response.json()["activation_code"],
            "installation_id": installation_id, "device_name": "Staging E2E",
            "os_version": "Linux test", "app_version": "0.3.0", "edition": "commercial",
        })
        activation.raise_for_status()
        refresh = client.post("/v1/licenses/refresh", json={
            "refresh_credential": activation.json()["refresh_credential"],
            "installation_id": installation_id, "app_version": "0.3.0",
        })
        refresh.raise_for_status()
    print(json.dumps({
        "invite_redeemed": redeem.json()["status"] == "ACTIVE",
        "activation": activation.json()["device"]["status"],
        "refresh": refresh.json()["device"]["status"],
        "secrets_printed": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
