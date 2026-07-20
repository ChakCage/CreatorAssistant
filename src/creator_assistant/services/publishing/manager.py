from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from creator_assistant.domain.publishing import (
    ConnectionStatus, PublishingAccount, PublishingAttempt, PublishingAttemptStatus,
    PublishingMode, PublishingReceipt,
)
from creator_assistant.infrastructure.credential_store import WindowsCredentialStore
from creator_assistant.infrastructure.publishing_store import PublishingStore
from creator_assistant.services.publishing.tiktok import TikTokPublishingConnector
from creator_assistant.services.publishing.youtube import YouTubePublishingConnector


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PublishingManager:
    def __init__(self, store: PublishingStore | None = None, credentials: WindowsCredentialStore | None = None, connector_factory: Callable[[str, str], Any] | None = None) -> None:
        self.store = store or PublishingStore()
        self.credentials = credentials or WindowsCredentialStore()
        self.connector_factory = connector_factory or self._connector
        self._cancelled: set[str] = set()

    def _connector(self, platform: str, account_id: str):
        if platform == "youtube": return YouTubePublishingConnector(account_id, self.credentials)
        if platform == "tiktok": return TikTokPublishingConnector(account_id, self.credentials)
        raise ValueError(f"Unsupported publishing platform: {platform}")

    def create_attempt(self, short_id: str, platform: str, account_id: str, local_file: str, *, mode: str = PublishingMode.DRY_RUN.value, scheduled_at: str = "", metadata: dict[str, Any] | None = None) -> PublishingAttempt:
        payload = {"short": short_id, "platform": platform, "account": account_id, "file": str(Path(local_file).resolve()), "scheduled": scheduled_at, "metadata": metadata or {}}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        existing = next((item for item in self.store.attempts() if item.idempotency_key == key and item.status != PublishingAttemptStatus.CANCELLED.value), None)
        if existing: return existing
        attempt = PublishingAttempt(
            attempt_id=f"publish-{uuid.uuid4().hex[:14]}", short_id=short_id, platform=platform,
            account_id=account_id, local_file=local_file, mode=mode, scheduled_at=scheduled_at,
            metadata=dict(metadata or {}), idempotency_key=key, created_at=utc_now(), updated_at=utc_now(),
        )
        self.store.save_attempt(attempt); return attempt

    def execute(self, attempt_id: str) -> PublishingAttempt:
        attempt = next((item for item in self.store.attempts() if item.attempt_id == attempt_id), None)
        if not attempt: raise KeyError(attempt_id)
        path = Path(attempt.local_file)
        if not path.is_file():
            attempt.status = PublishingAttemptStatus.FAILED.value; attempt.error = "Local artifact not found"; self._save(attempt); return attempt
        if attempt.mode == PublishingMode.DRY_RUN.value:
            attempt.total_bytes = path.stat().st_size; attempt.uploaded_bytes = 0; attempt.progress = 100
            attempt.status = PublishingAttemptStatus.SCHEDULED.value if attempt.scheduled_at else PublishingAttemptStatus.READY.value
            attempt.processing_status = "DRY_RUN: network disabled"
            self._save(attempt); return attempt
        if not bool(attempt.metadata.get("network_approved", False)):
            attempt.status = PublishingAttemptStatus.NEEDS_REVIEW.value
            attempt.error = "Network publishing requires explicit user confirmation"
            self._save(attempt); return attempt
        account = next((item for item in self.store.accounts() if item.account_id == attempt.account_id), None)
        if not account or account.status != ConnectionStatus.CONNECTED.value:
            attempt.status = PublishingAttemptStatus.NEEDS_REVIEW.value; attempt.error = "Publishing account is not connected"; self._save(attempt); return attempt
        metadata = dict(account.defaults); metadata.update(attempt.metadata)
        scopes = set(account.granted_scopes)
        required_scope = "https://www.googleapis.com/auth/youtube.upload" if attempt.platform == "youtube" else ("video.publish" if metadata.get("post_mode") == "direct" else "video.upload")
        if required_scope not in scopes:
            attempt.status = PublishingAttemptStatus.NEEDS_REVIEW.value; attempt.error = f"Missing required scope: {required_scope}"; self._save(attempt); return attempt
        if attempt.mode == PublishingMode.PRIVATE_TEST.value:
            if attempt.platform == "youtube": metadata["privacyStatus"] = "private"
            else: metadata.update({"privacy_level": "SELF_ONLY", "post_mode": metadata.get("post_mode", "draft")})
        elif attempt.mode == PublishingMode.REAL.value:
            if account.review_required or account.capability in {"private-only", "app-review-required"}:
                attempt.status = PublishingAttemptStatus.NEEDS_REVIEW.value; attempt.error = "Platform/app review does not allow public publishing"; self._save(attempt); return attempt
            has_private_test = any(r.account_id == account.account_id and r.status == PublishingMode.PRIVATE_TEST.value for r in self.store.receipts())
            if not has_private_test:
                attempt.status = PublishingAttemptStatus.NEEDS_REVIEW.value; attempt.error = "A successful private test is required before real publishing"; self._save(attempt); return attempt
        connector = self.connector_factory(attempt.platform, attempt.account_id)
        attempt.status = PublishingAttemptStatus.UPLOADING.value; attempt.total_bytes = path.stat().st_size; attempt.error = ""; self._save(attempt)
        try:
            remote_id = connector.upload(str(path), metadata, progress=lambda done, total: self._progress(attempt, done, total), cancelled=lambda: attempt.attempt_id in self._cancelled)
            attempt.remote_id = remote_id; attempt.progress = 100
            if attempt.platform == "youtube" and attempt.scheduled_at:
                connector.schedule(remote_id, attempt.scheduled_at); attempt.status = PublishingAttemptStatus.SCHEDULED.value
            else:
                attempt.status = PublishingAttemptStatus.PROCESSING.value
            self._save(attempt)
            receipt = PublishingReceipt(f"receipt-{uuid.uuid4().hex[:14]}", attempt.attempt_id, attempt.platform, attempt.account_id, remote_id, self.remote_url(attempt), utc_now(), attempt.scheduled_at, attempt.mode)
            self.store.save_receipt(receipt)
        except InterruptedError as exc:
            attempt.status = PublishingAttemptStatus.CANCELLED.value; attempt.error = str(exc); self._save(attempt)
        except Exception as exc:
            attempt.status = PublishingAttemptStatus.FAILED.value; attempt.error = str(exc); self._save(attempt)
        return attempt

    def refresh_status(self, attempt_id: str) -> PublishingAttempt:
        attempt = next((item for item in self.store.attempts() if item.attempt_id == attempt_id), None)
        if not attempt or not attempt.remote_id: raise KeyError(attempt_id)
        value = self.connector_factory(attempt.platform, attempt.account_id).get_status(attempt.remote_id)
        attempt.processing_status = str(value.get("status") or (value.get("processingDetails") or {}).get("processingStatus") or "")
        if attempt.processing_status.casefold() in {"complete", "published", "publish_complete"}:
            attempt.status = PublishingAttemptStatus.PUBLISHED.value
        elif attempt.processing_status.casefold() in {"failed", "rejected"}:
            attempt.status = PublishingAttemptStatus.FAILED.value; attempt.error = str(value.get("fail_reason") or "Remote processing failed")
        self._save(attempt); return attempt

    def retry(self, attempt_id: str) -> PublishingAttempt:
        attempt = next(item for item in self.store.attempts() if item.attempt_id == attempt_id)
        if attempt.status not in {PublishingAttemptStatus.FAILED.value, PublishingAttemptStatus.NEEDS_REVIEW.value}:
            return attempt
        attempt.status = PublishingAttemptStatus.PLANNED.value; attempt.error = ""; self._save(attempt); return self.execute(attempt_id)

    def cancel(self, attempt_id: str) -> None:
        self._cancelled.add(attempt_id)
        attempt = next((item for item in self.store.attempts() if item.attempt_id == attempt_id), None)
        if attempt:
            attempt.status = PublishingAttemptStatus.CANCELLED.value; self._save(attempt)

    def _progress(self, attempt: PublishingAttempt, done: int, total: int) -> None:
        attempt.uploaded_bytes = done; attempt.total_bytes = total; attempt.progress = done * 100 / max(1, total); self._save(attempt)

    def _save(self, attempt: PublishingAttempt) -> None:
        attempt.updated_at = utc_now(); self.store.save_attempt(attempt)

    @staticmethod
    def remote_url(attempt: PublishingAttempt) -> str:
        if attempt.platform == "youtube" and attempt.remote_id: return f"https://youtu.be/{attempt.remote_id}"
        return ""


class BackgroundPublishingAgent:
    def __init__(self, manager: PublishingManager, interval: float = 30.0) -> None:
        self.manager = manager; self.interval = interval; self._stop = threading.Event(); self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._thread = threading.Thread(target=self.run, name="CreatorAssistantPublishingAgent", daemon=True); self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self, now: datetime | None = None) -> list[str]:
        moment = now or datetime.now(timezone.utc); executed: list[str] = []
        for attempt in self.manager.store.attempts():
            if attempt.status == PublishingAttemptStatus.PROCESSING.value and attempt.remote_id:
                try: self.manager.refresh_status(attempt.attempt_id)
                except Exception: pass
                continue
            if attempt.status not in {PublishingAttemptStatus.PLANNED.value, PublishingAttemptStatus.READY.value, PublishingAttemptStatus.UPLOADING.value}: continue
            if attempt.scheduled_at:
                try:
                    due = datetime.fromisoformat(attempt.scheduled_at).astimezone(timezone.utc)
                except ValueError:
                    continue
                if due > moment: continue
            self.manager.execute(attempt.attempt_id); executed.append(attempt.attempt_id)
        return executed

    def run(self) -> None:
        while not self._stop.is_set():
            self.run_once(); self._stop.wait(self.interval)
