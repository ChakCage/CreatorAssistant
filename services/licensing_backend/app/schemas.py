from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class ActivateRequest(BaseModel):
    activation_code: str = Field(min_length=12, max_length=32)
    installation_id: str = Field(min_length=20, max_length=80)
    device_name: str = Field(min_length=1, max_length=160)
    os_version: str = Field(max_length=160)
    app_version: str = Field(max_length=40)
    edition: str = Field(pattern="^commercial$")


class RefreshRequest(BaseModel):
    refresh_credential: str = Field(min_length=32, max_length=300)
    installation_id: str = Field(min_length=20, max_length=80)
    app_version: str = Field(max_length=40)


class EntitlementResponse(BaseModel):
    entitlement_token: str
    refresh_credential: str = ""
    subscription: dict[str, Any]
    device: dict[str, Any]
    refresh: dict[str, Any]
    server_time: str
    notices: list[str] = []
    minimum_supported_version: str = ""
    recommended_app_version: str = ""


class AdminUserRequest(BaseModel):
    email: Optional[str] = None
    telegram_user_id: Optional[str] = None


class AdminGrantRequest(BaseModel):
    user_id: str
    plan_code: str = "beta"
    days: Optional[int] = Field(default=None, ge=1, le=3650)
    idempotency_key: Optional[str] = Field(default=None, max_length=120)
    reason: str = ""


class AdminCodeRequest(BaseModel):
    user_id: str
    ttl_minutes: int = Field(default=30, ge=1, le=1440)
    reason: str = ""


class DeactivateRequest(BaseModel):
    reason: str = "user_request"
