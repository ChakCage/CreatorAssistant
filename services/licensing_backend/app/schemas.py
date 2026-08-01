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


class TelegramUserRequest(BaseModel):
    telegram_user_id: str = Field(min_length=1, max_length=64)
    username: Optional[str] = Field(default=None, max_length=64)
    first_name: Optional[str] = Field(default=None, max_length=160)
    language_code: Optional[str] = Field(default=None, max_length=16)


class CheckoutRequest(BaseModel):
    telegram_user_id: str = Field(min_length=1, max_length=64)
    plan_id: str
    price_id: str
    idempotency_key: str = Field(min_length=8, max_length=120)
    return_url: Optional[str] = Field(default=None, max_length=600)


class BotUserRequest(BaseModel):
    telegram_user_id: str = Field(min_length=1, max_length=64)


class BetaRedeemRequest(BotUserRequest):
    invite_code: str = Field(min_length=10, max_length=32)


class BotDeviceRequest(BotUserRequest):
    device_id: str
    confirmed: bool = False


class NotificationResultRequest(BaseModel):
    success: bool
    error: str = Field(default="", max_length=500)


class SupportTicketCreateRequest(BotUserRequest):
    category: str = Field(pattern="^(activation|application|render|other)$")
    message: str = Field(min_length=3, max_length=4000)
    attachment: dict[str, Any] = Field(default_factory=dict)


class SupportTicketActionRequest(BaseModel):
    telegram_user_id: str = Field(min_length=1, max_length=64)
    message: str = Field(default="", max_length=4000)


class FreeMembershipRequest(BotUserRequest):
    status: str = Field(min_length=1, max_length=40)
    is_member: Optional[bool] = None


class FreeConfigUpdateRequest(BaseModel):
    telegram_user_id: str = Field(min_length=1, max_length=64)
    enabled: Optional[bool] = None
    channel_chat_id: Optional[int] = None
    channel_title: Optional[str] = Field(default=None, max_length=200)
    channel_invite_url: Optional[str] = Field(default=None, max_length=600)
    recheck_enabled: Optional[bool] = None
    project_limit: Optional[int] = Field(default=None, ge=0, le=1000)
    shorts_source_limit: Optional[int] = Field(default=None, ge=0, le=1000)
    device_limit: Optional[int] = Field(default=None, ge=1, le=100)
    offer_version: Optional[str] = Field(default=None, max_length=40)


class FreeBlockRequest(BotUserRequest):
    blocked: bool = True


class FreeQuotaRequest(BaseModel):
    kind: str = Field(pattern="^(PROJECT|SHORTS_SOURCE)$")
    operation_key: str = Field(min_length=8, max_length=160)


class FreeQuotaFinishRequest(BaseModel):
    success: bool


class FreeEventRequest(BotUserRequest):
    event: str = Field(pattern="^(free_offer_opened|channel_link_clicked|membership_check_started|membership_check_failed)$")
    result: str = Field(default="SUCCESS", max_length=40)
    reason: str = Field(default="", max_length=80)


class AdminPriceRequest(BaseModel):
    plan_code: str = "beta"
    provider: str = Field(default="fake", max_length=40)
    amount_minor: int = Field(ge=1, le=1_000_000_000)
    currency: str = Field(default="RUB", min_length=3, max_length=3)


class AdminReleaseRequest(BaseModel):
    edition: str = "commercial"
    channel: str = "stable"
    version: str = Field(max_length=40)
    build_number: int = Field(default=0, ge=0)
    architecture: str = Field(default="x86_64", max_length=40)
    download_url: str = Field(max_length=600)
    sha256: str = Field(min_length=64, max_length=64)
    file_size: int = Field(default=0, ge=0)
    release_notes: str = ""
    minimum_supported_version: str = ""
    mandatory: bool = False
    manifest_schema_version: int = 1
    key_id: str = Field(default="", max_length=80)
    signature: str = ""
