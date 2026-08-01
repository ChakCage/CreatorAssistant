from __future__ import annotations

import hmac
import hashlib
import html
import json
import logging
import uuid
from datetime import timedelta
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import hash_admin_token, load_settings
from .db import get_db
from .billing import BillingService
from .models import (ActivationCode, ActivationCodeStatus, AdminAction, BotNotification, Device, DeviceStatus,
                     FreeEntitlement, FreeQuotaKind, LicenseEvent, LicenseSession, Plan, Price, Release, SessionStatus, Subscription,
                     SubscriptionStatus, SupportBlock, SupportTicket, SupportTicketStatus, User, UserStatus, utcnow)
from .rate_limit import RateLimiter
from .schemas import (ActivateRequest, AdminCodeRequest, AdminGrantRequest, AdminPriceRequest,
                      AdminReleaseRequest, AdminUserRequest, BetaRedeemRequest, BotDeviceRequest, BotUserRequest,
                      CheckoutRequest, DeactivateRequest, NotificationResultRequest,
                      FreeBlockRequest, FreeConfigUpdateRequest, FreeEventRequest, FreeMembershipRequest, FreeQuotaFinishRequest,
                      FreeQuotaRequest, RefreshRequest, SupportTicketActionRequest, SupportTicketCreateRequest, TelegramUserRequest)
from .security import verify_service_token
from .service import LicenseError, LicenseManager, aware, iso
from .release_signing import release_manifest
from .beta_access import BetaAccessService
from .redaction import SecretRedactionFilter
from .free_access import FreeAccessService


