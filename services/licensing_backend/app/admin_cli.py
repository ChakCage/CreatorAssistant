from __future__ import annotations

import argparse
import json
from datetime import timedelta

from sqlalchemy import select, update

from .config import hash_admin_token, load_settings
from .billing import BillingService
from .db import SessionLocal
from .models import (ActivationCode, ActivationCodeStatus, AdminAction, BotNotification, Device,
                     DeviceStatus, LicenseEvent, LicenseSession, NotificationStatus, Payment,
                     PaymentEvent, PaymentEventStatus, PaymentStatus, Plan, Price, SessionStatus,
                     Release, Subscription, SubscriptionStatus, User, UserStatus, utcnow)
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
    p = sub.add_parser("create-price"); p.add_argument("--plan", default="beta"); p.add_argument("--provider", default="fake"); p.add_argument("--amount-minor", required=True, type=int); p.add_argument("--currency", default="RUB")
    p = sub.add_parser("create-release"); p.add_argument("--version", required=True); p.add_argument("--download-url", required=True); p.add_argument("--sha256", required=True); p.add_argument("--channel", default="stable"); p.add_argument("--notes", default="")
    p = sub.add_parser("list-payments"); p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("show-payment"); p.add_argument("--payment", required=True)
    p = sub.add_parser("expire-pending-payments")
    p = sub.add_parser("expire-payment"); p.add_argument("--payment", required=True); p.add_argument("--reason", default="")
    for name in ("cancel-payment", "mark-payment-review", "refund-payment", "reconcile-payment"):
        p = sub.add_parser(name); p.add_argument("--payment", required=True); p.add_argument("--reason", default="")
    p = sub.add_parser("retry-notification"); p.add_argument("--notification", required=True)
    p = sub.add_parser("list-payment-events"); p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("fake-payment-event"); p.add_argument("--payment", required=True); p.add_argument("--event", choices=("success", "cancel", "refund"), required=True); p.add_argument("--event-id"); p.add_argument("--invalid-signature", action="store_true")
    p = sub.add_parser("mark-payment-for-review"); p.add_argument("--payment", required=True); p.add_argument("--reason", required=True)
    for name in ("simulate-payment-success", "simulate-payment-cancel", "simulate-refund", "simulate-duplicate-webhook", "simulate-invalid-signature"):
        p = sub.add_parser(name); p.add_argument("--payment", required=True); p.add_argument("--event-id")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv); settings = load_settings(); manager = LicenseManager(settings); billing = BillingService(settings, manager)
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
        elif args.command == "show-license-events":
            result = [{"event": e.event_type, "result": e.result, "reason": e.reason_code, "at": iso(e.created_at)} for e in db.scalars(select(LicenseEvent).order_by(LicenseEvent.created_at.desc()).limit(args.limit)).all()]
        elif args.command == "create-price":
            plan = db.scalar(select(Plan).where(Plan.code == args.plan)) or beta
            row = Price(plan_id=plan.id, provider=args.provider, amount_minor=args.amount_minor, currency=args.currency.upper())
            db.add(row); db.flush(); result = {"id": row.id}
        elif args.command == "create-release":
            row = Release(edition="commercial", channel=args.channel, version=args.version,
                          download_url=args.download_url, sha256=args.sha256, release_notes=args.notes)
            db.add(row); db.flush(); result = {"id": row.id}
        elif args.command == "list-payments":
            result = [_payment_json(row) for row in db.scalars(select(Payment).order_by(Payment.created_at.desc()).limit(args.limit)).all()]
        elif args.command == "show-payment":
            row = db.get(Payment, args.payment); result = _payment_json(row) if row else {}
        elif args.command == "expire-pending-payments":
            count = db.execute(update(Payment).where(Payment.status == PaymentStatus.PENDING, Payment.expires_at <= utcnow()).values(status=PaymentStatus.EXPIRED)).rowcount; result = {"expired": count}
        elif args.command in {"cancel-payment", "expire-payment", "mark-payment-review", "mark-payment-for-review", "refund-payment", "reconcile-payment"}:
            row = db.get(Payment, args.payment)
            if not row: raise SystemExit("Payment not found")
            if args.command == "cancel-payment": row.status = PaymentStatus.CANCELLED; row.cancelled_at = utcnow()
            elif args.command == "expire-payment": row.status = PaymentStatus.EXPIRED
            elif args.command == "refund-payment": row.status = PaymentStatus.REFUNDED; row.refunded_at = utcnow()
            elif args.command in {"mark-payment-review", "mark-payment-for-review"}: row.payment_metadata = {**row.payment_metadata, "requires_review": True, "review_reason": args.reason}
            result = _payment_json(row)
            db.add(AdminAction(admin_id="cli", action=args.command, target_type="payment", target_id=row.id, reason=args.reason))
        elif args.command == "retry-notification":
            row = db.get(BotNotification, args.notification)
            if not row: raise SystemExit("Notification not found")
            row.status = NotificationStatus.PENDING; row.next_attempt_at = utcnow(); row.last_error = None; result = {"id": row.id, "status": row.status.value}
        elif args.command == "list-payment-events":
            result = [{"id": e.id, "payment_id": e.payment_id, "provider_event_id": e.provider_event_id,
                       "type": e.event_type, "status": e.processing_status.value, "error": e.error_code}
                      for e in db.scalars(select(PaymentEvent).order_by(PaymentEvent.created_at.desc()).limit(args.limit)).all()]
        elif args.command == "fake-payment-event" or args.command.startswith("simulate-"):
            row = db.get(Payment, args.payment)
            if not row: raise SystemExit("Payment not found")
            provider = billing.provider("fake")
            alias = {"simulate-payment-success": "success", "simulate-payment-cancel": "cancel",
                     "simulate-refund": "refund", "simulate-duplicate-webhook": "success",
                     "simulate-invalid-signature": "success"}
            event = alias.get(args.command, getattr(args, "event", ""))
            mapping = {"success": "payment.succeeded", "cancel": "payment.cancelled", "refund": "payment.refunded"}
            body, headers = provider.signed_event(payment_id=row.id, provider_payment_id=row.provider_payment_id,
                event_type=mapping[event], amount_minor=row.amount_minor, currency=row.currency, event_id=args.event_id)
            if getattr(args, "invalid_signature", False) or args.command == "simulate-invalid-signature": headers["x-payment-signature"] = "0" * 64
            result = billing.process_webhook(db, "fake", headers, body)
            if args.command == "simulate-duplicate-webhook":
                result = {"first": result, "second": billing.process_webhook(db, "fake", headers, body)}
        db.commit(); print(json.dumps(result, ensure_ascii=False, indent=2)); return 0


def _payment_json(row: Payment) -> dict:
    return {"id": row.id, "user_id": row.user_id, "provider": row.provider,
            "provider_payment_id": row.provider_payment_id, "status": row.status.value,
            "amount_minor": row.amount_minor, "currency": row.currency,
            "expires_at": iso(row.expires_at)}


if __name__ == "__main__": raise SystemExit(main())
