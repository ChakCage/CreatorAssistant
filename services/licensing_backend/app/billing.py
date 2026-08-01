from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import Settings
from .models import (
    ActivationCode, ActivationCodeStatus, BotNotification, CheckoutSession, Device,
    DeviceStatus, NotificationStatus, Payment, PaymentEvent, PaymentEventStatus,
    PaymentStatus, Plan, Price, Release, Subscription, SubscriptionSource,
    SubscriptionStatus, User, UserStatus, utcnow,
)
from .payment_provider import FakePaymentProvider, PaymentProvider, WebhookEvent
from .security import keyed_hash
from .service import LicenseError, LicenseManager, aware, iso


class BillingService:
    """Owns checkout and payment state transitions; callers own commit/rollback."""

    def __init__(self, settings: Settings, manager: LicenseManager) -> None:
        self.settings = settings
        self.manager = manager
        self.providers: dict[str, PaymentProvider] = {}
        if not settings.production:
            self.providers["fake"] = FakePaymentProvider(settings.fake_payment_secret, settings.public_base_url)

    def provider(self, name: str) -> PaymentProvider:
        provider = self.providers.get(name)
        if provider is None:
            raise LicenseError("PAYMENT_PROVIDER_UNAVAILABLE", 503)
        return provider

    def upsert_telegram_user(self, db: Session, value) -> User:
        user = db.scalar(select(User).where(User.telegram_user_id == value.telegram_user_id))
        created = user is None
        if created:
            user = User(telegram_user_id=value.telegram_user_id)
            db.add(user)
        user.telegram_username = value.username
        user.telegram_first_name = value.first_name
        user.telegram_language_code = value.language_code
        db.flush()
        self.manager.audit(db, "BOT_USER_UPSERT", "SUCCESS", user_id=user.id,
                           metadata={"created": created, "telegram_user_id": value.telegram_user_id})
        return user

    def plans(self, db: Session) -> list[dict]:
        now = utcnow()
        rows = db.execute(
            select(Plan, Price).join(Price, Price.plan_id == Plan.id).where(
                Plan.is_active.is_(True), Price.is_active.is_(True)
            )
        ).all()
        return [
            {
                "plan_id": plan.id, "plan_code": plan.code, "name": plan.name,
                "duration_days": plan.duration_days, "device_limit": plan.device_limit,
                "features": list(plan.features),
                "price_id": price.id, "provider": price.provider,
                "amount_minor": price.amount_minor, "currency": price.currency,
            }
            for plan, price in rows
            if (price.starts_at is None or aware(price.starts_at) <= now)
            and (price.ends_at is None or aware(price.ends_at) > now)
            and price.provider in self.providers
        ]

    def create_checkout(self, db: Session, value) -> dict:
        user = db.scalar(select(User).where(User.telegram_user_id == value.telegram_user_id))
        if user is None or user.status is UserStatus.DELETED:
            raise LicenseError("USER_NOT_FOUND", 404)
        if user.status is UserStatus.BLOCKED:
            raise LicenseError("USER_BLOCKED", 403)
        replay = db.scalar(select(Payment).where(
            Payment.user_id == user.id, Payment.idempotency_key == value.idempotency_key
        ))
        if replay is not None:
            return self._checkout_response(replay, idempotent=True)
        price = db.get(Price, value.price_id)
        plan = db.get(Plan, value.plan_id)
        now = utcnow()
        if not price or not plan or price.plan_id != plan.id or not price.is_active or not plan.is_active:
            raise LicenseError("PRICE_NOT_AVAILABLE", 409)
        if (price.starts_at and aware(price.starts_at) > now) or (price.ends_at and aware(price.ends_at) <= now):
            raise LicenseError("PRICE_NOT_AVAILABLE", 409)
        provider = self.provider(price.provider)
        payment = Payment(
            user_id=user.id, plan_id=plan.id, price_id=price.id, provider=price.provider,
            idempotency_key=value.idempotency_key, amount_minor=price.amount_minor,
            currency=price.currency.upper(), status=PaymentStatus.CREATED,
            description=f"Creator Assistant {plan.name}, {plan.duration_days} days",
            expires_at=now + timedelta(minutes=self.settings.checkout_ttl_minutes),
        )
        db.add(payment); db.flush()
        token = secrets.token_urlsafe(32)
        db.add(CheckoutSession(
            payment_id=payment.id,
            token_hash=keyed_hash(token, self.settings.fake_payment_secret),
            expires_at=payment.expires_at,
        ))
        checkout = provider.create_checkout(
            payment_id=payment.id, amount_minor=payment.amount_minor,
            currency=payment.currency, description=payment.description,
            checkout_token=token, return_url=self._safe_return_url(value.return_url) or "",
        )
        payment.provider_payment_id = checkout.provider_payment_id
        payment.checkout_url = checkout.checkout_url
        payment.status = PaymentStatus.PENDING
        self.manager.audit(db, "BOT_CHECKOUT", "SUCCESS", user_id=user.id,
                           metadata={"payment_id": payment.id, "plan_id": plan.id})
        db.flush()
        return self._checkout_response(payment)

    def _safe_return_url(self, value: str | None) -> str | None:
        # External redirects are deliberately not accepted by the local fake page.
        if not value or not value.startswith(self.settings.public_base_url + "/"):
            return None
        return value

    @staticmethod
    def _checkout_response(payment: Payment, *, idempotent: bool = False) -> dict:
        return {
            "payment_id": payment.id, "status": payment.status.value,
            "checkout_url": payment.checkout_url, "amount_minor": payment.amount_minor,
            "currency": payment.currency, "expires_at": iso(payment.expires_at),
            "idempotent_replay": idempotent,
        }

    def checkout_by_token(self, db: Session, token: str) -> CheckoutSession:
        digest = keyed_hash(token, self.settings.fake_payment_secret)
        row = db.scalar(select(CheckoutSession).where(CheckoutSession.token_hash == digest))
        if row is None or row.used_at is not None or aware(row.expires_at) <= utcnow():
            raise LicenseError("CHECKOUT_SESSION_INVALID", 410)
        return row

    def checkout_csrf(self, row: CheckoutSession) -> str:
        return keyed_hash(f"checkout:{row.id}:{row.payment_id}", self.settings.fake_payment_secret)

    def process_webhook(self, db: Session, provider_name: str, headers: dict[str, str], body: bytes) -> dict:
        provider = self.provider(provider_name)
        if not provider.verify_webhook(body, headers):
            raise LicenseError("WEBHOOK_SIGNATURE_INVALID", 401)
        event = provider.parse_webhook_event(body)
        duplicate = db.scalar(select(PaymentEvent).where(
            PaymentEvent.provider == provider_name,
            (PaymentEvent.provider_event_id == event.provider_event_id) | (PaymentEvent.provider_nonce == event.nonce),
        ))
        if duplicate:
            return {"status": "duplicate", "event_id": event.provider_event_id}
        payment = db.scalar(select(Payment).where(
            Payment.provider == provider_name,
            Payment.provider_payment_id == event.provider_payment_id,
        ).with_for_update())
        record = PaymentEvent(
            payment_id=payment.id if payment else None, provider=provider_name,
            provider_event_id=event.provider_event_id, event_type=event.event_type,
            provider_nonce=event.nonce,
            signature_valid=True, payload_hash=hashlib.sha256(body).hexdigest(),
        )
        db.add(record); db.flush()
        if payment is None:
            record.processing_status = PaymentEventStatus.REJECTED
            record.error_code = "PAYMENT_NOT_FOUND"
            raise LicenseError("PAYMENT_NOT_FOUND", 404)
        if (event.payment_id != payment.id or event.amount_minor != payment.amount_minor
                or event.currency.upper() != payment.currency.upper()):
            record.processing_status = PaymentEventStatus.REVIEW_REQUIRED
            record.error_code = "PAYMENT_AMOUNT_MISMATCH"
            payment.payment_metadata = {**payment.payment_metadata, "requires_review": True}
            return {"status": "review_required", "event_id": event.provider_event_id}
        if event.event_type == "payment.succeeded":
            result = self._mark_paid(db, payment, record)
        elif event.event_type == "payment.cancelled":
            payment.status = PaymentStatus.CANCELLED; payment.cancelled_at = utcnow()
            record.processing_status = PaymentEventStatus.PROCESSED
            result = "cancelled"
        elif event.event_type == "payment.refunded":
            payment.status = PaymentStatus.REFUNDED; payment.refunded_at = utcnow()
            record.processing_status = PaymentEventStatus.PROCESSED
            result = "refunded"
        else:
            record.processing_status = PaymentEventStatus.REJECTED
            record.error_code = "UNSUPPORTED_EVENT"
            result = "ignored"
        record.processed_at = utcnow()
        return {"status": result, "event_id": event.provider_event_id}

    def _mark_paid(self, db: Session, payment: Payment, record: PaymentEvent) -> str:
        if payment.status is PaymentStatus.PAID:
            record.processing_status = PaymentEventStatus.DUPLICATE
            return "duplicate"
        if payment.status in {PaymentStatus.CANCELLED, PaymentStatus.EXPIRED, PaymentStatus.REFUNDED}:
            record.processing_status = PaymentEventStatus.REVIEW_REQUIRED
            record.error_code = "PAYMENT_TERMINAL_STATE"
            return "review_required"
        blocked = db.scalar(select(Subscription).where(
            Subscription.user_id == payment.user_id,
            Subscription.product_id == payment.plan.product_id,
            Subscription.status == SubscriptionStatus.BLOCKED,
        ))
        if blocked or payment.user.status is UserStatus.BLOCKED:
            record.processing_status = PaymentEventStatus.REVIEW_REQUIRED
            record.error_code = "SUBSCRIPTION_BLOCKED"
            payment.payment_metadata = {**payment.payment_metadata, "requires_review": True}
            return "review_required"
        payment.status = PaymentStatus.PAID; payment.paid_at = utcnow()
        subscription = self.manager.grant(
            db, payment.user, payment.plan, payment.plan.duration_days,
            SubscriptionSource.PAYMENT,
        )
        subscription.source = SubscriptionSource.PAYMENT
        subscription.external_reference = payment.id
        self.manager.audit(
            db, "PAYMENT", "SUCCESS", user_id=payment.user_id,
            subscription_id=subscription.id,
            metadata={"payment_id": payment.id, "provider": payment.provider},
        )
        self._notify(db, payment, subscription)
        record.processing_status = PaymentEventStatus.PROCESSED
        return "paid"

    @staticmethod
    def _notify(db: Session, payment: Payment, subscription: Subscription) -> None:
        if not payment.user.telegram_user_id:
            return
        db.add(BotNotification(
            user_id=payment.user_id, telegram_user_id=payment.user.telegram_user_id,
            notification_type="PAYMENT_SUCCEEDED",
            payload={"payment_id": payment.id, "expires_at": iso(subscription.expires_at)},
            dedupe_key=f"payment-paid:{payment.id}",
        ))

    def subscription(self, db: Session, telegram_user_id: str) -> dict:
        user = db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if not user:
            return {"status": "NONE", "devices": []}
        sub = self._preferred_subscription(db, user.id, active_only=False)
        if not sub:
            return {"status": "NONE", "devices": []}
        devices = db.scalars(select(Device).where(Device.subscription_id == sub.id)).all()
        return {
            "subscription_id": sub.id, "status": sub.status.value,
            "plan": sub.plan.code, "expires_at": iso(sub.expires_at),
            "device_limit": sub.plan.device_limit,
            "devices": [{"id": d.id, "name": d.friendly_name, "status": d.status.value} for d in devices],
        }

    def create_activation_code(self, db: Session, telegram_user_id: str) -> dict:
        user = db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if not user:
            raise LicenseError("USER_NOT_FOUND", 404)
        sub = self._preferred_subscription(db, user.id, active_only=True)
        if not sub or aware(sub.expires_at) <= utcnow():
            raise LicenseError("SUBSCRIPTION_INACTIVE", 409)
        db.execute(update(ActivationCode).where(
            ActivationCode.subscription_id == sub.id,
            ActivationCode.status == ActivationCodeStatus.CREATED,
        ).values(status=ActivationCodeStatus.REVOKED))
        code = self.manager.new_code(db, sub, 30)
        self.manager.audit(db, "BOT_ACTIVATION_CODE", "SUCCESS", user_id=user.id, subscription_id=sub.id)
        return {"activation_code": code, "expires_in_minutes": 30}

    @staticmethod
    def _preferred_subscription(db: Session, user_id: str, *, active_only: bool) -> Subscription | None:
        statement = select(Subscription).where(Subscription.user_id == user_id)
        if active_only:
            statement = statement.where(Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE]))
        rows = db.scalars(statement.order_by(Subscription.expires_at.desc())).all()
        # A paid/beta subscription always wins over the long-lived FREE carrier subscription.
        return next((row for row in rows if row.plan.code != "free_channel"), rows[0] if rows else None)

    def deactivate_device(self, db: Session, telegram_user_id: str, device_id: str, confirmed: bool) -> dict:
        if not confirmed:
            raise LicenseError("CONFIRMATION_REQUIRED", 409)
        user = db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        device = db.get(Device, device_id)
        if not user or not device or device.user_id != user.id:
            raise LicenseError("DEVICE_NOT_FOUND", 404)
        device.status = DeviceStatus.DEACTIVATED; device.deactivated_at = utcnow()
        from .models import LicenseSession, SessionStatus
        db.execute(update(LicenseSession).where(
            LicenseSession.device_id == device.id,
            LicenseSession.status == SessionStatus.ACTIVE,
        ).values(status=SessionStatus.REVOKED, revoked_at=utcnow()))
        self.manager.audit(db, "BOT_DEVICE_DEACTIVATE", "SUCCESS", user_id=user.id,
                           subscription_id=device.subscription_id, device_id=device.id)
        return {"status": "DEACTIVATED"}

    @staticmethod
    def active_release(db: Session) -> dict:
        row = db.scalar(select(Release).where(
            Release.edition == "commercial", Release.channel == "stable", Release.is_active.is_(True),
        ).order_by(Release.published_at.desc()))
        if not row:
            raise LicenseError("RELEASE_NOT_FOUND", 404)
        return {"version": row.version, "download_url": row.download_url, "sha256": row.sha256,
                "release_notes": row.release_notes, "published_at": iso(row.published_at)}

    @staticmethod
    def claim_notifications(db: Session, limit: int = 20) -> list[BotNotification]:
        rows = db.scalars(select(BotNotification).where(
            BotNotification.status.in_([NotificationStatus.PENDING, NotificationStatus.FAILED, NotificationStatus.PROCESSING]),
            BotNotification.next_attempt_at <= utcnow(),
        ).order_by(BotNotification.created_at).limit(min(limit, 100)).with_for_update(skip_locked=True)).all()
        for row in rows:
            row.status = NotificationStatus.PROCESSING
            row.next_attempt_at = utcnow() + timedelta(minutes=5)
        return rows

    @staticmethod
    def finish_notification(row: BotNotification, success: bool, error: str = "") -> None:
        row.attempts += 1
        if success:
            row.status = NotificationStatus.SENT; row.sent_at = utcnow(); row.last_error = None
        else:
            row.status = NotificationStatus.FAILED; row.last_error = error[:500]
            row.next_attempt_at = utcnow() + timedelta(seconds=min(3600, 2 ** min(row.attempts, 10)))
