from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from creator_assistant.domain.publishing import (
    ConnectionStatus, PublishingAccount, PublishingAttemptStatus, PublishingMode, PublishingReceipt,
)
from creator_assistant.infrastructure.credential_store import WindowsCredentialStore
from creator_assistant.infrastructure.publishing_store import PublishingStore
from creator_assistant.services.publishing.manager import BackgroundPublishingAgent, PublishingManager
from creator_assistant.services.publishing.oauth import OAuthService
from creator_assistant.services.publishing.templates import PlatformTemplateResolver
from creator_assistant.services.shorts.project_template import ProjectShortsTemplate
from creator_assistant.ui.publishing_accounts import PublishingAccountsPanel


def test_tokens_are_kept_out_of_json_and_multiple_accounts_are_independent(tmp_path):
    credentials = WindowsCredentialStore(allow_test_memory=True)
    store = PublishingStore(tmp_path)
    credentials.write_json("one", {"access_token": "secret-one", "refresh_token": "refresh-one"})
    credentials.write_json("two", {"access_token": "secret-two"})
    store.save_account(PublishingAccount("one", "youtube", "First", status=ConnectionStatus.CONNECTED.value))
    store.save_account(PublishingAccount("two", "youtube", "Second", status=ConnectionStatus.CONNECTED.value))
    raw = store.path.read_text(encoding="utf-8")
    assert "secret-one" not in raw and "refresh-one" not in raw and "secret-two" not in raw
    assert {item.display_name for item in store.accounts()} == {"First", "Second"}
    assert credentials.read_json("one")["access_token"] == "secret-one"


def test_metadata_store_rejects_secret_fields(tmp_path):
    store = PublishingStore(tmp_path)
    account = PublishingAccount("bad", "youtube", defaults={"access_token": "must-not-leak"})
    try:
        store.save_account(account)
    except ValueError as exc:
        assert "Secret field" in str(exc)
    else:
        raise AssertionError("secret was written")


class RefreshHttp:
    def request(self, method, url, **kwargs):
        class Response:
            def json(self): return {"access_token": "new", "expires_in": 3600, "scope": "video.upload"}
        assert method == "POST" and "oauth/token" in url
        assert b"refresh_token=old" in kwargs["body"]
        return Response()


def test_tiktok_refresh_token_is_rotated_only_in_keyring():
    credentials = WindowsCredentialStore(allow_test_memory=True)
    credentials.write_json("cfg", {"client_key": "key", "client_secret": "secret"}, "client")
    credentials.write_json("account", {"refresh_token": "old", "client_config_id": "cfg"})
    updated = OAuthService(credentials, RefreshHttp()).refresh_tiktok("account")
    assert updated["access_token"] == "new"
    assert credentials.read_json("account")["access_token"] == "new"


def test_dry_run_never_creates_connector_or_network_request(tmp_path):
    media = tmp_path / "short.mp4"; media.write_bytes(b"video")
    called = []
    manager = PublishingManager(PublishingStore(tmp_path / "store"), WindowsCredentialStore(allow_test_memory=True), lambda *_: called.append(True))
    attempt = manager.create_attempt("short_001", "youtube", "", str(media), mode=PublishingMode.DRY_RUN.value)
    result = manager.execute(attempt.attempt_id)
    assert result.status == PublishingAttemptStatus.READY.value
    assert result.processing_status == "DRY_RUN: network disabled"
    assert called == []


def test_attempt_idempotency_and_background_agent_recovery(tmp_path):
    media = tmp_path / "short.mp4"; media.write_bytes(b"video")
    manager = PublishingManager(PublishingStore(tmp_path / "store"), WindowsCredentialStore(allow_test_memory=True), lambda *_: None)
    due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    first = manager.create_attempt("short_001", "youtube", "", str(media), scheduled_at=due)
    duplicate = manager.create_attempt("short_001", "youtube", "", str(media), scheduled_at=due)
    assert duplicate.attempt_id == first.attempt_id
    assert BackgroundPublishingAgent(manager).run_once() == [first.attempt_id]
    assert manager.store.attempts()[0].processing_status == "DRY_RUN: network disabled"
    restored = manager.store.attempts()[0]; restored.status = PublishingAttemptStatus.UPLOADING.value; manager.store.save_attempt(restored)
    assert BackgroundPublishingAgent(manager).run_once() == [first.attempt_id]


def test_private_test_cannot_contact_connector_without_explicit_confirmation(tmp_path):
    media = tmp_path / "short.mp4"; media.write_bytes(b"video")
    store = PublishingStore(tmp_path / "store")
    store.save_account(PublishingAccount("account", "youtube", "Channel", status=ConnectionStatus.CONNECTED.value, granted_scopes=["https://www.googleapis.com/auth/youtube.upload"]))
    called = []
    manager = PublishingManager(store, WindowsCredentialStore(allow_test_memory=True), lambda *_: called.append(True))
    attempt = manager.create_attempt("short_001", "youtube", "account", str(media), mode=PublishingMode.PRIVATE_TEST.value)
    result = manager.execute(attempt.attempt_id)
    assert result.status == PublishingAttemptStatus.NEEDS_REVIEW.value
    assert "explicit user confirmation" in result.error
    assert called == []


