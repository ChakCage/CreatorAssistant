from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deployment" / "staging" / "ops" / "webhook_readiness.py"
SPEC = importlib.util.spec_from_file_location("webhook_readiness", SCRIPT)
assert SPEC and SPEC.loader
webhook_readiness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = webhook_readiness
SPEC.loader.exec_module(webhook_readiness)

WebhookReadinessError = webhook_readiness.WebhookReadinessError
WebhookSnapshot = webhook_readiness.WebhookSnapshot
ensure_webhook_ready = webhook_readiness.ensure_webhook_ready


EXPECTED = "https://bot-staging.example/telegram/webhook"
SECRET = "s" * 32


class FakeTelegram:
    def __init__(self, snapshots, *, set_failures=0):
        self.snapshots = list(snapshots)
        self.current = self.snapshots[-1]
        self.set_failures = set_failures
        self.set_calls = []
        self.delete_calls = 0

    def get_webhook_info(self):
        if self.snapshots:
            self.current = self.snapshots.pop(0)
        return self.current

    def set_webhook(self, expected_url, secret):
        self.set_calls.append((expected_url, secret))
        if self.set_failures:
            self.set_failures -= 1
            raise WebhookReadinessError("temporary Telegram failure")

    def delete_webhook_preserving_updates(self):
        self.delete_calls += 1


def snapshot(url=EXPECTED, pending=0, error=""):
    return WebhookSnapshot(url, pending, 1 if error else None, error)


def run(
    client,
    *,
    repair=True,
    force_set=False,
    clear_stale_error=False,
    require_empty=False,
):
    probes = []
    report = ensure_webhook_ready(
        client,
        expected_url=EXPECTED,
        secret=SECRET,
        endpoint_probe=lambda url, secret: probes.append((url, secret)),
        repair=repair,
        force_set=force_set,
        clear_stale_error=clear_stale_error,
        require_empty=require_empty,
        sample_delay=0,
        sleep=lambda _seconds: None,
    )
    return report, probes


def test_existing_webhook_passes_without_mutation():
    client = FakeTelegram([snapshot(), snapshot(), snapshot()])
    report, probes = run(client, repair=False)
    assert report["pending_final"] == 0
    assert client.set_calls == []
    assert client.delete_calls == 0
    assert probes == [(EXPECTED, SECRET)]


def test_deleted_webhook_is_restored_without_dropping_queue():
    client = FakeTelegram([snapshot("", 3), snapshot(pending=3), snapshot(pending=0)])
    report, _ = run(client, require_empty=True)
    assert report["repaired"] is True
    assert report["pending_before"] == 3
    assert report["pending_final"] == 0
    assert client.set_calls == [(EXPECTED, SECRET)]
    assert client.delete_calls == 0


def test_wrong_webhook_url_is_replaced():
    client = FakeTelegram(
        [snapshot("https://wrong.example/webhook"), snapshot(), snapshot()]
    )
    report, _ = run(client)
    assert report["repaired"] is True
    assert client.set_calls == [(EXPECTED, SECRET)]


def test_temporary_set_webhook_error_is_retried():
    client = FakeTelegram([snapshot(""), snapshot(), snapshot()], set_failures=2)
    run(client)
    assert len(client.set_calls) == 3


def test_stale_error_is_cleared_only_after_endpoint_probe_and_preserves_updates():
    events = []
    client = FakeTelegram(
        [
            snapshot(error="old 502"),
            snapshot(error="old 502"),
            snapshot(),
            snapshot(),
        ]
    )
    original_delete = client.delete_webhook_preserving_updates

    def record_delete():
        events.append("delete-preserve")
        original_delete()

    client.delete_webhook_preserving_updates = record_delete
    report = ensure_webhook_ready(
        client,
        expected_url=EXPECTED,
        secret=SECRET,
        endpoint_probe=lambda _url, _secret: events.append("probe"),
        repair=True,
        clear_stale_error=True,
        sample_delay=0,
        sleep=lambda _seconds: None,
    )
    assert events == ["probe", "delete-preserve"]
    assert client.delete_calls == 1
    assert report["controlled_reset"] is True


def test_healthy_bot_does_not_hide_missing_webhook_when_repair_disabled():
    client = FakeTelegram([snapshot("")])
    with pytest.raises(WebhookReadinessError, match="repair is disabled"):
        run(client, repair=False)


def test_growing_pending_queue_fails_operational_readiness():
    client = FakeTelegram([snapshot(pending=1), snapshot(pending=1), snapshot(pending=2)])
    with pytest.raises(WebhookReadinessError, match="is growing"):
        run(client, repair=False)


def test_queue_recovery_preserves_three_updates_without_duplicate_set_calls():
    # Telegram retains and drains the original queue because repair is one
    # idempotent setWebhook operation; the missing-hook path needs no reset.
    client = FakeTelegram([snapshot("", 3), snapshot(pending=3), snapshot(pending=0)])
    report, _ = run(client, require_empty=True)
    assert (report["pending_before"], report["pending_final"]) == (3, 0)
    assert len(client.set_calls) == 1
    assert client.delete_calls == 0
