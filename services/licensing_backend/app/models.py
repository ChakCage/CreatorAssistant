from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return str(uuid.uuid4())


class UserStatus(str, enum.Enum): ACTIVE = "ACTIVE"; BLOCKED = "BLOCKED"; DELETED = "DELETED"
class ProductStatus(str, enum.Enum): ACTIVE = "ACTIVE"; INACTIVE = "INACTIVE"
class SubscriptionStatus(str, enum.Enum): PENDING = "PENDING"; ACTIVE = "ACTIVE"; GRACE = "GRACE"; EXPIRED = "EXPIRED"; CANCELLED = "CANCELLED"; BLOCKED = "BLOCKED"
class SubscriptionSource(str, enum.Enum): ADMIN = "ADMIN"; TRIAL = "TRIAL"; PAYMENT = "PAYMENT"; PROMO = "PROMO"
class ActivationCodeStatus(str, enum.Enum): CREATED = "CREATED"; USED = "USED"; EXPIRED = "EXPIRED"; REVOKED = "REVOKED"
class DeviceStatus(str, enum.Enum): ACTIVE = "ACTIVE"; DEACTIVATED = "DEACTIVATED"; BLOCKED = "BLOCKED"
class SessionStatus(str, enum.Enum): ACTIVE = "ACTIVE"; REVOKED = "REVOKED"; EXPIRED = "EXPIRED"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    telegram_user_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    email: Mapped[Optional[str]] = mapped_column(String(320), unique=True)
    status: Mapped[UserStatus] = mapped_column(Enum(UserStatus), default=UserStatus.ACTIVE)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[ProductStatus] = mapped_column(Enum(ProductStatus), default=ProductStatus.ACTIVE)


class Plan(TimestampMixin, Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    code: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160))
    duration_days: Mapped[int] = mapped_column(Integer)
    device_limit: Mapped[int] = mapped_column(Integer, default=1)
    features: Mapped[list] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (UniqueConstraint("product_id", "code", name="uq_plan_product_code"),)


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), index=True)
    status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), default=SubscriptionStatus.PENDING)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source: Mapped[SubscriptionSource] = mapped_column(Enum(SubscriptionSource))
    external_reference: Mapped[Optional[str]] = mapped_column(String(200))
    plan: Mapped[Plan] = relationship()
    user: Mapped[User] = relationship()


class ActivationCode(Base):
    __tablename__ = "activation_codes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    subscription_id: Mapped[str] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[ActivationCodeStatus] = mapped_column(Enum(ActivationCodeStatus), default=ActivationCodeStatus.CREATED)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    used_by_device_id: Mapped[Optional[str]] = mapped_column(String(36))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    subscription: Mapped[Subscription] = relationship()


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    subscription_id: Mapped[str] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    installation_id: Mapped[str] = mapped_column(String(80), index=True)
    friendly_name: Mapped[str] = mapped_column(String(160))
    os_version: Mapped[str] = mapped_column(String(160))
    app_version: Mapped[str] = mapped_column(String(40))
    edition: Mapped[str] = mapped_column(String(40))
    hardware_fingerprint_hash: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[DeviceStatus] = mapped_column(Enum(DeviceStatus), default=DeviceStatus.ACTIVE)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("subscription_id", "installation_id", name="uq_device_subscription_installation"),)


class LicenseSession(Base):
    __tablename__ = "license_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    token_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus), default=SessionStatus.ACTIVE)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_refresh_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    client_version: Mapped[str] = mapped_column(String(40))
    device: Mapped[Device] = relationship()


class LicenseEvent(Base):
    __tablename__ = "license_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    subscription_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    device_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    result: Mapped[str] = mapped_column(String(40))
    reason_code: Mapped[str] = mapped_column(String(80), default="")
    ip_hash: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    event_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AdminAction(Base):
    __tablename__ = "admin_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    admin_id: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text, default="")
    action_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

Index("ix_devices_active_subscription", Device.subscription_id, Device.status)
