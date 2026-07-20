from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class PublishingPlatform(str, Enum):
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"


class PublishingMode(str, Enum):
    DRY_RUN = "DRY_RUN"
    PRIVATE_TEST = "PRIVATE_TEST"
    REAL = "REAL"


class ConnectionStatus(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"
    EXPIRED = "EXPIRED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class PublishingAttemptStatus(str, Enum):
    RENDERED = "RENDERED"
    UPLOAD_QUEUED = "UPLOAD_QUEUED"
    PLANNED = "PLANNED"
    READY = "READY"
    UPLOADING = "UPLOADING"
    REMOTE_PROCESSING = "REMOTE_PROCESSING"
    UPLOADED_PRIVATE = "UPLOADED_PRIVATE"
    SCHEDULED_REMOTE = "SCHEDULED_REMOTE"
    PROCESSING = "PROCESSING"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class PublishingCredential:
    """Non-secret pointer to secrets stored by the operating system."""

    account_id: str
    provider: str = "windows-credential-manager"
    target_name: str = ""
    updated_at: str = ""


@dataclass
class PublishingAccount:
    account_id: str
    platform: str
    display_name: str = ""
    remote_user_id: str = ""
    status: str = ConnectionStatus.DISCONNECTED.value
    granted_scopes: list[str] = field(default_factory=list)
    token_expires_at: str = ""
    last_validation: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    capability: str = "private-only"
    review_required: bool = False
    credential: PublishingCredential | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PublishingAccount":
        value = dict(raw)
        if value.get("credential"):
            value["credential"] = PublishingCredential(**value["credential"])
        return cls(**value)


@dataclass
class PlatformPublishingProfile:
    profile_id: str
    platform: str
    account_id: str = ""
    name: str = ""
    composition_template: dict[str, Any] = field(default_factory=dict)
    title_template: str = "{title}"
    description_template: str = ""
    tags: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishingAttempt:
    attempt_id: str
    short_id: str
    platform: str
    account_id: str
    local_file: str
    mode: str = PublishingMode.DRY_RUN.value
    scheduled_at: str = ""
    upload_strategy: str = "REMOTE_SCHEDULE"
    status: str = PublishingAttemptStatus.PLANNED.value
    remote_id: str = ""
    upload_url_key: str = ""
    uploaded_bytes: int = 0
    total_bytes: int = 0
    progress: float = 0.0
    processing_status: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishingReceipt:
    receipt_id: str
    attempt_id: str
    platform: str
    account_id: str
    remote_id: str
    remote_url: str = ""
    published_at: str = ""
    scheduled_at: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
