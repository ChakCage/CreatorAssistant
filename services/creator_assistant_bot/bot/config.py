from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREATOR_BOT_", case_sensitive=False, populate_by_name=True)

    environment: str = "local"
    release_version: str = "development"
    release_commit: str = "unknown"
    token: str = ""
    backend_url: str = "http://127.0.0.1:18080"
    service_secret: str = Field(min_length=32)
    public_url: str = ""
    webhook_secret: str = ""
    webhook_path: str = "/telegram/webhook"
    health_port: int = 8081
    bind_host: str = "127.0.0.1"
    mock_telegram: bool = False
    support_url: str = ""
    admin_telegram_id: int = 0
    help_url: str = "https://example.invalid/creator-assistant/help"
    manage_webhook: bool = False
    run_notification_worker: bool = False
    allow_internal_backend_http: bool = False
    support_admin_notification_mode: str = Field(
        default="dashboard", validation_alias=AliasChoices("SUPPORT_ADMIN_NOTIFICATION_MODE", "CREATOR_BOT_SUPPORT_ADMIN_NOTIFICATION_MODE")
    )
    support_forum_enabled: bool = Field(
        default=False, validation_alias=AliasChoices("SUPPORT_FORUM_ENABLED", "CREATOR_BOT_SUPPORT_FORUM_ENABLED")
    )
    support_forum_chat_id: int = Field(
        default=0, validation_alias=AliasChoices("SUPPORT_FORUM_CHAT_ID", "CREATOR_BOT_SUPPORT_FORUM_CHAT_ID")
    )
    support_forum_topic_mode: str = Field(
        default="per_ticket", validation_alias=AliasChoices("SUPPORT_FORUM_TOPIC_MODE", "CREATOR_BOT_SUPPORT_FORUM_TOPIC_MODE")
    )
    support_forum_dashboard_topic_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "SUPPORT_FORUM_DASHBOARD_TOPIC_ENABLED",
            "CREATOR_BOT_SUPPORT_FORUM_DASHBOARD_TOPIC_ENABLED",
        ),
    )
    support_forum_relay_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("SUPPORT_FORUM_RELAY_ENABLED", "CREATOR_BOT_SUPPORT_FORUM_RELAY_ENABLED"),
    )

    @property
    def production(self) -> bool:
        return self.environment == "production"

    def validate_runtime(self) -> None:
        if self.support_admin_notification_mode not in {"dashboard", "compact", "full", "off"}:
            raise RuntimeError("Invalid support admin notification mode")
        if self.support_forum_enabled and not self.support_forum_chat_id:
            raise RuntimeError("Support forum chat ID is required when forum integration is enabled")
        if self.support_forum_topic_mode != "per_ticket":
            raise RuntimeError("Only per_ticket support forum topic mode is supported")
        if self.production:
            if not self.public_url.startswith("https://"):
                raise RuntimeError("Production bot webhook requires HTTPS")
            if len(self.webhook_secret) < 24:
                raise RuntimeError("Production bot webhook secret is required")
            if not self.token:
                raise RuntimeError("Telegram bot token is required")
            internal_http = self.allow_internal_backend_http and self.backend_url.startswith("http://api:")
            if not self.backend_url.startswith("https://") and not internal_http:
                raise RuntimeError("Production bot backend requires HTTPS")
            forbidden = ("change-me", "changeme", "placeholder", "example", "default")
            if any(marker in value.casefold() for marker in forbidden for value in
                   (self.token, self.webhook_secret, self.service_secret)):
                raise RuntimeError("Production bot refuses placeholder secrets")
