from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from creator_assistant.domain.publishing import ConnectionStatus, PublishingAccount, PublishingCredential
from creator_assistant.infrastructure.credential_store import WindowsCredentialStore
from creator_assistant.infrastructure.publishing_store import PublishingStore
from creator_assistant.services.publishing.oauth import OAuthService
from creator_assistant.services.publishing.tiktok import TikTokPublishingConnector
from creator_assistant.services.publishing.youtube import YouTubePublishingConnector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PublishingAccountService:
    def __init__(self, store: PublishingStore, credentials: WindowsCredentialStore, oauth: OAuthService | None = None) -> None:
        self.store = store; self.credentials = credentials; self.oauth = oauth or OAuthService(credentials)

    def connect_google(self, config_id: str, account_id: str = "") -> PublishingAccount:
        account_id = account_id or f"youtube-{uuid.uuid4().hex[:12]}"
        token = self.oauth.authorize_google(account_id, config_id)
        info = YouTubePublishingConnector(account_id, self.credentials, self.oauth.http).validate_account()
        account = PublishingAccount(
            account_id, "youtube", info.get("display_name", "YouTube"), info.get("channel_id", ""),
            ConnectionStatus.CONNECTED.value, list(str(token.get("scope") or "").split()),
            str(token.get("expires_at") or ""), _now(), {"privacyStatus": "private", "category": "22", "madeForKids": False},
            "private-only", False, PublishingCredential(account_id, target_name=self.credentials.target(account_id), updated_at=_now()),
        )
        self.store.save_account(account); return account

    def connect_tiktok(self, config_id: str, account_id: str = "") -> PublishingAccount:
        account_id = account_id or f"tiktok-{uuid.uuid4().hex[:12]}"
        token = self.oauth.authorize_tiktok(account_id, config_id)
        info = TikTokPublishingConnector(account_id, self.credentials, self.oauth.http).validate_account()
        scopes = str(token.get("scope") or "").replace(",", " ").split()
        account = PublishingAccount(
            account_id, "tiktok", info.get("display_name", "TikTok"), str(token.get("open_id") or ""),
            ConnectionStatus.CONNECTED.value, scopes, str(token.get("expires_at") or ""), _now(),
            {"post_mode": "draft", "privacy_level": "SELF_ONLY"},
            "app-review-required" if "video.publish" in scopes else "private-only", True,
            PublishingCredential(account_id, target_name=self.credentials.target(account_id), updated_at=_now()),
        )
        self.store.save_account(account); return account

    def validate(self, account_id: str) -> PublishingAccount:
        account = next(item for item in self.store.accounts() if item.account_id == account_id)
        try:
            connector = YouTubePublishingConnector(account_id, self.credentials) if account.platform == "youtube" else TikTokPublishingConnector(account_id, self.credentials)
            info = connector.validate_account(); account.status = ConnectionStatus.CONNECTED.value if info.get("valid") else ConnectionStatus.NEEDS_REVIEW.value
            account.display_name = str(info.get("display_name") or account.display_name); account.last_validation = _now()
        except Exception:
            account.status = ConnectionStatus.EXPIRED.value; account.last_validation = _now()
        self.store.save_account(account); return account

    def disconnect(self, account_id: str) -> None:
        self.credentials.delete(account_id); self.store.delete_account(account_id)
