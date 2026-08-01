from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api import free_access, manager, settings
from app.db import SessionLocal
from app.models import (
    AdminAction, FreeAccessState, FreeEntitlement, FreeQuotaKind, FreeQuotaUseStatus,
    Subscription, SubscriptionSource, SubscriptionStatus, User, utcnow,
)
from app.security import mint_service_token


def service_headers(permission: str):
    token = mint_service_token("test-bot", [permission], "creator_assistant", settings.bot_service_secret)
    return {"Authorization": "Bearer " + token}


def add_user(value: str = "70001") -> User:
    with SessionLocal() as db:
        user = User(telegram_user_id=value, telegram_username="tester")
        db.add(user); db.commit(); db.refresh(user)
        return user


@pytest.mark.parametrize(("status", "is_member", "allowed"), [
    ("creator", None, True), ("administrator", None, True), ("member", None, True),
    ("restricted", True, True), ("restricted", False, False), ("left", None, False),
    ("kicked", None, False), ("banned", None, False),
])
def test_membership_status_policy(status, is_member, allowed):
    assert free_access.membership_allowed(status, is_member) is allowed


def test_free_config_exposes_server_side_channel_username():
    with SessionLocal() as db:
        config = free_access.config(db)
    assert "channel_username" in config


def test_free_grant_is_idempotent_and_unsubscribe_resume_preserves_usage():
    user = add_user()
    with SessionLocal() as db:
        row = free_access.membership_result(db, db.get(User, user.id), status="member", is_member=None)
        first_id, subscription_id = row.id, row.subscription_id
        db.commit()
    with SessionLocal() as db:
        row = free_access.membership_result(db, db.get(User, user.id), status="member", is_member=None)
        assert row.id == first_id and row.subscription_id == subscription_id
        assert len(db.scalars(select(Subscription).where(Subscription.user_id == user.id)).all()) == 1
        reservation = free_access.acquire_quota(db, user.id, FreeQuotaKind.PROJECT, "project-operation-0001")
        free_access.finish_quota(db, user.id, reservation.id, True)
        db.commit()
    with SessionLocal() as db:
        row = free_access.membership_result(db, db.get(User, user.id), status="left", is_member=False)
        assert row.state is FreeAccessState.PAUSED_UNSUBSCRIBED and row.projects_used == 1
        row = free_access.membership_result(db, db.get(User, user.id), status="member", is_member=True)
        assert row.state is FreeAccessState.ACTIVE and row.projects_used == 1


def test_quota_reservation_commit_release_and_idempotency():
    user = add_user("70002")
    with SessionLocal() as db:
        free_access.membership_result(db, db.get(User, user.id), status="member", is_member=True)
        first = free_access.acquire_quota(db, user.id, FreeQuotaKind.SHORTS_SOURCE, "source-operation-0001")
        replay = free_access.acquire_quota(db, user.id, FreeQuotaKind.SHORTS_SOURCE, "source-operation-0001")
        assert replay.id == first.id
        free_access.finish_quota(db, user.id, first.id, False)
        assert first.status is FreeQuotaUseStatus.RELEASED
        retry = free_access.acquire_quota(db, user.id, FreeQuotaKind.SHORTS_SOURCE, "source-operation-0001")
        free_access.finish_quota(db, user.id, retry.id, True)
        free_access.finish_quota(db, user.id, retry.id, True)
        assert db.get(FreeEntitlement, retry.entitlement_id).shorts_sources_used == 1


def test_two_projects_succeed_and_third_is_blocked_without_limiting_shorts_outputs():
    user = add_user("70003")
    with SessionLocal() as db:
        free_access.membership_result(db, db.get(User, user.id), status="member", is_member=True)
        for index in range(2):
            if index:
                free_access.membership_result(db, db.get(User, user.id), status="member", is_member=True)
            use = free_access.acquire_quota(db, user.id, FreeQuotaKind.PROJECT, f"project-operation-{index:04d}")
            free_access.finish_quota(db, user.id, use.id, True)
        with pytest.raises(Exception) as exc:
            free_access.acquire_quota(db, user.id, FreeQuotaKind.PROJECT, "project-operation-0003")
        assert getattr(exc.value, "code", "") == "FREE_QUOTA_EXHAUSTED"
        # Rendered Shorts are deliberately not represented as a quota kind.
        assert {item.value for item in FreeQuotaKind} == {"PROJECT", "SHORTS_SOURCE"}


def test_outstanding_reservations_cannot_oversubscribe_quota():
    user = add_user("70007")
    with SessionLocal() as db:
        free_access.membership_result(db, db.get(User, user.id), status="member", is_member=True)
        first = free_access.acquire_quota(db, user.id, FreeQuotaKind.PROJECT, "parallel-project-0001")
        second = free_access.acquire_quota(db, user.id, FreeQuotaKind.PROJECT, "parallel-project-0002")
        assert first.id != second.id
        with pytest.raises(Exception) as exc:
            free_access.acquire_quota(db, user.id, FreeQuotaKind.PROJECT, "parallel-project-0003")
        assert getattr(exc.value, "code", "") == "FREE_QUOTA_EXHAUSTED"


def test_paid_subscription_has_priority_over_free_gate():
    user = add_user("70004")
    with SessionLocal() as db:
        row = free_access.membership_result(db, db.get(User, user.id), status="member", is_member=True)
        _, paid_plan = manager.bootstrap(db)
        paid = Subscription(user_id=user.id, product_id=paid_plan.product_id, plan_id=paid_plan.id,
                            status=SubscriptionStatus.ACTIVE, starts_at=utcnow(),
                            expires_at=utcnow() + __import__("datetime").timedelta(days=30),
                            source=SubscriptionSource.PAYMENT)
        db.add(paid); db.flush()
        payload = free_access.payload(db, db.get(User, user.id))
        assert payload["state"] == "CONVERTED_TO_PAID"
        assert row.subscription_id != paid.id


def test_bot_routes_save_server_config_and_enforce_admin_id(client):
    add_user("70005")
    headers = service_headers("free:admin")
    denied = client.post("/v1/bot/free/admin/config", headers=headers, json={
        "telegram_user_id": "999", "project_limit": 3,
    })
    assert denied.status_code == 403
    changed = client.post("/v1/bot/free/admin/config", headers=headers, json={
        "telegram_user_id": str(settings.free_access_admin_telegram_id),
        "channel_username": "@chak_kazak", "project_limit": 3,
    })
    assert changed.status_code == 200
    assert changed.json()["project_limit"] == 3
    assert changed.json()["channel_username"] == "@chak_kazak"
    invalid = client.post("/v1/bot/free/admin/config", headers=headers, json={
        "telegram_user_id": str(settings.free_access_admin_telegram_id),
        "channel_username": "https://t.me/chak_kazak",
    })
    assert invalid.status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(AdminAction).where(AdminAction.action == "update-free-access-settings"))


def test_membership_route_grants_only_once_and_returns_server_quota(client):
    add_user("70006")
    headers = service_headers("free:write")
    payload = {"telegram_user_id": "70006", "status": "member", "is_member": True}
    first = client.post("/v1/bot/free/membership", headers=headers, json=payload)
    second = client.post("/v1/bot/free/membership", headers=headers, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["projects"] == {"used": 0, "limit": 2}
