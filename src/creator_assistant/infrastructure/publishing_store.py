from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, TypeVar

from creator_assistant.domain.publishing import (
    PlatformPublishingProfile, PublishingAccount, PublishingAttempt, PublishingReceipt,
)
from creator_assistant.infrastructure.settings_store import config_root

T = TypeVar("T")


class PublishingStore:
    """Atomic, non-secret publishing metadata store."""

    forbidden_keys = {"access_token", "refresh_token", "client_secret", "token"}

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or (config_root() / "publishing"))
        self.path = self.root / "publishing.json"

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": 1, "accounts": [], "profiles": [], "attempts": [], "receipts": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"schema_version": 1, "accounts": [], "profiles": [], "attempts": [], "receipts": []}
        return value if isinstance(value, dict) else {}

    def _save(self, value: dict[str, Any]) -> None:
        self._reject_secrets(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(self.path))

    def _reject_secrets(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in self.forbidden_keys and item:
                    raise ValueError(f"Secret field cannot be persisted: {key}")
                self._reject_secrets(item)
        elif isinstance(value, list):
            for item in value:
                self._reject_secrets(item)

    def _replace(self, collection: str, key: str, key_value: str, raw: dict[str, Any]) -> None:
        data = self._load()
        values = [item for item in data.get(collection, []) if str(item.get(key)) != str(key_value)]
        values.append(raw)
        data[collection] = values
        self._save(data)

    def accounts(self) -> list[PublishingAccount]:
        return [PublishingAccount.from_dict(item) for item in self._load().get("accounts", [])]

    def save_account(self, account: PublishingAccount) -> None:
        self._replace("accounts", "account_id", account.account_id, account.to_dict())

    def delete_account(self, account_id: str) -> None:
        data = self._load()
        data["accounts"] = [item for item in data.get("accounts", []) if item.get("account_id") != account_id]
        self._save(data)

    def profiles(self) -> list[PlatformPublishingProfile]:
        return [PlatformPublishingProfile(**item) for item in self._load().get("profiles", [])]

    def save_profile(self, profile: PlatformPublishingProfile) -> None:
        self._replace("profiles", "profile_id", profile.profile_id, profile.to_dict())

    def attempts(self) -> list[PublishingAttempt]:
        return [PublishingAttempt(**item) for item in self._load().get("attempts", [])]

    def save_attempt(self, attempt: PublishingAttempt) -> None:
        self._replace("attempts", "attempt_id", attempt.attempt_id, attempt.to_dict())

    def receipts(self) -> list[PublishingReceipt]:
        return [PublishingReceipt(**item) for item in self._load().get("receipts", [])]

    def save_receipt(self, receipt: PublishingReceipt) -> None:
        self._replace("receipts", "receipt_id", receipt.receipt_id, receipt.to_dict())
