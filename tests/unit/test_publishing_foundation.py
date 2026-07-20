from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from creator_assistant.domain.publishing import (
    ConnectionStatus, PublishingAccount, PublishingAttemptStatus, PublishingMode,
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