def test_tiktok_template_disables_promotional_banner_and_can_reuse_identical_render():
    project = ProjectShortsTemplate.from_dict({"schema_version": 1, "layout": {"mode": "blur_background"}, "subtitle": {}, "branding": {"show_channel_card": True, "channel_profile_id": "beppo"}, "render": {}})
    resolver = PlatformTemplateResolver()
    youtube = resolver.resolve(project, "youtube")
    tiktok = resolver.resolve(project, "tiktok")
    assert youtube.branding["show_channel_card"] is True
    assert tiktok.branding["show_channel_card"] is False
    assert resolver.needs_separate_render(youtube, tiktok)
    plain = ProjectShortsTemplate.from_dict({"schema_version": 1, "layout": {}, "subtitle": {}, "branding": {"show_channel_card": False}, "render": {}})
    assert not resolver.needs_separate_render(resolver.resolve(plain, "youtube"), resolver.resolve(plain, "tiktok"))


def test_connected_accounts_ui_lists_multiple_youtube_channels(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = PublishingStore(tmp_path)
    store.save_account(PublishingAccount("one", "youtube", "Channel One", status=ConnectionStatus.CONNECTED.value))
    store.save_account(PublishingAccount("two", "youtube", "Channel Two", status=ConnectionStatus.CONNECTED.value))
    panel = PublishingAccountsPanel(SimpleNamespace(publishing_store=store))
    assert panel.table.rowCount() == 2
    assert {panel.table.item(row, 1).text() for row in range(2)} == {"Channel One", "Channel Two"}
    panel.close()
    assert QApplication.instance() is app


class RemoteScheduleConnector:
    def __init__(self):
        self.scheduled = []

    def upload(self, path, metadata, progress, cancelled):
        progress(5, 5)
        assert metadata["privacyStatus"] == "private"
        return "youtube-id"

    def schedule(self, remote_id, at):
        self.scheduled.append((remote_id, at))

    def get_status(self, remote_id):
        return {"id": remote_id, "processingDetails": {"processingStatus": "complete"}, "status": {"privacyStatus": "private"}}


def test_youtube_remote_schedule_uploads_immediately_and_uses_utc_publish_at(tmp_path):
    media = tmp_path / "short.mp4"; media.write_bytes(b"video")
    store = PublishingStore(tmp_path / "store")
    store.save_account(PublishingAccount("account", "youtube", "Channel", status=ConnectionStatus.CONNECTED.value,
        capability="public", granted_scopes=["https://www.googleapis.com/auth/youtube.upload"]))
    store.save_receipt(PublishingReceipt("r", "a", "youtube", "account", "old", status=PublishingMode.PRIVATE_TEST.value))
    connector = RemoteScheduleConnector()
    manager = PublishingManager(store, WindowsCredentialStore(allow_test_memory=True), lambda *_: connector)
    future = "2026-07-21T00:50:00+03:00"
    attempt = manager.create_attempt("short_001", "youtube", "account", str(media), mode=PublishingMode.REAL.value,
        scheduled_at=future, metadata={"network_approved": True})
    assert attempt.status == PublishingAttemptStatus.UPLOAD_QUEUED.value
    assert BackgroundPublishingAgent(manager).run_once(datetime(2026, 7, 20, tzinfo=timezone.utc)) == [attempt.attempt_id]
    uploaded = manager.store.attempts()[0]
    assert connector.scheduled == [("youtube-id", "2026-07-20T21:50:00Z")]
    assert uploaded.status == PublishingAttemptStatus.REMOTE_PROCESSING.value
    final = manager.refresh_status(attempt.attempt_id)
    assert final.status == PublishingAttemptStatus.SCHEDULED_REMOTE.value


def test_rebuild_schedule_changes_only_planned_attempts(tmp_path):
    media = tmp_path / "short.mp4"; media.write_bytes(b"video")
    manager = PublishingManager(PublishingStore(tmp_path / "store"), WindowsCredentialStore(allow_test_memory=True))
    planned = manager.create_attempt("short_001", "youtube", "", str(media), upload_strategy="LOCAL_AT_TIME")
    completed = manager.create_attempt("short_002", "youtube", "", str(media), upload_strategy="LOCAL_AT_TIME", scheduled_at="2026-07-01T09:00:00+03:00")
    completed.status = PublishingAttemptStatus.PUBLISHED.value; manager.store.save_attempt(completed)
    manager.rebuild_planned_schedule({"start_date": "2026-07-21", "timezone": "Europe/Moscow", "publications_per_day": 1, "preferred_time_slots": ["1:00"]})
    values = {item.short_id: item for item in manager.store.attempts()}
    assert values["short_001"].scheduled_at == "2026-07-21T01:00:00+03:00"
    assert values["short_002"].scheduled_at == "2026-07-01T09:00:00+03:00"
