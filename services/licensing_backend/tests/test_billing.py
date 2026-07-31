from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.api import billing, manager, settings
from app.db import SessionLocal
from app.models import ActivationCode, ActivationCodeStatus, BotNotification, Payment, PaymentStatus, Price, Subscription, SubscriptionStatus, User, utcnow
from app.security import mint_service_token
from app.service import aware


PERMISSIONS = ["users:write", "plans:read", "checkout:create", "subscription:read", "activation:create", "devices:write", "release:read", "notifications:read", "notifications:write"]


def service_headers(permission: str | None = None):
    permissions = [permission] if permission else PERMISSIONS
    token = mint_service_token("test-bot", permissions, "creator_assistant", settings.bot_service_secret)
    return {"Authorization": "Bearer " + token}


def prepare(client):
    client.post("/v1/bot/users/upsert", headers=service_headers("users:write"), json={"telegram_user_id": "42", "username": "tester"})
    with SessionLocal() as db:
        _, plan = manager.bootstrap(db)
        price = db.scalar(select(Price).where(Price.plan_id == plan.id, Price.provider == "fake"))
        if price is None:
            price = Price(plan_id=plan.id, provider="fake", amount_minor=99000, currency="RUB")
            db.add(price)
        db.commit()
        return plan.id, price.id


def checkout(client, key="idem-payment-0001"):
    plan_id, price_id = prepare(client)
    response = client.post("/v1/billing/checkout", headers=service_headers("checkout:create"), json={
        "telegram_user_id": "42", "plan_id": plan_id, "price_id": price_id, "idempotency_key": key,
    })
    assert response.status_code == 200, response.text
    return response.json()


def event_for(payment: Payment, event_type="payment.succeeded", event_id="evt-stable", amount=None):
    provider = billing.provider("fake")
    return provider.signed_event(payment_id=payment.id, provider_payment_id=payment.provider_payment_id,
                                 event_type=event_type, amount_minor=amount or payment.amount_minor,
                                 currency=payment.currency, event_id=event_id)


def test_checkout_is_idempotent_and_price_is_backend_owned(client):
    first = checkout(client)
    with SessionLocal() as db:
        payment = db.get(Payment, first["payment_id"])
        value = {"telegram_user_id": "42", "plan_id": payment.plan_id, "price_id": payment.price_id,
                 "idempotency_key": "idem-payment-0001"}
    second = client.post("/v1/billing/checkout", headers=service_headers("checkout:create"), json=value)
    assert second.status_code == 200
    assert second.json()["payment_id"] == first["payment_id"]
    assert second.json()["amount_minor"] == 99000
    assert second.json()["idempotent_replay"] is True


def test_repeated_telegram_start_does_not_create_duplicate(client):
    headers = service_headers("users:write")
    for username in ("first", "second"):
        assert client.post("/v1/bot/users/upsert", headers=headers, json={"telegram_user_id": "777", "username": username}).status_code == 200
    with SessionLocal() as db:
        assert len(db.scalars(select(User).where(User.telegram_user_id == "777")).all()) == 1
        assert db.scalar(select(User).where(User.telegram_user_id == "777")).telegram_username == "second"


def test_paid_webhook_extends_once_and_queues_notification(client):
    value = checkout(client)
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"]); body, headers = event_for(payment)
    first = client.post("/v1/billing/webhooks/fake", content=body, headers=headers)
    second = client.post("/v1/billing/webhooks/fake", content=body, headers=headers)
    assert first.json()["status"] == "paid"
    assert second.json()["status"] == "duplicate"
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"])
        subscriptions = db.scalars(select(Subscription).where(Subscription.user_id == payment.user_id)).all()
        assert payment.status is PaymentStatus.PAID
        assert len(subscriptions) == 1
        assert subscriptions[0].source.value == "PAYMENT"
        assert db.scalar(select(BotNotification).where(BotNotification.dedupe_key == f"payment-paid:{payment.id}"))


def test_existing_active_time_is_preserved_on_payment(client):
    value = checkout(client)
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"])
        subscription = manager.grant(db, payment.user, payment.plan, 10)
        old_expiry = subscription.expires_at
        body, headers = event_for(payment, event_id="evt-extension")
        db.commit()
    response = client.post("/v1/billing/webhooks/fake", content=body, headers=headers)
    assert response.status_code == 200
    with SessionLocal() as db:
        row = db.scalar(select(Subscription))
        assert aware(row.expires_at) >= aware(old_expiry) + timedelta(days=29, hours=23)


