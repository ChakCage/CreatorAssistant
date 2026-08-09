from __future__ import annotations

import hmac
import hashlib
import html
import json
import logging
import uuid
from datetime import timedelta
from typing import Optional
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
                     ServerSetting, SubscriptionStatus, SupportBlock, SupportMessage, SupportTicket, SupportTicketStatus,
                     User, UserStatus, utcnow)
from .rate_limit import RateLimiter
from .schemas import (ActivateRequest, AdminCodeRequest, AdminGrantRequest, AdminPriceRequest,
                      AdminReleaseRequest, AdminUserRequest, BetaRedeemRequest, BotDeviceRequest, BotUserRequest,
                      CheckoutRequest, DeactivateRequest, NotificationResultRequest,
                      FreeBlockRequest, FreeConfigUpdateRequest, FreeEventRequest, FreeMembershipRequest, FreeQuotaFinishRequest,
                      FreeQuotaRequest, RefreshRequest, SupportDashboardStateRequest, SupportForumMessageRequest,
                      SupportForumThreadRequest,
                      SupportMessageRequest, SupportTicketActionRequest,
                      SupportTicketCreateRequest, TelegramUserRequest)
from .security import verify_service_token
from .service import LicenseError, LicenseManager, aware, iso
from .release_signing import release_manifest
from .beta_access import BetaAccessService
from .redaction import SecretRedactionFilter
from .free_access import FreeAccessService
from .support import support_service, ticket_payload


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
        main, separator, prerelease = value.strip().split("+", 1)[0].partition("-")
        try:
            release = tuple(int(item) for item in main.split("."))
        except ValueError:
            return ()
        # A stable release sorts after every prerelease of the same release.
        # Prerelease identifiers use SemVer ordering: numeric identifiers sort
        # numerically and before non-numeric identifiers.
        if not separator:
            return release, 1, ()
        identifiers = tuple(
            (0, int(item)) if item.isdigit() else (1, item.casefold())
            for item in prerelease.replace("-", ".").split(".")
        )
        return release, 0, identifiers
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
def bot_free_config(telegram_user_id: Optional[str] = None,
                    _identity: dict = Depends(require_service("free:read")), db: Session = Depends(get_db)):
    return free_access.config(db, telegram_user_id)


