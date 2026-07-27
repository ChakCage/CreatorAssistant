from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AdminAction, BetaInvite, BetaInviteUse, Plan, Subscription, SubscriptionSource,
    SubscriptionStatus, User, utcnow,
)
from .security import secret_hash
from .service import LicenseError, LicenseManager, aware


class BetaAccessService:
    def __init__(self, manager: LicenseManager) -> None:
        self.manager = manager

    def create_invite(
        self, db: Session, *, valid_days: int, max_uses: int, subscription_days: int,
        device_limit: int, description: str = "", admin_id: str = "cli",
    ) -> tuple[BetaInvite, str]:
        code = "BETA-" + "-".join(
            "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
            for _ in range(2)
        )
        row = BetaInvite(
            code_hash=secret_hash(code, self.manager.settings.activation_pepper),
            code_last4=code[-4:], expires_at=utcnow() + timedelta(days=valid_days),
            max_uses=max_uses, subscription_days=subscription_days, device_limit=device_limit,
            group_description=description,
        )
        db.add(row); db.flush()
        db.add(AdminAction(
            admin_id=admin_id, action="create-beta-invite", target_type="beta_invite",
            target_id=row.id, action_metadata={"last4": row.code_last4, "max_uses": max_uses},
        ))
        return row, code

    def redeem(self, db: Session, telegram_user_id: str, code: str) -> Subscription:
        code_hash = secret_hash(code.strip().upper(), self.manager.settings.activation_pepper)
        invite = db.scalar(select(BetaInvite).where(BetaInvite.code_hash == code_hash).with_for_update())
        if not invite:
            raise LicenseError("BETA_INVITE_INVALID", 404)
        if invite.revoked_at:
            raise LicenseError("BETA_INVITE_REVOKED", 410)
        if aware(invite.expires_at) <= utcnow():
            raise LicenseError("BETA_INVITE_EXPIRED", 410)
        if invite.used_count >= invite.max_uses:
            raise LicenseError("BETA_INVITE_USES_EXHAUSTED", 409)
        if db.scalar(select(BetaInviteUse).where(BetaInviteUse.telegram_user_id == telegram_user_id)):
            raise LicenseError("BETA_ALREADY_REDEEMED", 409)
        user = db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if not user:
            user = User(telegram_user_id=telegram_user_id); db.add(user); db.flush()
        beta_history = db.scalar(
            select(Subscription.id).join(Plan, Subscription.plan_id == Plan.id)
            .where(Subscription.user_id == user.id, Plan.code.like("beta-device-%"))
            .limit(1)
        )
        if beta_history:
            raise LicenseError("BETA_ALREADY_REDEEMED", 409)
        existing = db.scalar(select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE]),
        ))
        if existing and aware(existing.expires_at) > utcnow():
            raise LicenseError("BETA_SUBSCRIPTION_EXISTS", 409)
        product, _ = self.manager.bootstrap(db)
        plan_code = f"beta-device-{invite.device_limit}"
        plan = db.scalar(select(Plan).where(Plan.product_id == product.id, Plan.code == plan_code))
        if not plan:
            plan = Plan(
                product_id=product.id, code=plan_code, name="Closed Beta",
                duration_days=invite.subscription_days, device_limit=invite.device_limit,
                features=["project_preparation", "shorts_analysis", "shorts_render"],
            )
            db.add(plan); db.flush()
        subscription = self.manager.grant(
            db, user, plan, invite.subscription_days, source=SubscriptionSource.PROMO,
        )
        invite.used_count += 1
        db.add(BetaInviteUse(
            invite_id=invite.id, user_id=user.id, telegram_user_id=telegram_user_id,
            subscription_id=subscription.id,
        ))
        self.manager.audit(
            db, "BETA_INVITE_REDEEM", "SUCCESS", user_id=user.id,
            subscription_id=subscription.id, metadata={"invite_id": invite.id},
        )
        return subscription

    @staticmethod
    def revoke(db: Session, invite: BetaInvite, admin_id: str = "cli") -> None:
        invite.revoked_at = utcnow()
        db.add(AdminAction(
            admin_id=admin_id, action="revoke-beta-invite",
            target_type="beta_invite", target_id=invite.id,
        ))
