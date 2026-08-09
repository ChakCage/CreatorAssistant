from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .models import (
    AdminAction, FreeAccessState, FreeEntitlement, FreeQuotaKind, FreeQuotaUse, LicenseEvent,
    FreeQuotaUseStatus, Plan, ServerSetting, Subscription, SubscriptionSource,
    SubscriptionStatus, User, utcnow,
)
from .service import FEATURES, LicenseError, LicenseManager, aware, iso


FREE_CONFIG_KEY = "free_access"
ALLOWED_MEMBER_STATUSES = {"creator", "administrator", "member"}


class FreeAccessService:
    def __init__(self, settings: Settings, manager: LicenseManager) -> None:
        self.settings = settings
        self.manager = manager

    def defaults(self) -> dict:
        return {
            "enabled": self.settings.free_access_enabled,
            "channel_chat_id": self.settings.free_access_channel_chat_id,
            "channel_username": self.settings.free_access_channel_username,
            "channel_title": self.settings.free_access_channel_title,
            "channel_invite_url": self.settings.free_access_channel_invite_url,
            "recheck_enabled": self.settings.free_access_recheck_enabled,
            "project_limit": self.settings.free_access_project_limit,
            "shorts_source_limit": self.settings.free_access_shorts_source_limit,
            "device_limit": self.settings.free_access_device_limit,
            "offer_version": self.settings.free_access_offer_version,
        }

    def config(self, db: Session, telegram_user_id: str | int | None = None, *, admin_view: bool = False) -> dict:
        row = db.get(ServerSetting, FREE_CONFIG_KEY)
        # The runtime environment is the security boundary. A persisted admin
        # setting may tune presentation/quotas, but it cannot turn FREE on.
        result = {**self.defaults(), **(row.value if row else {})}
        result["enabled"] = (
            self.settings.free_access_enabled
            if admin_view
            else self.settings.free_access_eligible(telegram_user_id)
        )
        result["test_mode"] = self.settings.free_access_test_mode
        result["test_allowlist_configured"] = bool(
            self.settings.free_access_test_allowlist_valid
            and self.settings.free_access_test_allowlist
        )
        if admin_view:
            result["test_allowlist_size"] = len(self.settings.free_access_test_allowlist)
            result["test_allowlist_valid"] = self.settings.free_access_test_allowlist_valid
        return result

    def require_eligible(self, telegram_user_id: str | int | None) -> None:
        if not self.settings.free_access_enabled:
            raise LicenseError("FREE_ACCESS_DISABLED", 409)
        if not self.settings.free_access_eligible(telegram_user_id):
            raise LicenseError("FREE_ACCESS_NOT_ELIGIBLE", 403)

    def update_config(self, db: Session, values: dict, admin_id: str) -> dict:
        values = dict(values)
        if "enabled" in values:
            raise LicenseError("FREE_RUNTIME_FLAG_REQUIRED", 409)
        if "channel_chat_id" in values and int(values["channel_chat_id"]) >= 0:
            raise LicenseError("FREE_CHANNEL_CHAT_ID_INVALID", 422)
        if "channel_username" in values:
            username = str(values["channel_username"]).strip()
            bare = username[1:]
            if (not username.startswith("@") or not 5 <= len(bare) <= 32
                    or not bare.isascii() or not bare.replace("_", "").isalnum()):
                raise LicenseError("FREE_CHANNEL_USERNAME_INVALID", 422)
            values["channel_username"] = username
        if "channel_invite_url" in values and not str(values["channel_invite_url"]).startswith("https://t.me/"):
            raise LicenseError("FREE_CHANNEL_INVITE_URL_INVALID", 422)
        current = self.config(db, admin_view=True)
        for runtime_key in ("enabled", "test_mode", "test_allowlist_configured", "test_allowlist_size", "test_allowlist_valid"):
            current.pop(runtime_key, None)
        current.update(values)
        row = db.get(ServerSetting, FREE_CONFIG_KEY)
        if row is None:
            row = ServerSetting(key=FREE_CONFIG_KEY, value=current)
            db.add(row)
        else:
            row.value = dict(current)
        db.add(AdminAction(admin_id=admin_id, action="update-free-access-settings",
                           target_type="server-setting", target_id=FREE_CONFIG_KEY,
                           action_metadata={key: value for key, value in current.items() if "invite" not in key}))
        return self.config(db, admin_view=True)

    @staticmethod
    def membership_allowed(status: str, is_member: bool | None = None) -> bool:
        normalized = status.strip().casefold()
        return normalized in ALLOWED_MEMBER_STATUSES or (normalized == "restricted" and is_member is True)

    def _paid_subscription(self, db: Session, user: User) -> Subscription | None:
        now = utcnow()
        rows = db.scalars(select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE]),
        )).all()
        return next((row for row in rows if row.plan.code != "free_channel" and aware(row.expires_at) > now), None)

    def _free_plan(self, db: Session, config: dict) -> Plan:
        product, _ = self.manager.bootstrap(db)
        row = db.scalar(select(Plan).where(Plan.product_id == product.id, Plan.code == "free_channel"))
        if row is None:
            row = Plan(product_id=product.id, code="free_channel", name="Creator Assistant Free",
                       duration_days=3650, device_limit=int(config["device_limit"]), features=FEATURES)
            db.add(row); db.flush()
        else:
            row.device_limit = int(config["device_limit"])
        return row

    def membership_result(self, db: Session, user: User, *, status: str, is_member: bool | None,
                          source: str = "telegram-bot") -> FreeEntitlement:
        self.require_eligible(user.telegram_user_id)
        config = self.config(db, user.telegram_user_id)
        entitlement = db.scalar(select(FreeEntitlement).where(FreeEntitlement.user_id == user.id).with_for_update())
        allowed = self.membership_allowed(status, is_member)
        now = utcnow()
        previous_state = entitlement.state if entitlement is not None else None
        if entitlement is None:
            entitlement = FreeEntitlement(
                user_id=user.id, state=FreeAccessState.ELIGIBLE,
                free_offer_version=str(config["offer_version"]), projects_limit=int(config["project_limit"]),
                shorts_sources_limit=int(config["shorts_source_limit"]), device_limit=int(config["device_limit"]),
            )
            db.add(entitlement); db.flush()
        entitlement.channel_membership_last_checked_at = now
        entitlement.channel_membership_last_status = status.strip().casefold()
        paid = self._paid_subscription(db, user)
        if paid:
            entitlement.state = FreeAccessState.CONVERTED_TO_PAID
            if previous_state is not FreeAccessState.CONVERTED_TO_PAID:
                self.manager.audit(db, "CONVERTED_TO_PAID", "SUCCESS", user_id=user.id,
                                   subscription_id=paid.id)
        elif entitlement.state is FreeAccessState.BLOCKED:
            pass
        elif not allowed:
            if entitlement.free_granted_at:
                entitlement.state = FreeAccessState.PAUSED_UNSUBSCRIBED
                if entitlement.subscription_id:
                    subscription = db.get(Subscription, entitlement.subscription_id)
                    if subscription: subscription.status = SubscriptionStatus.BLOCKED
        else:
            if entitlement.free_granted_at is None:
                plan = self._free_plan(db, config)
                subscription = Subscription(
                    user_id=user.id, product_id=plan.product_id, plan_id=plan.id,
                    status=SubscriptionStatus.ACTIVE, starts_at=now,
                    expires_at=now + timedelta(days=3650), source=SubscriptionSource.PROMO,
                    external_reference=f"free:{entitlement.id}",
                )
                db.add(subscription); db.flush()
                entitlement.subscription_id = subscription.id
                entitlement.free_granted_at = now
                entitlement.free_offer_version = str(config["offer_version"])
                entitlement.projects_limit = int(config["project_limit"])
                entitlement.shorts_sources_limit = int(config["shorts_source_limit"])
                entitlement.device_limit = int(config["device_limit"])
                db.add(AdminAction(admin_id=source, action="grant-free-access", target_type="free-entitlement",
                                   target_id=entitlement.id, action_metadata={"offer_version": entitlement.free_offer_version}))
                self.manager.audit(db, "FREE_GRANTED", "SUCCESS", user_id=user.id,
                                   subscription_id=subscription.id, metadata={"offer_version": entitlement.free_offer_version})
            entitlement.state = FreeAccessState.ACTIVE
            if entitlement.subscription_id:
                subscription = db.get(Subscription, entitlement.subscription_id)
                if subscription:
                    subscription.status = SubscriptionStatus.ACTIVE
        self.manager.audit(db, "CHANNEL_MEMBERSHIP_CHECK", "SUCCESS" if allowed else "DENIED",
                           user_id=user.id, reason=status.upper(), metadata={"source": source})
        self.manager.audit(db, "MEMBERSHIP_CHECK_SUCCEEDED", "SUCCESS" if allowed else "DENIED",
                           user_id=user.id, reason=status.upper())
        if entitlement.state is FreeAccessState.PAUSED_UNSUBSCRIBED and previous_state is not FreeAccessState.PAUSED_UNSUBSCRIBED:
            self.manager.audit(db, "FREE_PAUSED_UNSUBSCRIBED", "SUCCESS", user_id=user.id)
        if entitlement.state is FreeAccessState.ACTIVE and previous_state is FreeAccessState.PAUSED_UNSUBSCRIBED:
            self.manager.audit(db, "FREE_RESUMED", "SUCCESS", user_id=user.id)
        return entitlement

    def payload(self, db: Session, user: User) -> dict:
        row = db.scalar(select(FreeEntitlement).where(FreeEntitlement.user_id == user.id))
        if row is None:
            return {"state": "ELIGIBLE", "granted": False,
                    "config": self.config(db, user.telegram_user_id)}
        paid = self._paid_subscription(db, user)
        state = FreeAccessState.CONVERTED_TO_PAID if paid else row.state
        return {
            "id": row.id, "state": state.value, "granted": row.free_granted_at is not None,
            "telegram_user_id": user.telegram_user_id,
            "free_granted_at": iso(row.free_granted_at) if row.free_granted_at else None,
            "offer_version": row.free_offer_version,
            "projects": {"used": row.projects_used, "limit": row.projects_limit},
            "shorts_sources": {"used": row.shorts_sources_used, "limit": row.shorts_sources_limit},
            "devices": {"used": self._active_devices(db, row), "limit": row.device_limit},
            "membership": {"status": row.channel_membership_last_status,
                           "checked_at": iso(row.channel_membership_last_checked_at) if row.channel_membership_last_checked_at else None},
        }

    @staticmethod
    def _active_devices(db: Session, entitlement: FreeEntitlement) -> int:
        if not entitlement.subscription_id:
            return 0
        from .models import Device, DeviceStatus
        return len(db.scalars(select(Device).where(Device.subscription_id == entitlement.subscription_id,
                                                   Device.status == DeviceStatus.ACTIVE)).all())

    def set_blocked(self, db: Session, entitlement_id: str, blocked: bool, admin_id: str) -> FreeEntitlement:
        row = db.get(FreeEntitlement, entitlement_id)
        if row is None: raise LicenseError("FREE_ENTITLEMENT_NOT_FOUND", 404)
        row.state = FreeAccessState.BLOCKED if blocked else FreeAccessState.ACTIVE
        if row.subscription_id:
            subscription = db.get(Subscription, row.subscription_id)
            if subscription:
                subscription.status = SubscriptionStatus.BLOCKED if blocked else SubscriptionStatus.ACTIVE
        db.add(AdminAction(admin_id=admin_id, action="block-free-access" if blocked else "unblock-free-access",
                           target_type="free-entitlement", target_id=row.id))
        return row

    def acquire_quota(self, db: Session, user_id: str, kind: FreeQuotaKind, operation_key: str) -> FreeQuotaUse:
        row = db.scalar(select(FreeEntitlement).where(FreeEntitlement.user_id == user_id).with_for_update())
        if row is None: raise LicenseError("FREE_ACCESS_REQUIRED", 403)
        if self._paid_subscription(db, row.user):
            raise LicenseError("FREE_QUOTA_NOT_REQUIRED", 409)
        self.require_eligible(row.user.telegram_user_id)
        if row.state is not FreeAccessState.ACTIVE: raise LicenseError("FREE_ACCESS_INACTIVE", 403, {"state": row.state.value})
        existing = db.scalar(select(FreeQuotaUse).where(FreeQuotaUse.entitlement_id == row.id,
                             FreeQuotaUse.kind == kind, FreeQuotaUse.operation_key == operation_key))
        stale_before = utcnow() - timedelta(hours=24)
        if existing and existing.status is FreeQuotaUseStatus.COMMITTED:
            return existing
        if existing and existing.status is FreeQuotaUseStatus.RESERVED and aware(existing.created_at) >= stale_before:
            return existing
        stale = db.scalars(select(FreeQuotaUse).where(
            FreeQuotaUse.entitlement_id == row.id, FreeQuotaUse.kind == kind,
            FreeQuotaUse.status == FreeQuotaUseStatus.RESERVED,
        )).all()
        for reservation in stale:
            if aware(reservation.created_at) < stale_before:
                reservation.status = FreeQuotaUseStatus.RELEASED
        used, limit = ((row.projects_used, row.projects_limit) if kind is FreeQuotaKind.PROJECT
                       else (row.shorts_sources_used, row.shorts_sources_limit))
        reserved = len(db.scalars(select(FreeQuotaUse).where(FreeQuotaUse.entitlement_id == row.id,
                       FreeQuotaUse.kind == kind, FreeQuotaUse.status == FreeQuotaUseStatus.RESERVED)).all())
        if used + reserved >= limit:
            raise LicenseError("FREE_QUOTA_EXHAUSTED", 409, {"kind": kind.value, "used": used, "limit": limit})
        if self.config(db).get("recheck_enabled"):
            last_committed = db.scalar(select(FreeQuotaUse).where(
                FreeQuotaUse.entitlement_id == row.id,
                FreeQuotaUse.status == FreeQuotaUseStatus.COMMITTED,
            ).order_by(FreeQuotaUse.committed_at.desc()))
            if (last_committed and last_committed.committed_at and
                    (not row.channel_membership_last_checked_at or
                     aware(row.channel_membership_last_checked_at) <= aware(last_committed.committed_at))):
                raise LicenseError("FREE_MEMBERSHIP_RECHECK_REQUIRED", 409)
        if existing:
            existing.status = FreeQuotaUseStatus.RESERVED
            existing.created_at = utcnow()
            return existing
        use = FreeQuotaUse(entitlement_id=row.id, kind=kind, operation_key=operation_key)
        db.add(use); db.flush()
        self.manager.audit(db, "FREE_QUOTA_RESERVED", "SUCCESS", user_id=user_id,
                           metadata={"kind": kind.value, "operation_key_hash": operation_key[:16]})
        return use

    def analytics(self, db: Session) -> dict:
        event_names = {
            "offer_views": "FREE_OFFER_OPENED",
            "membership_checks": "MEMBERSHIP_CHECK_STARTED",
            "granted": "FREE_GRANTED",
            "converted_to_paid": "CONVERTED_TO_PAID",
        }
        event_counts = {
            key: int(db.scalar(select(func.count(LicenseEvent.id)).where(
                LicenseEvent.event_type == event_name
            )) or 0)
            for key, event_name in event_names.items()
        }
        states = {
            state.value: int(db.scalar(select(func.count(FreeEntitlement.id)).where(
                FreeEntitlement.state == state
            )) or 0)
            for state in FreeAccessState
        }
        exhausted = int(db.scalar(select(func.count(FreeEntitlement.id)).where(
            FreeEntitlement.projects_used >= FreeEntitlement.projects_limit,
            FreeEntitlement.shorts_sources_used >= FreeEntitlement.shorts_sources_limit,
        )) or 0)
        return {
            **event_counts,
            "active": states[FreeAccessState.ACTIVE.value],
            "paused": states[FreeAccessState.PAUSED_UNSUBSCRIBED.value],
            "exhausted": exhausted,
            "blocked": states[FreeAccessState.BLOCKED.value],
            "runtime": self.config(db, admin_view=True),
        }

    def finish_quota(self, db: Session, user_id: str, use_id: str, success: bool) -> FreeQuotaUse:
        use = db.scalar(select(FreeQuotaUse).where(FreeQuotaUse.id == use_id).with_for_update())
        row = db.get(FreeEntitlement, use.entitlement_id) if use else None
        if use is None or row is None or row.user_id != user_id: raise LicenseError("FREE_QUOTA_RESERVATION_NOT_FOUND", 404)
        if use.status is FreeQuotaUseStatus.COMMITTED: return use
        if success:
            use.status = FreeQuotaUseStatus.COMMITTED; use.committed_at = utcnow()
            if use.kind is FreeQuotaKind.PROJECT: row.projects_used += 1
            else: row.shorts_sources_used += 1
            if use.kind is FreeQuotaKind.PROJECT and row.projects_used == 1:
                self.manager.audit(db, "FIRST_PROJECT_CREATED", "SUCCESS", user_id=user_id)
            if use.kind is FreeQuotaKind.SHORTS_SOURCE and row.shorts_sources_used == 1:
                self.manager.audit(db, "FIRST_SHORTS_SOURCE_USED", "SUCCESS", user_id=user_id)
            if row.projects_used >= row.projects_limit and row.shorts_sources_used >= row.shorts_sources_limit:
                row.state = FreeAccessState.EXHAUSTED
                self.manager.audit(db, "FREE_EXHAUSTED", "SUCCESS", user_id=user_id)
        else:
            use.status = FreeQuotaUseStatus.RELEASED
        self.manager.audit(db, "FREE_QUOTA_COMMITTED" if success else "FREE_QUOTA_RELEASED", "SUCCESS",
                           user_id=user_id, metadata={"kind": use.kind.value})
        return use