def test_invalid_signature_and_amount_mismatch_do_not_activate(client):
    value = checkout(client)
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"])
        body, headers = event_for(payment, event_id="evt-bad-signature")
    headers["x-payment-signature"] = "0" * 64
    assert client.post("/v1/billing/webhooks/fake", content=body, headers=headers).status_code == 401
    with SessionLocal() as db: assert db.get(Payment, value["payment_id"]).status is PaymentStatus.PENDING
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"])
        body, headers = event_for(payment, event_id="evt-wrong-amount", amount=payment.amount_minor + 1)
    response = client.post("/v1/billing/webhooks/fake", content=body, headers=headers)
    assert response.json()["status"] == "review_required"
    with SessionLocal() as db: assert db.get(Payment, value["payment_id"]).status is PaymentStatus.PENDING


def test_blocked_subscription_requires_manual_review(client):
    value = checkout(client)
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"])
        subscription = Subscription(user_id=payment.user_id, product_id=payment.plan.product_id,
                                    plan_id=payment.plan_id, status=SubscriptionStatus.BLOCKED,
                                    starts_at=utcnow(), expires_at=utcnow() + timedelta(days=10), source="ADMIN")
        db.add(subscription); db.commit()
        body, headers = event_for(payment, event_id="evt-blocked")
    assert client.post("/v1/billing/webhooks/fake", content=body, headers=headers).json()["status"] == "review_required"


def test_cancel_and_refund_transitions(client):
    value = checkout(client)
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"]); body, headers = event_for(payment, "payment.cancelled", "evt-cancel")
    assert client.post("/v1/billing/webhooks/fake", content=body, headers=headers).json()["status"] == "cancelled"
    with SessionLocal() as db: assert db.get(Payment, value["payment_id"]).status is PaymentStatus.CANCELLED
    value = checkout(client, "idem-refund-0002")
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"]); body, headers = event_for(payment, "payment.refunded", "evt-refund")
    assert client.post("/v1/billing/webhooks/fake", content=body, headers=headers).json()["status"] == "refunded"
    with SessionLocal() as db: assert db.get(Payment, value["payment_id"]).status is PaymentStatus.REFUNDED


def test_activation_code_rotation_keeps_only_one_active_code(client):
    value = checkout(client)
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"]); body, headers = event_for(payment, event_id="evt-code")
    client.post("/v1/billing/webhooks/fake", content=body, headers=headers)
    headers = service_headers("activation:create")
    assert client.post("/v1/bot/activation-code", headers=headers, json={"telegram_user_id": "42"}).status_code == 200
    assert client.post("/v1/bot/activation-code", headers=headers, json={"telegram_user_id": "42"}).status_code == 200
    with SessionLocal() as db:
        values = db.scalars(select(ActivationCode)).all()
        assert sum(item.status is ActivationCodeStatus.CREATED for item in values) == 1
        assert sum(item.status is ActivationCodeStatus.REVOKED for item in values) == 1


def test_bot_subscription_includes_backend_owned_device_limit(client):
    value = checkout(client)
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"])
        body, headers = event_for(payment, event_id="evt-device-limit")
        expected_limit = payment.plan.device_limit
    assert client.post("/v1/billing/webhooks/fake", content=body, headers=headers).status_code == 200
    response = client.get("/v1/bot/subscription/42", headers=service_headers("subscription:read"))
    assert response.status_code == 200
    assert response.json()["device_limit"] == expected_limit


def test_service_token_is_scoped(client):
    checkout(client)
    response = client.get("/v1/bot/plans", headers=service_headers("users:write"))
    assert response.status_code == 401


def test_notification_claim_is_atomic_and_acknowledged(client):
    value = checkout(client)
    with SessionLocal() as db:
        payment = db.get(Payment, value["payment_id"]); body, headers = event_for(payment, event_id="evt-notification")
    assert client.post("/v1/billing/webhooks/fake", content=body, headers=headers).status_code == 200
    claimed = client.get("/v1/bot/notifications", headers=service_headers("notifications:read"))
    assert claimed.status_code == 200
    item = claimed.json()["notifications"][0]
    assert client.get("/v1/bot/notifications", headers=service_headers("notifications:read")).json()["notifications"] == []
    result = client.post(f"/v1/bot/notifications/{item['id']}/result",
                         headers=service_headers("notifications:write"), json={"success": True})
    assert result.json()["status"] == "SENT"