settings = load_settings(); manager = LicenseManager(settings); billing = BillingService(settings, manager); beta_access = BetaAccessService(manager); free_access = FreeAccessService(settings, manager); limiter = RateLimiter(settings.redis_url)
app = FastAPI(title="Creator Assistant Licensing", version=settings.release_version)
audit_log = logging.getLogger("creator_assistant.requests")
audit_log.addFilter(SecretRedactionFilter())


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = str(uuid.uuid4()); request.state.request_id = request_id
    raw_content_length = request.headers.get("content-length", "0") or "0"
    if not raw_content_length.isdigit():
        return JSONResponse(status_code=400, content={"error": {"code": "INVALID_CONTENT_LENGTH"}, "request_id": request_id})
    content_length = int(raw_content_length)
    if content_length > settings.webhook_max_bytes:
        return JSONResponse(status_code=413, content={"error": {"code": "REQUEST_TOO_LARGE"}, "request_id": request_id})
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if settings.public_base_url.startswith("https://"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    route = request.scope.get("route")
    audit_log.info(json.dumps({"request_id": request_id, "method": request.method,
                               "route": getattr(route, "path", "unmatched"),
                               "status": response.status_code}, separators=(",", ":")))
    return response


@app.exception_handler(LicenseError)
def license_error(request: Request, exc: LicenseError):
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, **exc.details},
                        "request_id": getattr(request.state, "request_id", str(uuid.uuid4()))})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def refresh_session(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> LicenseSession:
    if not authorization.startswith("Bearer "): raise HTTPException(401, "Missing license credential")
    return manager.session_from_refresh(db, authorization[7:])


def require_admin(x_admin_token: str = Header(default="")) -> str:
    if not settings.admin_token_hash or not hmac.compare_digest(hash_admin_token(x_admin_token), settings.admin_token_hash):
        raise HTTPException(401, "Admin authorization required")
    return "api-admin"


def require_service(permission: str):
    def dependency(authorization: str = Header(default="")) -> dict:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Service authorization required")
        try:
            return verify_service_token(authorization[7:], settings.bot_service_secret, permission)
        except ValueError:
            raise HTTPException(401, "Invalid service credential")
    return dependency


def require_free_admin(telegram_user_id: str) -> None:
    if str(settings.free_access_admin_telegram_id) != str(telegram_user_id):
        raise LicenseError("FREE_ADMIN_FORBIDDEN", 403)


def release_metadata() -> dict:
    return {"version": settings.release_version, "commit": settings.release_commit}


@app.get("/health")
def health(): return {"status": "ok", **release_metadata()}


@app.get("/ready")
def ready(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1")); return {"status": "ready", **release_metadata()}


@app.get("/version")
def version(): return {**release_metadata(), "environment": settings.environment}


@app.get("/v1/licenses/keys")
def keys(): return {"keys": {settings.signing_key_id: manager.signer.public_key_b64()}, "schema_version": 1}


@app.get("/v1/releases/latest")
def latest_release(
    edition: str,
    channel: str,
    current_version: str,
    architecture: str,
    db: Session = Depends(get_db),
):
    """Public, secret-free release metadata, strictly partitioned by product profile."""
    if edition not in {"developer", "commercial"}:
        raise HTTPException(400, "Unknown edition")
    if channel not in {"developer", "stable", "beta"}:
        raise HTTPException(400, "Unknown channel")
    row = db.scalar(select(Release).where(
        Release.edition == edition,
        Release.channel == channel,
        Release.architecture == architecture,
        Release.is_active.is_(True),
    ).order_by(Release.build_number.desc(), Release.published_at.desc()))
    if not row or not row.signature or not _newer(row.version, current_version):
        return {}
    return release_manifest(row)


def _newer(candidate: str, current: str) -> bool:
    def key(value: str):
        main = value.split("-", 1)[0]
        try:
            return tuple(int(item) for item in main.split("."))
        except ValueError:
            return ()
    return key(candidate) > key(current)


@app.post("/v1/licenses/activate")
def activate(value: ActivateRequest, request: Request, db: Session = Depends(get_db)):
    code_key = hashlib.sha256(value.activation_code.upper().encode()).hexdigest()[:16]
    key = f"activate:{client_ip(request)}:{value.installation_id[:16]}:{code_key}"
    if not limiter.allow(key, 12, 300): raise LicenseError("TOO_MANY_ATTEMPTS", 429)
    try:
        result = manager.activate(db, value, ip=client_ip(request), user_agent=request.headers.get("user-agent", ""))
        if (result.get("subscription") or {}).get("plan") == "free_channel":
            manager.audit(db, "FREE_DESKTOP_ACTIVATED", "SUCCESS", subscription_id=result["subscription"].get("id"))
        db.commit(); return result
    except Exception:
        db.commit(); raise


@app.post("/v1/licenses/refresh")
def refresh(value: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    if not limiter.allow(f"refresh:{client_ip(request)}:{value.installation_id[:16]}", 60, 3600):
        raise LicenseError("TOO_MANY_ATTEMPTS", 429)
    result = manager.refresh(db, value, ip=client_ip(request), user_agent=request.headers.get("user-agent", "")); db.commit(); return result


@app.get("/v1/licenses/status")
def status(session: LicenseSession = Depends(refresh_session), db: Session = Depends(get_db)):
    subscription = db.get(Subscription, session.device.subscription_id)
    if session.device.status is not DeviceStatus.ACTIVE:
        raise LicenseError("DEVICE_REVOKED", 403)
    if subscription.user.status is UserStatus.BLOCKED:
        raise LicenseError("SUBSCRIPTION_INACTIVE", 403)
    if aware(subscription.expires_at) <= utcnow():
        subscription.status = SubscriptionStatus.EXPIRED
        db.commit()
    return {"subscription": {"status": subscription.status.value, "plan": subscription.plan.code, "expires_at": iso(subscription.expires_at)},
            "device": {"id": session.device.id, "status": session.device.status.value}, "server_time": iso(utcnow())}


@app.get("/v1/licenses/devices")
def devices(request: Request, session: LicenseSession = Depends(refresh_session), db: Session = Depends(get_db)):
    if not limiter.allow(f"devices:{session.device.user_id}:{client_ip(request)}", 120, 3600):
        raise LicenseError("TOO_MANY_ATTEMPTS", 429)
    return {"devices": manager.devices(db, session), "server_time": iso(utcnow())}


@app.post("/v1/licenses/devices/{device_id}/deactivate")
def deactivate(device_id: str, _value: DeactivateRequest, session: LicenseSession = Depends(refresh_session), db: Session = Depends(get_db)):
    manager.deactivate(db, session, device_id); db.commit(); return {"status": "DEACTIVATED"}


@app.post("/v1/licenses/logout")
@app.post("/v1/licenses/revoke-local")
def logout(session: LicenseSession = Depends(refresh_session), db: Session = Depends(get_db)):
    session.status = SessionStatus.REVOKED; session.revoked_at = utcnow(); db.commit(); return {"status": "REVOKED"}


@app.post("/v1/admin/users")
def create_user(value: AdminUserRequest, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    user = User(email=value.email, telegram_user_id=value.telegram_user_id); db.add(user); db.flush()
    db.add(AdminAction(admin_id=admin, action="create-user", target_type="user", target_id=user.id)); db.commit(); return {"id": user.id}


@app.post("/v1/admin/subscriptions/grant")
def grant(value: AdminGrantRequest, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    if value.idempotency_key:
        previous = next((item for item in db.scalars(select(AdminAction).where(
            AdminAction.action == "grant-subscription",
            AdminAction.admin_id == admin,
        ).order_by(AdminAction.created_at.desc())).all()
            if item.action_metadata.get("idempotency_key") == value.idempotency_key), None)
        if previous and previous.action_metadata.get("idempotency_key") == value.idempotency_key:
            subscription = db.get(Subscription, previous.target_id)
            if subscription:
                return {"id": subscription.id, "expires_at": iso(subscription.expires_at), "idempotent_replay": True}
    user = db.get(User, value.user_id)
    if not user: raise HTTPException(404, "User not found")
    _, default_plan = manager.bootstrap(db)
    plan = db.scalar(select(Plan).where(Plan.code == value.plan_code)) or default_plan
    subscription = manager.grant(db, user, plan, value.days)
    db.add(AdminAction(admin_id=admin, action="grant-subscription", target_type="subscription", target_id=subscription.id,
                       reason=value.reason, action_metadata={"days": value.days or plan.duration_days, "idempotency_key": value.idempotency_key or ""}))
    db.commit(); return {"id": subscription.id, "expires_at": iso(subscription.expires_at)}


@app.post("/v1/admin/activation-codes")
def create_code(value: AdminCodeRequest, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == value.user_id,
        Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE])).order_by(Subscription.expires_at.desc()))
    if not subscription: raise HTTPException(409, "Active subscription required")
    code = manager.new_code(db, subscription, value.ttl_minutes)
    db.add(AdminAction(admin_id=admin, action="create-activation-code", target_type="subscription", target_id=subscription.id,
                       reason=value.reason, action_metadata={"ttl_minutes": value.ttl_minutes, "code_last4": code[-4:]}))
    db.commit(); return {"activation_code": code, "expires_in_minutes": value.ttl_minutes}


@app.post("/v1/admin/activation-codes/{code_id}/revoke")
def revoke_code(code_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    code = db.get(ActivationCode, code_id)
    if not code: raise HTTPException(404, "Code not found")
    code.status = ActivationCodeStatus.REVOKED; db.add(AdminAction(admin_id=admin, action="revoke-code", target_type="activation-code", target_id=code.id)); db.commit(); return {"status": "REVOKED"}


@app.get("/v1/admin/events")
def events(limit: int = 100, _admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    values = db.scalars(select(LicenseEvent).order_by(LicenseEvent.created_at.desc()).limit(min(limit, 500))).all()
    return [{"event_type": item.event_type, "result": item.result, "reason_code": item.reason_code, "created_at": iso(item.created_at)} for item in values]


@app.post("/v1/bot/users/upsert")
def bot_user(value: TelegramUserRequest, _identity: dict = Depends(require_service("users:write")), db: Session = Depends(get_db)):
    user = billing.upsert_telegram_user(db, value); db.commit()
    return {"id": user.id, "status": user.status.value}


@app.get("/v1/bot/plans")
def bot_plans(_identity: dict = Depends(require_service("plans:read")), db: Session = Depends(get_db)):
    return {"plans": billing.plans(db)}


@app.post("/v1/billing/checkout")
def checkout(value: CheckoutRequest, _identity: dict = Depends(require_service("checkout:create")), db: Session = Depends(get_db)):
    if not settings.payments_enabled:
        raise HTTPException(404)
    if not limiter.allow(f"checkout:{value.telegram_user_id}", 20, 3600):
        raise LicenseError("TOO_MANY_ATTEMPTS", 429)
    result = billing.create_checkout(db, value); db.commit(); return result


@app.get("/v1/billing/fake/checkout/{token}", response_class=HTMLResponse)
def fake_checkout(token: str, db: Session = Depends(get_db)):
    if not settings.payments_enabled:
        raise HTTPException(404)
    row = billing.checkout_by_token(db, token); payment = row.payment
    csrf = billing.checkout_csrf(row)
    page = f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'\">
<title>Тестовая оплата</title><style>body{{font:18px sans-serif;max-width:620px;margin:60px auto}}button{{padding:12px 20px;margin:8px}}</style></head>
<body><h1>Тестовая оплата</h1><p>{html.escape(payment.description)}</p>
<p>{payment.amount_minor / 100:.2f} {html.escape(payment.currency)}</p>
<form method=post><input type=hidden name=csrf value=\"{csrf}\"><button name=action value=success>Успешная оплата</button>
<button name=action value=cancel>Отмена</button></form></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"})


@app.post("/v1/billing/fake/checkout/{token}", response_class=HTMLResponse)
async def fake_checkout_action(token: str, request: Request, db: Session = Depends(get_db)):
    if not settings.payments_enabled:
        raise HTTPException(404)
    raw_form = await request.body()
    if len(raw_form) > 4096:
        raise HTTPException(413)
    form = parse_qs(raw_form.decode("utf-8", "strict"))
    row = billing.checkout_by_token(db, token)
    if not hmac.compare_digest(form.get("csrf", [""])[0], billing.checkout_csrf(row)):
        raise HTTPException(403, "Invalid CSRF token")
    action = form.get("action", [""])[0]
    if action not in {"success", "cancel"}:
        raise HTTPException(400, "Invalid action")
    payment = row.payment
    provider = billing.provider("fake")
    body, headers = provider.signed_event(
        payment_id=payment.id, provider_payment_id=payment.provider_payment_id,
        event_type="payment.succeeded" if action == "success" else "payment.cancelled",
        amount_minor=payment.amount_minor, currency=payment.currency,
    )
    result = billing.process_webhook(db, "fake", headers, body)
    row.used_at = utcnow(); db.commit()
    return HTMLResponse(f"<h1>{'Оплата принята' if action == 'success' else 'Оплата отменена'}</h1><p>{html.escape(result['status'])}</p>")


@app.post("/v1/billing/webhooks/{provider}")
async def payment_webhook(provider: str, request: Request, db: Session = Depends(get_db)):
    if not settings.payments_enabled:
        raise HTTPException(404)
    body = await request.body()
    if len(body) > settings.webhook_max_bytes:
        raise HTTPException(413, "Webhook body too large")
    headers = {key.lower(): value for key, value in request.headers.items()}
    result = billing.process_webhook(db, provider, headers, body)
    db.commit(); return result


@app.get("/v1/bot/subscription/{telegram_user_id}")
def bot_subscription(telegram_user_id: str, _identity: dict = Depends(require_service("subscription:read")), db: Session = Depends(get_db)):
    return billing.subscription(db, telegram_user_id)


@app.post("/v1/bot/activation-code")
def bot_activation(value: BotUserRequest, _identity: dict = Depends(require_service("activation:create")), db: Session = Depends(get_db)):
    if not limiter.allow(f"activation-code:{value.telegram_user_id}", 5, 3600):
        raise HTTPException(429, "Activation code rate limit exceeded")
    result = billing.create_activation_code(db, value.telegram_user_id); db.commit(); return result


@app.post("/v1/bot/beta/redeem")
def bot_beta_redeem(value: BetaRedeemRequest, _identity: dict = Depends(require_service("beta:redeem")), db: Session = Depends(get_db)):
    if not limiter.allow(f"beta-redeem:{value.telegram_user_id}", 8, 3600):
        raise HTTPException(429, "Invite redemption rate limit exceeded")
    subscription = beta_access.redeem(db, value.telegram_user_id, value.invite_code)
    db.commit()
    return {"status": subscription.status.value, "expires_at": iso(subscription.expires_at)}


@app.get("/v1/bot/free/config")
def bot_free_config(_identity: dict = Depends(require_service("free:read")), db: Session = Depends(get_db)):
    return free_access.config(db)


@app.get("/v1/bot/free/status/{telegram_user_id}")
def bot_free_status(telegram_user_id: str, _identity: dict = Depends(require_service("free:read")),
                    db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if not user:
        return {"state": "ELIGIBLE", "granted": False, "config": free_access.config(db)}
    return free_access.payload(db, user)


@app.post("/v1/bot/free/membership")
def bot_free_membership(value: FreeMembershipRequest, request: Request,
                        _identity: dict = Depends(require_service("free:write")), db: Session = Depends(get_db)):
    if not limiter.allow(f"free-membership:{value.telegram_user_id}:{client_ip(request)}", 6, 300):
        raise LicenseError("FREE_MEMBERSHIP_RATE_LIMIT", 429)
    user = db.scalar(select(User).where(User.telegram_user_id == value.telegram_user_id))
    if not user: raise LicenseError("USER_NOT_FOUND", 404)
    row = free_access.membership_result(db, user, status=value.status, is_member=value.is_member)
    db.commit()
    return free_access.payload(db, user)


@app.post("/v1/bot/free/events")
def bot_free_event(value: FreeEventRequest, _identity: dict = Depends(require_service("free:write")),
                   db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.telegram_user_id == value.telegram_user_id))
    manager.audit(db, value.event.upper(), value.result.upper(), reason=value.reason.upper(),
                  user_id=user.id if user else None)
    db.commit(); return {"status": "RECORDED"}


@app.get("/v1/bot/free/admin/entitlements")
def bot_free_admin_list(telegram_user_id: str, _identity: dict = Depends(require_service("free:admin")),
                        db: Session = Depends(get_db)):
    require_free_admin(telegram_user_id)
    rows = db.scalars(select(FreeEntitlement).order_by(FreeEntitlement.created_at.desc()).limit(200)).all()
    return {"entitlements": [free_access.payload(db, row.user) for row in rows]}


@app.post("/v1/bot/free/admin/config")
def bot_free_admin_config(value: FreeConfigUpdateRequest,
                          _identity: dict = Depends(require_service("free:admin")), db: Session = Depends(get_db)):
    require_free_admin(value.telegram_user_id)
    values = value.model_dump(exclude={"telegram_user_id"}, exclude_none=True)
    result = free_access.update_config(db, values, "telegram-admin:" + value.telegram_user_id)
    db.commit(); return result


@app.post("/v1/bot/free/admin/entitlements/{entitlement_id}/block")
def bot_free_admin_block(entitlement_id: str, value: FreeBlockRequest,
                         _identity: dict = Depends(require_service("free:admin")), db: Session = Depends(get_db)):
    require_free_admin(value.telegram_user_id)
    row = free_access.set_blocked(db, entitlement_id, value.blocked, "telegram-admin:" + value.telegram_user_id)
    db.commit(); return {"id": row.id, "state": row.state.value}


@app.get("/v1/licenses/free/status")
def license_free_status(session: LicenseSession = Depends(refresh_session), db: Session = Depends(get_db)):
    return free_access.payload(db, session.device.user)


@app.post("/v1/licenses/free/quotas/acquire")
def license_free_quota_acquire(value: FreeQuotaRequest, session: LicenseSession = Depends(refresh_session),
                               db: Session = Depends(get_db)):
    row = free_access.acquire_quota(db, session.device.user_id, FreeQuotaKind(value.kind), value.operation_key)
    db.commit(); return {"reservation_id": row.id, "status": row.status.value}


@app.post("/v1/licenses/free/quotas/{reservation_id}/finish")
def license_free_quota_finish(reservation_id: str, value: FreeQuotaFinishRequest,
                              session: LicenseSession = Depends(refresh_session), db: Session = Depends(get_db)):
    row = free_access.finish_quota(db, session.device.user_id, reservation_id, value.success)
    db.commit(); return {"reservation_id": row.id, "status": row.status.value}


@app.post("/v1/bot/devices/deactivate")
def bot_deactivate(value: BotDeviceRequest, _identity: dict = Depends(require_service("devices:write")), db: Session = Depends(get_db)):
    result = billing.deactivate_device(db, value.telegram_user_id, value.device_id, value.confirmed); db.commit(); return result


@app.get("/v1/bot/release")
def bot_release(request: Request, _identity: dict = Depends(require_service("release:read")), db: Session = Depends(get_db)):
    if not limiter.allow(f"bot-release:{client_ip(request)}", 60, 60):
        raise HTTPException(429, "Release lookup rate limit exceeded")
    row = db.scalar(select(Release).where(
        Release.edition == "commercial", Release.channel == "beta",
        Release.is_active.is_(True), Release.signature.is_not(None),
    ).order_by(Release.published_at.desc()))
    if not row:
        raise LicenseError("RELEASE_NOT_FOUND", 404)
    return {
        "version": row.version, "download_url": row.download_url, "sha256": row.sha256,
        "file_size": row.file_size, "release_notes": row.release_notes,
        "published_at": iso(row.published_at), "unsigned_beta": True,
        "edition": row.edition, "channel": row.channel,
    }


@app.get("/v1/bot/notifications")
def bot_notifications(limit: int = 20, _identity: dict = Depends(require_service("notifications:read")), db: Session = Depends(get_db)):
    rows = billing.claim_notifications(db, limit)
    result = {"notifications": [{"id": row.id, "telegram_user_id": row.telegram_user_id,
                                "type": row.notification_type, "payload": row.payload,
                                "attempts": row.attempts} for row in rows]}
    db.commit(); return result


@app.post("/v1/bot/notifications/{notification_id}/result")
def bot_notification_result(notification_id: str, value: NotificationResultRequest,
                            _identity: dict = Depends(require_service("notifications:write")), db: Session = Depends(get_db)):
    row = db.get(BotNotification, notification_id)
    if not row: raise HTTPException(404, "Notification not found")
    billing.finish_notification(row, value.success, value.error)
    if row.notification_type == "SUPPORT_TICKET_CREATED":
        ticket_id = str((row.payload or {}).get("ticket_id") or "")
        db.add(AdminAction(
            admin_id="system:telegram-notification-worker",
            action="support-admin-notification-sent" if value.success else "support-admin-notification-failed",
            target_type="support-ticket", target_id=ticket_id,
            reason="" if value.success else str(value.error or "delivery failed")[:300],
            metadata={"notification_id": row.id, "attempts": row.attempts},
        ))
    db.commit()
    return {"status": row.status.value}


def _ticket_payload(row: SupportTicket) -> dict:
    return {
        "id": row.id,
        "number": "CA-" + row.id.split("-", 1)[0].upper(),
        "telegram_user_id": row.telegram_user_id,
        "category": row.category,
        "message": row.message,
        "attachment": row.attachment or {},
        "status": row.status.value,
        "admin_reply": row.admin_reply,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


@app.post("/v1/bot/support/tickets")
def bot_support_create(value: SupportTicketCreateRequest, request: Request,
                       _identity: dict = Depends(require_service("support:write")),
                       db: Session = Depends(get_db)):
    if not limiter.allow(f"support-create:{value.telegram_user_id}:{client_ip(request)}", 5, 3600):
        raise LicenseError("TOO_MANY_ATTEMPTS", 429)
    user = db.scalar(select(User).where(User.telegram_user_id == value.telegram_user_id))
    if not user:
        raise LicenseError("USER_NOT_FOUND", 404)
    if db.scalar(select(SupportBlock).where(SupportBlock.user_id == user.id)):
        raise LicenseError("SUPPORT_BLOCKED", 403)
    attachment = dict(value.attachment or {})
    if attachment:
        allowed = {"photo", "text/plain", "application/zip"}
        if str(attachment.get("type", "")) not in allowed:
            raise LicenseError("UNSUPPORTED_ATTACHMENT", 422)
        if int(attachment.get("size", 0) or 0) > 5 * 1024 * 1024:
            raise LicenseError("ATTACHMENT_TOO_LARGE", 413)
        attachment = {key: attachment[key] for key in ("type", "name", "file_id", "size") if key in attachment}
    row = SupportTicket(
        user_id=user.id, telegram_user_id=value.telegram_user_id,
        category=value.category, message=value.message, attachment=attachment,
    )
    db.add(row); db.flush()
    db.add(AdminAction(
        admin_id="telegram-user:" + value.telegram_user_id,
        action="create-support-ticket", target_type="support-ticket", target_id=row.id,
    ))
    db.add(BotNotification(
        user_id=user.id,
        telegram_user_id=value.telegram_user_id,
        notification_type="SUPPORT_TICKET_CREATED",
        payload={"ticket_id": row.id},
        dedupe_key=f"support-created:{row.id}",
    ))
    db.commit()
    return _ticket_payload(row)


@app.get("/v1/bot/support/tickets")
def bot_support_list(status: str = "OPEN", limit: int = 30,
                     _identity: dict = Depends(require_service("support:admin")),
                     db: Session = Depends(get_db)):
    query = select(SupportTicket).order_by(SupportTicket.created_at.desc()).limit(min(max(limit, 1), 100))
    if status in {"OPEN", "CLOSED"}:
        query = query.where(SupportTicket.status == SupportTicketStatus(status))
    return {"tickets": [_ticket_payload(row) for row in db.scalars(query).all()]}


@app.get("/v1/bot/support/tickets/{ticket_id}")
def bot_support_get(ticket_id: str, _identity: dict = Depends(require_service("support:admin")),
                    db: Session = Depends(get_db)):
    row = db.get(SupportTicket, ticket_id)
    if not row:
        raise LicenseError("SUPPORT_TICKET_NOT_FOUND", 404)
    return _ticket_payload(row)


@app.post("/v1/bot/support/tickets/{ticket_id}/reply")
def bot_support_reply(ticket_id: str, value: SupportTicketActionRequest,
                      _identity: dict = Depends(require_service("support:admin")),
                      db: Session = Depends(get_db)):
    row = db.get(SupportTicket, ticket_id)
    if not row:
        raise LicenseError("SUPPORT_TICKET_NOT_FOUND", 404)
    row.admin_reply = value.message
    db.add(AdminAction(admin_id="telegram-admin:" + value.telegram_user_id, action="reply-support-ticket",
                       target_type="support-ticket", target_id=row.id))
    db.commit()
    return _ticket_payload(row)


@app.post("/v1/bot/support/tickets/{ticket_id}/close")
def bot_support_close(ticket_id: str, value: SupportTicketActionRequest,
                      _identity: dict = Depends(require_service("support:admin")),
                      db: Session = Depends(get_db)):
    row = db.get(SupportTicket, ticket_id)
    if not row:
        raise LicenseError("SUPPORT_TICKET_NOT_FOUND", 404)
    row.status = SupportTicketStatus.CLOSED
    row.closed_at = utcnow()
    db.add(AdminAction(admin_id="telegram-admin:" + value.telegram_user_id, action="close-support-ticket",
                       target_type="support-ticket", target_id=row.id))
    db.commit()
    return _ticket_payload(row)


@app.post("/v1/bot/support/tickets/{ticket_id}/block")
def bot_support_block(ticket_id: str, value: SupportTicketActionRequest,
                      _identity: dict = Depends(require_service("support:admin")),
                      db: Session = Depends(get_db)):
    row = db.get(SupportTicket, ticket_id)
    if not row:
        raise LicenseError("SUPPORT_TICKET_NOT_FOUND", 404)
    if not db.scalar(select(SupportBlock).where(SupportBlock.user_id == row.user_id)):
        db.add(SupportBlock(user_id=row.user_id, reason=value.message or "spam"))
    db.add(AdminAction(admin_id="telegram-admin:" + value.telegram_user_id, action="block-support-user",
                       target_type="user", target_id=row.user_id, reason=value.message or "spam"))
    db.commit()
    return {"status": "BLOCKED"}


@app.post("/v1/admin/prices")
def admin_price(value: AdminPriceRequest, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    _, default_plan = manager.bootstrap(db)
    plan = db.scalar(select(Plan).where(Plan.code == value.plan_code)) or default_plan
    row = db.scalar(select(Price).where(Price.plan_id == plan.id, Price.provider == value.provider,
                                        Price.currency == value.currency.upper()))
    if row is None:
        row = Price(plan_id=plan.id, provider=value.provider, amount_minor=value.amount_minor,
                    currency=value.currency.upper()); db.add(row)
    else:
        row.amount_minor = value.amount_minor; row.is_active = True
    db.flush(); db.add(AdminAction(admin_id=admin, action="upsert-price", target_type="price", target_id=row.id)); db.commit()
    return {"id": row.id}


@app.post("/v1/admin/releases")
def admin_release(value: AdminReleaseRequest, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    row = Release(**value.model_dump()); db.add(row); db.flush()
    db.add(AdminAction(admin_id=admin, action="create-release", target_type="release", target_id=row.id)); db.commit()
    return {"id": row.id}
