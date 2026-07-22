from __future__ import annotations

import argparse
import json
from datetime import timedelta

from sqlalchemy import select, update

from .config import hash_admin_token, load_settings
from .db import SessionLocal
from .models import ActivationCode, ActivationCodeStatus, AdminAction, Device, DeviceStatus, LicenseEvent, LicenseSession, Plan, SessionStatus, Subscription, SubscriptionStatus, User, UserStatus, utcnow
from .service import LicenseManager, iso


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="creator-license-admin"); sub = root.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-admin"); p.add_argument("--token", required=True)
    p = sub.add_parser("create-user"); p.add_argument("--email"); p.add_argument("--telegram-user-id")
    p = sub.add_parser("find-user"); p.add_argument("--user"); p.add_argument("--email")
    for name in ("grant-subscription", "extend-subscription"):
        p = sub.add_parser(name); p.add_argument("--user", required=True); p.add_argument("--plan", default="beta"); p.add_argument("--days", type=int, default=30); p.add_argument("--reason", default="")
    for name in ("cancel-subscription", "block-user", "unblock-user", "revoke-all-sessions"):
        p = sub.add_parser(name); p.add_argument("--user", required=True); p.add_argument("--reason", default="")
    p = sub.add_parser("create-activation-code"); p.add_argument("--user", required=True); p.add_argument("--ttl-minutes", type=int, default=30)
    p = sub.add_parser("revoke-activation-code"); p.add_argument("--code-id", required=True)
    for name in ("deactivate-device", "block-device"):
        p = sub.add_parser(name); p.add_argument("--device", required=True); p.add_argument("--reason", default="")
    p = sub.add_parser("list-devices"); p.add_argument("--user", required=True)
    p = sub.add_parser("show-license-events"); p.add_argument("--limit", type=int, default=100)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv); settings = load_settings(); manager = LicenseManager(settings)
    if args.command == "create-admin":
        print("LICENSE_ADMIN_TOKEN_HASH=" + hash_admin_token(args.token)); return 0
    with SessionLocal() as db:
        product, beta = manager.bootstrap(db)
        if args.command == "create-user":
            user = User(email=args.email, telegram_user_id=args.telegram_user_id); db.add(user); db.flush(); result = {"id": user.id}
        elif args.command == "find-user":
            user = db.get(User, args.user) if args.user else db.scalar(select(User).where(User.email == args.email)); result = {"id": user.id, "status": user.status.value, "email": user.email} if user else {}
        elif args.command in {"grant-subscription", "extend-subscription"}:
            user = db.get(User, args.user); plan = db.scalar(select(Plan).where(Plan.code == args.plan)) or beta
            subscription = manager.grant(db, user, plan, args.days); result = {"id": subscription.id, "expires_at": iso(subscription.expires_at)}
            db.add(AdminAction(admin_id="cli", action=args.command, target_type="subscription", target_id=subscription.id, reason=args.reason))
        elif args.command == "create-activation-code":
            subscription = db.scalar(select(Subscription).where(Subscription.user_id == args.user, Subscription.status == SubscriptionStatus.ACTIVE).order_by(Subscription.expires_at.desc()))
            code = manager.new_code(db, subscription, args.ttl_minutes); result = {"activation_code": code, "note": "Код показывается только один раз"}
            db.add(AdminAction(admin_id="cli", action=args.command, target_type="subscription", target_id=subscription.id, action_metadata={"code_last4": code[-4:]}))
        elif args.command in {"block-user", "unblock-user"}:
            user = db.get(User, args.user); user.status = UserStatus.BLOCKED if args.command == "block-user" else UserStatus.ACTIVE; result = {"id": user.id, "status": user.status.value}
        elif args.command == "cancel-subscription":
            values = db.scalars(select(Subscription).where(Subscription.user_id == args.user, Subscription.status == SubscriptionStatus.ACTIVE)).all()
            for item in values: item.status = SubscriptionStatus.CANCELLED; item.cancelled_at = utcnow()
            result = {"cancelled": len(values)}
        elif args.command == "revoke-all-sessions":
            device_ids = select(Device.id).where(Device.user_id == args.user)
            count = db.execute(update(LicenseSession).where(LicenseSession.device_id.in_(device_ids), LicenseSession.status == SessionStatus.ACTIVE).values(status=SessionStatus.REVOKED, revoked_at=utcnow())).rowcount; result = {"revoked": count}
        elif args.command in {"deactivate-device", "block-device"}:
            device = db.get(Device, args.device); device.status = DeviceStatus.DEACTIVATED if args.command == "deactivate-device" else DeviceStatus.BLOCKED; result = {"id": device.id, "status": device.status.value}
        elif args.command == "list-devices":
            result = [{"id": d.id, "name": d.friendly_name, "status": d.status.value} for d in db.scalars(select(Device).where(Device.user_id == args.user)).all()]
        elif args.command == "revoke-activation-code":
            code = db.get(ActivationCode, args.code_id); code.status = ActivationCodeStatus.REVOKED; result = {"id": code.id, "status": code.status.value}
        else:
            result = [{"event": e.event_type, "result": e.result, "reason": e.reason_code, "at": iso(e.created_at)} for e in db.scalars(select(LicenseEvent).order_by(LicenseEvent.created_at.desc()).limit(args.limit)).all()]
        db.commit(); print(json.dumps(result, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__": raise SystemExit(main())
