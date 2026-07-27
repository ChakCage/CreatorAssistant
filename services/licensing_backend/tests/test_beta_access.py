from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.api import beta_access, settings
from app.db import SessionLocal
from app.models import BetaInvite, BetaInviteUse, Subscription, SubscriptionStatus, User, utcnow
from app.security import mint_service_token


def service_headers(permission: str) -> dict[str, str]:
    token = mint_service_token("test-bot", [permission], "creator_assistant", settings.bot_service_secret)
    return {"Authorization": "Bearer " + token}


def invite(*, uses: int = 1, valid_days: int = 1, subscription_days: int = 14) -> tuple[str, str]:
    with SessionLocal() as db:
        row, code = beta_access.create_invite(
            db, valid_days=valid_days, max_uses=uses,
            subscription_days=subscription_days, device_limit=1,
        )
        db.commit()
        return row.id, code


def redeem(client, telegram_id: str, code: str):
    return client.post(
        "/v1/bot/beta/redeem",
        headers=service_headers("beta:redeem"),
        json={"telegram_user_id": telegram_id, "invite_code": code},
    )


def test_invite_creation_stores_only_hash_and_redemption_grants_subscription(client):
    invite_id, code = invite()
    response = redeem(client, "10001", code)
    assert response.status_code == 200
    assert response.json()["status"] == SubscriptionStatus.ACTIVE.value
    with SessionLocal() as db:
        row = db.get(BetaInvite, invite_id)
        assert row.code_hash != code
        assert code not in row.code_hash
        assert row.used_count == 1
        use = db.scalar(select(BetaInviteUse).where(BetaInviteUse.invite_id == invite_id))
        user = db.scalar(select(User).where(User.telegram_user_id == "10001"))
        subscription = db.get(Subscription, use.subscription_id)
        assert use.user_id == user.id
        assert subscription.plan.device_limit == 1


def test_expired_invite_is_rejected(client):
    invite_id, code = invite()
    with SessionLocal() as db:
        row = db.get(BetaInvite, invite_id)
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    response = redeem(client, "10002", code)
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "BETA_INVITE_EXPIRED"


def test_invite_use_limit_and_duplicate_telegram_redemption(client):
    _, code = invite(uses=1)
    assert redeem(client, "10003", code).status_code == 200
    exhausted = redeem(client, "10004", code)
    assert exhausted.status_code == 409
    assert exhausted.json()["error"]["code"] == "BETA_INVITE_USES_EXHAUSTED"

    _, second_code = invite(uses=2)
    duplicate = redeem(client, "10003", second_code)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "BETA_ALREADY_REDEEMED"


def test_active_beta_subscriber_can_rotate_activation_code(client):
    _, code = invite()
    assert redeem(client, "10005", code).status_code == 200
    headers = service_headers("activation:create")
    first = client.post("/v1/bot/activation-code", headers=headers, json={"telegram_user_id": "10005"})
    second = client.post("/v1/bot/activation-code", headers=headers, json={"telegram_user_id": "10005"})
    assert first.status_code == second.status_code == 200
    assert first.json()["activation_code"] != second.json()["activation_code"]


def test_non_subscriber_cannot_get_activation_code(client):
    response = client.post(
        "/v1/bot/activation-code",
        headers=service_headers("activation:create"),
        json={"telegram_user_id": "19999"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"
