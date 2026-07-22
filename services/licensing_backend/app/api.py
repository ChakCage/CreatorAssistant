from __future__ import annotations

import hmac
import hashlib
import uuid
from datetime import timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import hash_admin_token, load_settings
from .db import get_db
from .models import ActivationCode, ActivationCodeStatus, AdminAction, Device, DeviceStatus, LicenseEvent, LicenseSession, Plan, SessionStatus, Subscription, SubscriptionStatus, User, UserStatus, utcnow
from .rate_limit import RateLimiter
from .schemas import ActivateRequest, AdminCodeRequest, AdminGrantRequest, AdminUserRequest, DeactivateRequest, RefreshRequest
from .service import LicenseError, LicenseManager, aware, iso


settings = load_settings(); manager = LicenseManager(settings); limiter = RateLimiter(settings.redis_url)
app = FastAPI(title="Creator Assistant Licensing", version="1.0.0")


@app.exception_handler(LicenseError)
def license_error(_request: Request, exc: LicenseError):
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, **exc.details}, "request_id": str(uuid.uuid4())})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def refresh_session(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> LicenseSession:
    if not authorization.startswith("Bearer "): raise HTTPException(401, "Missing license credential")
    return manager.session_from_refresh(db, authorization[7:])


def require_admin(x_admin_token: str = Header(default="")) -> str:
    if not settings.admin_token_hash or not hmac.compare_digest(hash_admin_token(x_admin_token), settings.admin_token_hash):
        raise HTTPException(401, "Admin authorization required")
    return "api-admin"


@app.get("/health")
def health(): return {"status": "ok"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1")); return {"status": "ready"}


@app.get("/version")
def version(): return {"version": app.version, "environment": settings.environment}


@app.get("/v1/licenses/keys")
def keys(): return {"keys": {settings.signing_key_id: manager.signer.public_key_b64()}, "schema_version": 1}


@app.post("/v1/licenses/activate")
def activate(value: ActivateRequest, request: Request, db: Session = Depends(get_db)):
    code_key = hashlib.sha256(value.activation_code.upper().encode()).hexdigest()[:16]
    key = f"activate:{client_ip(request)}:{value.installation_id[:16]}:{code_key}"
    if not limiter.allow(key, 12, 300): raise LicenseError("TOO_MANY_ATTEMPTS", 429)
    try:
        result = manager.activate(db, value, ip=client_ip(request), user_agent=request.headers.get("user-agent", "")); db.commit(); return result
    except Exception:
        db.commit(); raise


@app.post("/v1/licenses/refresh")
def refresh(value: RefreshRequest, request: Request, db: Session = Depends(get_db)):
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
def devices(session: LicenseSession = Depends(refresh_session), db: Session = Depends(get_db)):
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
