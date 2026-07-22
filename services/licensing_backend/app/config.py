from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str
    redis_url: str
    activation_pepper: str
    signing_key_id: str
    signing_private_key: bytes
    admin_token_hash: str
    token_hours: int
    offline_grace_hours: int
    refresh_days: int
    activation_ttl_minutes: int
    max_code_attempts: int
    minimum_app_version: str
    recommended_app_version: str

    @property
    def production(self) -> bool:
        return self.environment == "production"


def _decode_key(value: str) -> bytes:
    if not value:
        return b""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def load_settings() -> Settings:
    environment = os.getenv("LICENSE_ENV", "local").strip().lower()
    database_url = os.getenv("LICENSE_DATABASE_URL", "postgresql+psycopg://creator_assistant:creator_assistant@localhost:5432/creator_assistant")
    pepper = os.getenv("LICENSE_ACTIVATION_PEPPER", "")
    private_key = _decode_key(os.getenv("LICENSE_SIGNING_PRIVATE_KEY", ""))
    if environment in {"production", "staging"}:
        if not database_url.startswith("postgresql+"):
            raise RuntimeError("Production/staging licensing backend requires PostgreSQL")
        if len(pepper) < 32 or len(private_key) != 32:
            raise RuntimeError("LICENSE_ACTIVATION_PEPPER and a 32-byte Ed25519 private key are required")
    else:
        # Ephemeral values are safe for one-process local/test use only and are never persisted.
        pepper = pepper or secrets.token_urlsafe(32)
        private_key = private_key or secrets.token_bytes(32)
    return Settings(
        environment=environment, database_url=database_url,
        redis_url=os.getenv("LICENSE_REDIS_URL", ""), activation_pepper=pepper,
        signing_key_id=os.getenv("LICENSE_SIGNING_KEY_ID", "local-ephemeral"),
        signing_private_key=private_key, admin_token_hash=os.getenv("LICENSE_ADMIN_TOKEN_HASH", ""),
        token_hours=int(os.getenv("LICENSE_TOKEN_HOURS", "48")),
        offline_grace_hours=int(os.getenv("LICENSE_OFFLINE_GRACE_HOURS", "72")),
        refresh_days=int(os.getenv("LICENSE_REFRESH_DAYS", "30")),
        activation_ttl_minutes=int(os.getenv("LICENSE_ACTIVATION_TTL_MINUTES", "30")),
        max_code_attempts=int(os.getenv("LICENSE_MAX_CODE_ATTEMPTS", "8")),
        minimum_app_version=os.getenv("LICENSE_MINIMUM_APP_VERSION", "0.1.0"),
        recommended_app_version=os.getenv("LICENSE_RECOMMENDED_APP_VERSION", "0.1.0"),
    )


def hash_admin_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