@app.get("/v1/bot/free/status/{telegram_user_id}")
def bot_free_status(telegram_user_id: str, _identity: dict = Depends(require_service("free:read")),
                    db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if not user:
        return {"state": "ELIGIBLE", "granted": False,
                "config": free_access.config(db, telegram_user_id)}
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
    free_access.require_eligible(value.telegram_user_id)
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


@app.get("/v1/bot/free/admin/stats")
def bot_free_admin_stats(telegram_user_id: str,
                         _identity: dict = Depends(require_service("free:admin")),
                         db: Session = Depends(get_db)):
    require_free_admin(telegram_user_id)
    return free_access.analytics(db)


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
    user = db.get(User, session.device.user_id)
    if not user:
        raise LicenseError("USER_NOT_FOUND", 404)
    return free_access.payload(db, user)


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
    if row.notification_type == "SUPPORT_ADMIN_REPLY":
        message_id = str((row.payload or {}).get("message_id") or "")
        message = db.get(SupportMessage, message_id) if message_id else None
        if message:
            message.delivery_status = "DELIVERED" if value.success else "FAILED"
    if row.notification_type.startswith("SUPPORT_"):
        ticket_id = str((row.payload or {}).get("ticket_id") or "")
        admin_notification = row.notification_type in {
            "SUPPORT_TICKET_CREATED", "SUPPORT_DASHBOARD_REFRESH", "SUPPORT_ADMIN_REPLY",
        }
        action_prefix = "support-admin-notification" if admin_notification else "support-notification"
        db.add(AdminAction(
            admin_id="system:telegram-notification-worker",
            action=action_prefix + ("-sent" if value.success else "-failed"),
            target_type="support-ticket", target_id=ticket_id,
            reason="" if value.success else str(value.error or "delivery failed")[:300],
            action_metadata={"notification_id": row.id, "attempts": row.attempts},
        ))
    db.commit()
    return {"status": row.status.value}


@app.post("/v1/bot/support/tickets")
def bot_support_create(value: SupportTicketCreateRequest, request: Request,
                       _identity: dict = Depends(require_service("support:write")),
                       db: Session = Depends(get_db)):
    if not limiter.allow(f"support-create:{value.telegram_user_id}:{client_ip(request)}", 5, 3600):
        raise LicenseError("TOO_MANY_ATTEMPTS", 429)
    user = db.scalar(select(User).where(User.telegram_user_id == value.telegram_user_id))
    if not user:
        raise LicenseError("USER_NOT_FOUND", 404)
    row = support_service.create(db, user, value.category, value.message, value.attachment, value.idempotency_key)
    db.commit()
    return ticket_payload(row, user)


@app.get("/v1/bot/support/tickets")
def bot_support_list(status: str = "OPEN", page: int = 1, page_size: int = 10,
                     telegram_user_id: str = None,
                     _identity: dict = Depends(require_service("support:admin")),
                     db: Session = Depends(get_db)):
    return support_service.list(db, status.upper(), page, page_size, telegram_user_id)


@app.get("/v1/bot/support/users/{telegram_user_id}/tickets")
def bot_support_user_list(telegram_user_id: str, status: str = "ALL", page: int = 1,
                          _identity: dict = Depends(require_service("support:write")),
                          db: Session = Depends(get_db)):
    return support_service.list(db, status.upper(), page, 10, telegram_user_id)


@app.get("/v1/bot/support/tickets/{ticket_id}")
def bot_support_get(ticket_id: str, admin_view: bool = True, telegram_user_id: str = None,
                    message_page: int = 1, _identity: dict = Depends(require_service("support:admin")),
                    db: Session = Depends(get_db)):
    result = support_service.detail(db, ticket_id, admin_view=admin_view,
                                    telegram_user_id=telegram_user_id, message_page=message_page)
    db.commit()
    return result


@app.get("/v1/bot/support/users/{telegram_user_id}/tickets/{ticket_id}")
def bot_support_user_get(telegram_user_id: str, ticket_id: str, message_page: int = 1,
                         _identity: dict = Depends(require_service("support:write")),
                         db: Session = Depends(get_db)):
    result = support_service.detail(db, ticket_id, admin_view=False,
                                    telegram_user_id=telegram_user_id, message_page=message_page)
    db.commit()
    return result


@app.post("/v1/bot/support/tickets/{ticket_id}/messages")
def bot_support_message(ticket_id: str, value: SupportMessageRequest,
                        _identity: dict = Depends(require_service("support:write")),
                        db: Session = Depends(get_db)):
    row = support_service.add_user_message(db, ticket_id, value.telegram_user_id, value.message,
                                           value.attachment, value.idempotency_key)
    db.commit()
    return ticket_payload(row, db.get(User, row.user_id))


@app.post("/v1/bot/support/tickets/{ticket_id}/reply")
def bot_support_reply(ticket_id: str, value: SupportTicketActionRequest,
                      _identity: dict = Depends(require_service("support:admin")),
                      db: Session = Depends(get_db)):
    row, message = support_service.reply(db, ticket_id, value.telegram_user_id, value.message,
                                         value.idempotency_key, value.attachment)
    db.flush()
    dedupe_key = f"support-reply:{message.id}"
    if not db.scalar(select(BotNotification).where(BotNotification.dedupe_key == dedupe_key)):
        db.add(BotNotification(user_id=row.user_id, telegram_user_id=row.telegram_user_id,
                               notification_type="SUPPORT_ADMIN_REPLY",
                               payload={"ticket_id": row.id, "message_id": message.id},
                               dedupe_key=dedupe_key))
    db.commit()
    return ticket_payload(row, db.get(User, row.user_id))


@app.post("/v1/bot/support/tickets/{ticket_id}/close")
def bot_support_close(ticket_id: str, value: SupportTicketActionRequest,
                      _identity: dict = Depends(require_service("support:admin")),
                      db: Session = Depends(get_db)):
    row = support_service.transition(db, ticket_id, value.telegram_user_id, "close", value.message)
    db.commit()
    return ticket_payload(row, db.get(User, row.user_id))


@app.post("/v1/bot/support/tickets/{ticket_id}/reopen")
def bot_support_reopen(ticket_id: str, value: SupportTicketActionRequest,
                       _identity: dict = Depends(require_service("support:admin")),
                       db: Session = Depends(get_db)):
    row = support_service.transition(db, ticket_id, value.telegram_user_id, "reopen", value.message)
    db.commit(); return ticket_payload(row, db.get(User, row.user_id))


@app.post("/v1/bot/support/tickets/{ticket_id}/block")
def bot_support_block(ticket_id: str, value: SupportTicketActionRequest,
                      _identity: dict = Depends(require_service("support:admin")),
                      db: Session = Depends(get_db)):
    row = support_service.transition(db, ticket_id, value.telegram_user_id, "block", value.message)
    db.commit()
    return ticket_payload(row, db.get(User, row.user_id))


@app.post("/v1/bot/support/tickets/{ticket_id}/unblock")
def bot_support_unblock(ticket_id: str, value: SupportTicketActionRequest,
                        _identity: dict = Depends(require_service("support:admin")),
                        db: Session = Depends(get_db)):
    row = support_service.transition(db, ticket_id, value.telegram_user_id, "unblock", value.message)
    db.commit(); return ticket_payload(row, db.get(User, row.user_id))


@app.get("/v1/bot/support/dashboard")
def bot_support_dashboard(_identity: dict = Depends(require_service("support:admin")),
                          db: Session = Depends(get_db)):
    result = support_service.dashboard(db)
    state = db.get(ServerSetting, "support_admin_dashboard")
    stored = state.value or {} if state else {}
    result["message_id"] = int(stored.get("message_id", 0))
    result["chat_id"] = stored.get("chat_id")
    result["message_thread_id"] = stored.get("message_thread_id")
    return result


@app.post("/v1/bot/support/dashboard/message")
def bot_support_dashboard_message(value: SupportDashboardStateRequest,
                                  _identity: dict = Depends(require_service("support:admin")),
                                  db: Session = Depends(get_db)):
    state = db.get(ServerSetting, "support_admin_dashboard")
    payload = {
        "message_id": value.message_id,
        "telegram_user_id": value.telegram_user_id,
        "chat_id": value.chat_id,
        "message_thread_id": value.message_thread_id,
    }
    if state is None:
        state = ServerSetting(key="support_admin_dashboard", value=payload); db.add(state)
    else:
        state.value = payload
    db.commit(); return payload


@app.post("/v1/bot/support/tickets/{ticket_id}/forum-thread")
def bot_support_forum_thread(ticket_id: str, value: SupportForumThreadRequest,
                             _identity: dict = Depends(require_service("support:admin")),
                             db: Session = Depends(get_db)):
    row = db.get(SupportTicket, ticket_id)
    if not row: raise LicenseError("SUPPORT_TICKET_NOT_FOUND", 404)
    if (row.forum_topic_creation_key == value.idempotency_key
            and row.forum_chat_id == value.forum_chat_id
            and row.forum_message_thread_id == value.message_thread_id):
        return ticket_payload(row, db.get(User, row.user_id))
    collision = db.scalar(select(SupportTicket).where(
        SupportTicket.forum_chat_id == value.forum_chat_id,
        SupportTicket.forum_message_thread_id == value.message_thread_id,
        SupportTicket.id != ticket_id,
    ))
    if collision: raise LicenseError("SUPPORT_FORUM_THREAD_ALREADY_LINKED", 409)
    if row.forum_topic_creation_key and row.forum_topic_creation_key != value.idempotency_key:
        raise LicenseError("SUPPORT_FORUM_TOPIC_KEY_MISMATCH", 409)
    row.forum_chat_id = value.forum_chat_id
    row.forum_message_thread_id = value.message_thread_id
    row.forum_topic_name = value.topic_name
    row.forum_topic_created_at = row.forum_topic_created_at or utcnow()
    row.forum_topic_state = value.topic_state
    row.forum_topic_creation_key = value.idempotency_key
    row.forum_initial_message_id = value.initial_message_id
    db.add(AdminAction(admin_id="telegram-admin:" + value.telegram_user_id,
                       action="link-support-forum-topic", target_type="support-ticket", target_id=row.id,
                       action_metadata={"forum_chat_id": value.forum_chat_id,
                                        "message_thread_id": value.message_thread_id,
                                        "topic_state": value.topic_state}))
    db.commit(); return ticket_payload(row, db.get(User, row.user_id))


@app.get("/v1/bot/support/forum-threads/{message_thread_id}")
def bot_support_by_forum_thread(message_thread_id: int, forum_chat_id: Optional[int] = None,
                                _identity: dict = Depends(require_service("support:admin")),
                                db: Session = Depends(get_db)):
    filters = [SupportTicket.forum_message_thread_id == message_thread_id]
    if forum_chat_id is not None:
        filters.append(SupportTicket.forum_chat_id == forum_chat_id)
    row = db.scalar(select(SupportTicket).where(*filters))
    if not row: raise LicenseError("SUPPORT_TICKET_NOT_FOUND", 404)
    return support_service.detail(db, row.id, admin_view=True)


@app.post("/v1/bot/support/messages/{message_id}/forum-mapping")
def bot_support_forum_message(message_id: str, value: SupportForumMessageRequest,
                              _identity: dict = Depends(require_service("support:admin")),
                              db: Session = Depends(get_db)):
    row = db.get(SupportMessage, message_id)
    if not row: raise LicenseError("SUPPORT_MESSAGE_NOT_FOUND", 404)
    incoming = str(value.forum_message_id)
    if row.forum_message_id and row.forum_message_id != incoming:
        raise LicenseError("SUPPORT_FORUM_MESSAGE_ALREADY_LINKED", 409)
    if not row.forum_message_id:
        row.forum_message_id = incoming
        db.add(AdminAction(admin_id="telegram-admin:" + value.telegram_user_id,
                           action="link-support-forum-message", target_type="support-message",
                           target_id=row.id, action_metadata={"forum_message_id": incoming}))
        db.commit()
    return {"message_id": row.id, "forum_message_id": row.forum_message_id}


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
