from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PublishingConnector(ABC):
    @abstractmethod
    def authenticate(self) -> bool: ...

    @abstractmethod
    def validate_account(self) -> dict[str, Any]: ...

    @abstractmethod
    def upload(self, artifact_path: str, metadata: dict[str, Any]) -> str: ...

    @abstractmethod
    def schedule(self, upload_id: str, scheduled_at: str) -> str: ...

    @abstractmethod
    def get_status(self, operation_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def retry(self, operation_id: str) -> bool: ...

    @abstractmethod
    def cancel(self, operation_id: str) -> bool: ...

    @abstractmethod
    def refresh_token(self) -> bool: ...


class _MockPublishingConnector(PublishingConnector):
    platform = "mock"

    def authenticate(self) -> bool:
        return True

    def validate_account(self) -> dict[str, Any]:
        return {"valid": True, "mock": True, "platform": self.platform}

    def upload(self, artifact_path: str, metadata: dict[str, Any]) -> str:
        return f"mock:{self.platform}:upload:{metadata.get('short_id', 'short')}"

    def schedule(self, upload_id: str, scheduled_at: str) -> str:
        return f"mock:{self.platform}:schedule:{upload_id}"

    def get_status(self, operation_id: str) -> dict[str, Any]:
        return {"operation_id": operation_id, "status": "mock_planned", "remote": False}

    def retry(self, operation_id: str) -> bool:
        return True

    def cancel(self, operation_id: str) -> bool:
        return True

    def refresh_token(self) -> bool:
        return True


class YouTubePublishingConnector(_MockPublishingConnector):
    platform = "youtube"


class TikTokPublishingConnector(_MockPublishingConnector):
    platform = "tiktok"
