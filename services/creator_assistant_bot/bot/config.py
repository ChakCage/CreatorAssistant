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
    support_url: str = "https://t.me/creator_assistant_support"
    help_url: str = "https://example.invalid/creator-assistant/help"

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
