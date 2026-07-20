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
from creator_assistant.services.automation.schedule import SchedulePlanner


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

    def create_attempt(self, short_id: str, platform: str, account_id: str, local_file: str, *, mode: str = PublishingMode.DRY_RUN.value, scheduled_at: str = "", metadata: dict[str, Any] | None = None, upload_strategy: str = "REMOTE_SCHEDULE") -> PublishingAttempt:
        payload = {"short": short_id, "platform": platform, "account": account_id, "file": str(Path(local_file).resolve()), "scheduled": scheduled_at, "upload_strategy": upload_strategy, "metadata": metadata or {}}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        existing = next((item for item in self.store.attempts() if item.idempotency_key == key and item.status != PublishingAttemptStatus.CANCELLED.value), None)
        if existing: return existing
        attempt = PublishingAttempt(
            attempt_id=f"publish-{uuid.uuid4().hex[:14]}", short_id=short_id, platform=platform,
            account_id=account_id, local_file=local_file, mode=mode, scheduled_at=scheduled_at,
            upload_strategy=upload_strategy,
            status=(PublishingAttemptStatus.UPLOAD_QUEUED.value if upload_strategy == "REMOTE_SCHEDULE" else PublishingAttemptStatus.PLANNED.value),
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
            attempt.status = PublishingAttemptStatus.SCHEDULED_REMOTE.value if attempt.scheduled_at and attempt.upload_strategy == "REMOTE_SCHEDULE" else PublishingAttemptStatus.READY.value
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
                metadata["privacyStatus"] = "private"
                metadata["private_only"] = True
                attempt.metadata["capability_notice"] = (
                    "API-проект пока допускает только приватные загрузки. Для публичного расписания потребуется проверка/audit YouTube API"
                )
            has_private_test = any(r.account_id == account.account_id and r.status == PublishingMode.PRIVATE_TEST.value for r in self.store.receipts())
            if not has_private_test and not metadata.get("private_only"):
                attempt.status = PublishingAttemptStatus.NEEDS_REVIEW.value; attempt.error = "A successful private test is required before real publishing"; self._save(attempt); return attempt
        if attempt.platform == "youtube" and attempt.upload_strategy == "REMOTE_SCHEDULE":
            metadata["privacyStatus"] = "private"
        connector = self.connector_factory(attempt.platform, attempt.account_id)
        attempt.status = PublishingAttemptStatus.UPLOADING.value; attempt.total_bytes = path.stat().st_size; attempt.error = ""; self._save(attempt)
        try:
            remote_id = connector.upload(str(path), metadata, progress=lambda done, total: self._progress(attempt, done, total), cancelled=lambda: attempt.attempt_id in self._cancelled)
            attempt.remote_id = remote_id; attempt.progress = 100
            if attempt.platform == "youtube" and attempt.scheduled_at and attempt.upload_strategy == "REMOTE_SCHEDULE" and not metadata.get("private_only"):
                connector.schedule(remote_id, self._youtube_timestamp(attempt.scheduled_at))
                attempt.metadata["remote_schedule_applied"] = True
                attempt.status = PublishingAttemptStatus.REMOTE_PROCESSING.value
            else:
                attempt.status = PublishingAttemptStatus.REMOTE_PROCESSING.value
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
        attempt.processing_status = str(
            (value.get("processingDetails") or {}).get("processingStatus")
            or (value.get("status") or {}).get("uploadStatus")
            or ""
        )
        status = attempt.processing_status.casefold()
        remote_privacy = str((value.get("status") or {}).get("privacyStatus") or "").casefold()
        if status in {"complete", "succeeded", "processed", "published", "publish_complete"}:
            if attempt.metadata.get("private_only"):
                attempt.status = PublishingAttemptStatus.UPLOADED_PRIVATE.value
            elif attempt.metadata.get("remote_schedule_applied"):
                attempt.status = PublishingAttemptStatus.SCHEDULED_REMOTE.value
            elif remote_privacy == "public":
                attempt.status = PublishingAttemptStatus.PUBLISHED.value
            else:
                attempt.status = PublishingAttemptStatus.UPLOADED_PRIVATE.value
        elif status in {"failed", "rejected", "terminated"}:
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

    def rebuild_planned_schedule(self, settings: dict[str, Any]) -> list[PublishingAttempt]:
        planned = [item for item in self.store.attempts() if item.status == PublishingAttemptStatus.PLANNED.value]
        if not planned:
            return []
        plan = SchedulePlanner().build([item.short_id for item in planned], settings, [planned[0].platform])
        for attempt, slot in zip(planned, plan.slots):
            attempt.scheduled_at = slot.scheduled_at
            attempt.updated_at = utc_now()
        self.store.save_attempts_atomic(planned)
        return planned

    def _progress(self, attempt: PublishingAttempt, done: int, total: int) -> None:
        attempt.uploaded_bytes = done; attempt.total_bytes = total; attempt.progress = done * 100 / max(1, total); self._save(attempt)

    def _save(self, attempt: PublishingAttempt) -> None:
        attempt.updated_at = utc_now(); self.store.save_attempt(attempt)

    @staticmethod
    def _youtube_timestamp(value: str) -> str:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError("Время публикации должно содержать часовой пояс")
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def remote_url(attempt: PublishingAttempt) -> str:
        if attempt.platform == "youtube" and attempt.remote_id: return f"https://youtu.be/{attempt.remote_id}"
        return ""


class BackgroundPublishingAgent:
    def __init__(self, manager: PublishingManager, interval: float = 30.0) -> None:
        self.manager = manager; self.interval = interval; self._stop = threading.Event(); self._wake = threading.Event(); self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._thread = threading.Thread(target=self.run, name="CreatorAssistantPublishingAgent", daemon=True); self._thread.start()

    def stop(self) -> None:
        self._stop.set(); self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def run_once(self, now: datetime | None = None) -> list[str]:
        moment = now or datetime.now(timezone.utc); executed: list[str] = []
        for attempt in self.manager.store.attempts():
            if attempt.status in {PublishingAttemptStatus.PROCESSING.value, PublishingAttemptStatus.REMOTE_PROCESSING.value} and attempt.remote_id:
                try: self.manager.refresh_status(attempt.attempt_id)
                except Exception: pass
                continue
            if attempt.status not in {PublishingAttemptStatus.PLANNED.value, PublishingAttemptStatus.READY.value, PublishingAttemptStatus.RENDERED.value, PublishingAttemptStatus.UPLOAD_QUEUED.value, PublishingAttemptStatus.UPLOADING.value}: continue
            if attempt.upload_strategy == "LOCAL_AT_TIME" and attempt.scheduled_at:
                try:
                    due = datetime.fromisoformat(attempt.scheduled_at).astimezone(timezone.utc)
                except ValueError:
                    continue
                if due > moment: continue
            self.manager.execute(attempt.attempt_id); executed.append(attempt.attempt_id)
        return executed

    def run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._wake.wait(self.interval)
            self._wake.clear()
