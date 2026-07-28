from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREATOR_BOT_", case_sensitive=False)

    environment: str = "local"
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

    @property
    def production(self) -> bool:
        return self.environment == "production"

    def validate_runtime(self) -> None:
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
