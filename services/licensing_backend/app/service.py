from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .config import Settings
from .models import (
    ActivationCode, ActivationCodeStatus, AdminAction, Device, DeviceStatus, LicenseEvent,
    LicenseSession, Plan, Product, ProductStatus, SessionStatus, Subscription,
    SubscriptionSource, SubscriptionStatus, User, UserStatus, utcnow,
)
from .security import EntitlementSigner, activation_code, opaque_hash, secret_hash


FEATURES = ["project_preparation", "shorts_analysis", "vertical_editor", "batch_render", "local_ai_profiles", "templates", "brand_assets"]


class LicenseError(RuntimeError):
    def __init__(self, code: str, status: int = 400, details: dict | None = None) -> None:
        super().__init__(code); self.code = code; self.status = status; self.details = details or {}


def iso(value: datetime) -> str:
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class LicenseManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.signer = EntitlementSigner(settings.signing_key_id, settings.signing_private_key)

    def bootstrap(self, db: Session) -> tuple[Product, Plan]:
        product = db.scalar(select(Product).where(Product.code == "creator_assistant"))
        if not product:
            product = Product(code="creator_assistant", name="Creator Assistant", status=ProductStatus.ACTIVE); db.add(product); db.flush()
        plan = db.scalar(select(Plan).where(Plan.product_id == product.id, Plan.code == "beta"))
        if not plan:
            plan = Plan(product_id=product.id, code="beta", name="Creator Assistant Beta", duration_days=30, device_limit=1, features=FEATURES); db.add(plan); db.flush()
        return product, plan

    def audit(self, db: Session, event_type: str, result: str, *, reason: str = "", user_id: str | None = None,
              subscription_id: str | None = None, device_id: str | None = None, ip: str = "", user_agent: str = "", metadata: dict | None = None) -> None:
        ip_hash = hashlib.sha256((ip + self.settings.activation_pepper).encode()).hexdigest() if ip else ""
        safe_metadata = {k: v for k, v in (metadata or {}).items() if "token" not in k.lower() and "code" not in k.lower()}
        db.add(LicenseEvent(user_id=user_id, subscription_id=subscription_id, device_id=device_id,
                            event_type=event_type, result=result, reason_code=reason, ip_hash=ip_hash,
                            user_agent=user_agent[:300], event_metadata=safe_metadata))

    def grant(self, db: Session, user: User, plan: Plan, days: int | None = None, source: SubscriptionSource = SubscriptionSource.ADMIN) -> Subscription:
        now = utcnow(); duration = days or plan.duration_days
        existing = db.scalar(select(Subscription).where(Subscription.user_id == user.id, Subscription.product_id == plan.product_id,
                                                         Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE])))
        if existing:
            existing.expires_at = max(aware(existing.expires_at), now) + timedelta(days=duration); existing.status = SubscriptionStatus.ACTIVE
            return existing
        subscription = Subscription(user_id=user.id, product_id=plan.product_id, plan_id=plan.id,
                                    status=SubscriptionStatus.ACTIVE, starts_at=now, expires_at=now + timedelta(days=duration), source=source)
        db.add(subscription); db.flush(); return subscription

    def new_code(self, db: Session, subscription: Subscription, ttl_minutes: int | None = None) -> str:
        code = activation_code()
        db.add(ActivationCode(subscription_id=subscription.id, code_hash=secret_hash(code, self.settings.activation_pepper),
                              expires_at=utcnow() + timedelta(minutes=ttl_minutes or self.settings.activation_ttl_minutes)))
        db.flush(); return code

    def _active_subscription(self, subscription: Subscription) -> None:
        now = utcnow()
        if subscription.status not in {SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE} or aware(subscription.expires_at) <= now:
            if aware(subscription.expires_at) <= now: subscription.status = SubscriptionStatus.EXPIRED
            raise LicenseError("SUBSCRIPTION_INACTIVE", 403)
        if subscription.user.status is UserStatus.BLOCKED: raise LicenseError("SUBSCRIPTION_INACTIVE", 403)
        if (subscription.plan.code == "free_channel"
                and not self.settings.free_access_eligible(subscription.user.telegram_user_id)):
            raise LicenseError("FREE_ACCESS_INACTIVE", 403)

    def activate(self, db: Session, request, *, ip: str = "", user_agent: str = "") -> dict[str, Any]:
        now = utcnow(); code_hash = secret_hash(request.activation_code, self.settings.activation_pepper)
        row = db.scalar(select(ActivationCode).where(ActivationCode.code_hash == code_hash).with_for_update())
        if not row:
            self.audit(db, "ACTIVATE", "DENIED", reason="INVALID_ACTIVATION_CODE", ip=ip, user_agent=user_agent)
            raise LicenseError("INVALID_ACTIVATION_CODE")
        if row.attempts >= self.settings.max_code_attempts: raise LicenseError("TOO_MANY_ATTEMPTS", 429)
        row.attempts += 1
        if row.status is ActivationCodeStatus.USED: raise LicenseError("ACTIVATION_CODE_USED", 409)
        if row.status is ActivationCodeStatus.REVOKED: raise LicenseError("ACTIVATION_CODE_REVOKED", 410)
        if aware(row.expires_at) <= now:
            row.status = ActivationCodeStatus.EXPIRED; raise LicenseError("ACTIVATION_CODE_EXPIRED", 410)
        subscription = row.subscription; self._active_subscription(subscription)
        active_count = db.scalar(select(func.count(Device.id)).where(Device.subscription_id == subscription.id, Device.status == DeviceStatus.ACTIVE)) or 0
        device = db.scalar(select(Device).where(Device.subscription_id == subscription.id, Device.installation_id == request.installation_id))
        if (not device or device.status is not DeviceStatus.ACTIVE) and active_count >= subscription.plan.device_limit:
            known_devices = [
                {"id": item.id, "name": item.friendly_name, "status": item.status.value,
                 "last_seen_at": iso(item.last_seen_at)}
                for item in db.scalars(select(Device).where(Device.subscription_id == subscription.id)).all()
            ]
            raise LicenseError("DEVICE_LIMIT_REACHED", 409, {"devices": known_devices})
        if not device:
            device = Device(user_id=subscription.user_id, subscription_id=subscription.id, installation_id=request.installation_id,
                            friendly_name=request.device_name, os_version=request.os_version, app_version=request.app_version, edition=request.edition)
            db.add(device); db.flush()
        elif device.status is DeviceStatus.BLOCKED: raise LicenseError("DEVICE_REVOKED", 403)
        else:
            device.status = DeviceStatus.ACTIVE; device.deactivated_at = None; device.last_seen_at = now; device.app_version = request.app_version
        row.status = ActivationCodeStatus.USED; row.used_at = now; row.used_by_device_id = device.id
        result = self._new_session(db, subscription, device, request.app_version)
        self.audit(db, "ACTIVATE", "SUCCESS", user_id=subscription.user_id, subscription_id=subscription.id, device_id=device.id,
                   ip=ip, user_agent=user_agent, metadata={"code_last4": request.activation_code[-4:]})
        return result

    def _new_session(self, db: Session, subscription: Subscription, device: Device, app_version: str) -> dict[str, Any]:
        now = utcnow(); token_id = str(uuid.uuid4()); refresh = secrets.token_urlsafe(48)
        session = LicenseSession(device_id=device.id, token_id=token_id, refresh_hash=opaque_hash(refresh),
                                 expires_at=now + timedelta(days=self.settings.refresh_days), client_version=app_version)
        db.add(session); db.flush()
        token = self._entitlement(subscription, device, token_id, now)
        return self._response(subscription, device, session, token, refresh, now)

    def _entitlement(self, subscription: Subscription, device: Device, token_id: str, now: datetime) -> str:
        token_expires = min(now + timedelta(hours=self.settings.token_hours), aware(subscription.expires_at))
        grace = min(aware(subscription.expires_at), now + timedelta(hours=self.settings.offline_grace_hours))
        payload = {"schema_version": 1, "token_id": token_id, "key_id": self.settings.signing_key_id,
                   "product_code": "creator_assistant", "user_id": subscription.user_id,
                   "subscription_id": subscription.id, "device_id": device.id, "installation_id": device.installation_id,
                   "plan_code": subscription.plan.code, "features": subscription.plan.features,
                   "issued_at": iso(now), "not_before": iso(now - timedelta(minutes=2)), "expires_at": iso(token_expires),
                   "subscription_until": iso(subscription.expires_at), "offline_grace_until": iso(grace),
                   "minimum_app_version": self.settings.minimum_app_version or None}
        return self.signer.sign(payload)

    def _response(self, subscription, device, session, token, refresh, now) -> dict[str, Any]:
        return {"entitlement_token": token, "refresh_credential": refresh,
                "subscription": {"id": subscription.id, "status": subscription.status.value, "plan": subscription.plan.code,
                                 "expires_at": iso(subscription.expires_at)},
                "device": {"id": device.id, "name": device.friendly_name, "status": device.status.value},
                "refresh": {"after_seconds": 86400, "session_expires_at": iso(session.expires_at)}, "server_time": iso(now),
                "notices": [], "minimum_supported_version": self.settings.minimum_app_version,
                "recommended_app_version": self.settings.recommended_app_version}

    def refresh(self, db: Session, request, *, ip: str = "", user_agent: str = "") -> dict[str, Any]:
        now = utcnow(); row = db.scalar(select(LicenseSession).where(LicenseSession.refresh_hash == opaque_hash(request.refresh_credential)).with_for_update())
        if not row or row.status is not SessionStatus.ACTIVE or aware(row.expires_at) <= now: raise LicenseError("REFRESH_SESSION_INVALID", 401)
        device = row.device
        if device.installation_id != request.installation_id: raise LicenseError("ENTITLEMENT_DEVICE_MISMATCH", 403)
        if device.status is not DeviceStatus.ACTIVE: raise LicenseError("DEVICE_REVOKED", 403)
        subscription = db.get(Subscription, device.subscription_id); self._active_subscription(subscription)
        row.status = SessionStatus.REVOKED; row.revoked_at = now
        result = self._new_session(db, subscription, device, request.app_version)
        self.audit(db, "REFRESH", "SUCCESS", user_id=subscription.user_id, subscription_id=subscription.id,
                   device_id=device.id, ip=ip, user_agent=user_agent)
        return result

    def session_from_refresh(self, db: Session, credential: str) -> LicenseSession:
        row = db.scalar(select(LicenseSession).where(LicenseSession.refresh_hash == opaque_hash(credential)))
        if not row or row.status is not SessionStatus.ACTIVE or aware(row.expires_at) <= utcnow():
            raise LicenseError("REFRESH_SESSION_INVALID", 401)
        subscription = db.get(Subscription, row.device.subscription_id)
        if subscription is None:
            raise LicenseError("SUBSCRIPTION_INACTIVE", 403)
        self._active_subscription(subscription)
        return row

    def devices(self, db: Session, session: LicenseSession) -> list[dict]:
        return [{"id": item.id, "name": item.friendly_name, "status": item.status.value,
                 "first_seen_at": iso(item.first_seen_at), "last_seen_at": iso(item.last_seen_at)}
                for item in db.scalars(select(Device).where(Device.subscription_id == session.device.subscription_id)).all()]

    def deactivate(self, db: Session, session: LicenseSession, device_id: str) -> None:
        device = db.get(Device, device_id)
        if not device or device.user_id != session.device.user_id: raise LicenseError("DEVICE_NOT_FOUND", 404)
        device.status = DeviceStatus.DEACTIVATED; device.deactivated_at = utcnow()
        db.execute(update(LicenseSession).where(LicenseSession.device_id == device.id, LicenseSession.status == SessionStatus.ACTIVE)
                   .values(status=SessionStatus.REVOKED, revoked_at=utcnow()))
