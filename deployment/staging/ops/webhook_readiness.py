#!/usr/bin/env python3
"""Operational readiness check for the staging Telegram webhook.

Normal repair uses setWebhook with drop_pending_updates=false, so Telegram
retains queued updates. A stale historical Telegram error is cleared with a
brief controlled reset only after endpoint and runtime-secret verification.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class WebhookReadinessError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebhookSnapshot:
    url: str
    pending_update_count: int
    last_error_date: int | None = None
    last_error_message: str = ""


class TelegramClient(Protocol):
    def get_webhook_info(self) -> WebhookSnapshot: ...

    def set_webhook(self, expected_url: str, secret: str) -> None: ...

    def delete_webhook_preserving_updates(self) -> None: ...


class TelegramHTTPClient:
    def __init__(self, token: str, timeout: float = 12.0):
        self.api = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def _json(self, method: str, data: dict[str, str] | None = None) -> dict:
        encoded = urllib.parse.urlencode(data).encode() if data is not None else None
        request = urllib.request.Request(f"{self.api}/{method}", data=encoded)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise WebhookReadinessError(f"Telegram {method} request failed: {exc}") from exc
        if not payload.get("ok"):
            raise WebhookReadinessError(f"Telegram {method} returned ok=false")
        return payload.get("result") or {}

    def get_webhook_info(self) -> WebhookSnapshot:
        result = self._json("getWebhookInfo")
        return WebhookSnapshot(
            url=str(result.get("url") or ""),
            pending_update_count=int(result.get("pending_update_count") or 0),
            last_error_date=result.get("last_error_date"),
            last_error_message=str(result.get("last_error_message") or ""),
        )

    def set_webhook(self, expected_url: str, secret: str) -> None:
        self._json(
            "setWebhook",
            {
                "url": expected_url,
                "secret_token": secret,
                "allowed_updates": json.dumps(["message", "callback_query"]),
                "drop_pending_updates": "false",
            },
        )

    def delete_webhook_preserving_updates(self) -> None:
        self._json("deleteWebhook", {"drop_pending_updates": "false"})


def retry_set_webhook(
    client: TelegramClient,
    expected_url: str,
    secret: str,
    *,
    attempts: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            client.set_webhook(expected_url, secret)
            return
        except Exception as exc:  # operational retry boundary
            last_error = exc
            if attempt < attempts:
                sleep(min(2 ** (attempt - 1), 8))
    raise WebhookReadinessError(
        f"setWebhook failed after {attempts} attempts: {last_error}"
    ) from last_error


def ensure_webhook_ready(
    client: TelegramClient,
    *,
    expected_url: str,
    secret: str,
    endpoint_probe: Callable[[str, str], None],
    repair: bool,
    force_set: bool = False,
    clear_stale_error: bool = False,
    require_empty: bool = False,
    sample_delay: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    before = client.get_webhook_info()
    needs_repair = (
        force_set
        or before.url != expected_url
        or bool(before.last_error_message)
    )
    if needs_repair:
        if not repair:
            raise WebhookReadinessError(
                "webhook is not operational and repair is disabled: "
                f"url_match={before.url == expected_url}, "
                f"last_error={bool(before.last_error_message)}"
            )
        retry_set_webhook(client, expected_url, secret, sleep=sleep)

    # This proves both the public endpoint and the secret expected by the
    # running bot.  Post-deploy force_set additionally writes that exact
    # secret to Telegram before this probe.
    endpoint_probe(expected_url, secret)
    first = client.get_webhook_info()
    if first.url != expected_url:
        raise WebhookReadinessError("Telegram webhook URL does not match staging")
    controlled_reset = False
    if first.last_error_message:
        if not (repair and clear_stale_error):
            raise WebhookReadinessError(
                f"Telegram reports a webhook delivery error: {first.last_error_message}"
            )
        # Telegram retains historical last_error_message after an idempotent
        # setWebhook. Only after the new endpoint and runtime secret have been
        # proven do we briefly reset registration, preserving every update.
        # A successful setWebhook above proves the Telegram API accepts the
        # desired registration before the short reset begins.
        client.delete_webhook_preserving_updates()
        retry_set_webhook(client, expected_url, secret, attempts=12, sleep=sleep)
        controlled_reset = True
        first = client.get_webhook_info()
        if first.url != expected_url or first.last_error_message:
            raise WebhookReadinessError(
                "Telegram webhook error could not be cleared by controlled reset"
            )

    sleep(sample_delay)
    final = client.get_webhook_info()
    if final.url != expected_url:
        raise WebhookReadinessError("Telegram webhook URL changed during readiness check")
    if final.last_error_message:
        raise WebhookReadinessError(
            f"Telegram reports a webhook delivery error: {final.last_error_message}"
        )
    if final.pending_update_count > first.pending_update_count:
        raise WebhookReadinessError(
            "Telegram pending_update_count is growing: "
            f"{first.pending_update_count} -> {final.pending_update_count}"
        )
    if require_empty and final.pending_update_count != 0:
        raise WebhookReadinessError(
            f"Telegram queue is not empty: {final.pending_update_count}"
        )
    return {
        "ok": True,
        "url": final.url,
        "pending_before": before.pending_update_count,
        "pending_first": first.pending_update_count,
        "pending_final": final.pending_update_count,
        "repaired": needs_repair,
        "forced_set": force_set,
        "controlled_reset": controlled_reset,
        "last_error": False,
        "secret_probe": True,
    }


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def probe_endpoint(expected_url: str, secret: str, timeout: float = 12.0) -> None:
    parsed = urllib.parse.urlsplit(expected_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    try:
        with urllib.request.urlopen(f"{base}/ready", timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise WebhookReadinessError(f"bot /ready returned HTTP {response.status}")
        synthetic = json.dumps(
            {"update_id": int(time.time() * 1000) & 0x7FFFFFFF}
        ).encode()
        request = urllib.request.Request(
            expected_url,
            data=synthetic,
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": secret,
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise WebhookReadinessError(
                    f"runtime secret probe returned HTTP {response.status}"
                )
    except (OSError, urllib.error.URLError) as exc:
        raise WebhookReadinessError(f"bot endpoint probe failed: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        default="/opt/creator-assistant-staging/shared/.env.staging",
    )
    parser.add_argument("--phase", choices=("pre", "post", "verify"), required=True)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--force-set", action="store_true")
    parser.add_argument("--clear-stale-error", action="store_true")
    parser.add_argument("--require-empty", action="store_true")
    parser.add_argument("--sample-delay", type=float, default=3.0)
    args = parser.parse_args(argv)
    env = {**load_env(Path(args.env_file)), **os.environ}
    token = env.get("CREATOR_BOT_TOKEN", "")
    secret = env.get("CREATOR_BOT_WEBHOOK_SECRET", "")
    domain = env.get("STAGING_BOT_DOMAIN", "")
    if not token or len(secret) < 24 or not domain:
        raise WebhookReadinessError("required Telegram runtime configuration is missing")
    expected_url = f"https://{domain}/telegram/webhook"
    report = ensure_webhook_ready(
        TelegramHTTPClient(token),
        expected_url=expected_url,
        secret=secret,
        endpoint_probe=probe_endpoint,
        repair=args.repair,
        force_set=args.force_set,
        clear_stale_error=args.clear_stale_error,
        require_empty=args.require_empty,
        sample_delay=args.sample_delay,
    )
    report["phase"] = args.phase
    # The report intentionally contains no token or secret.
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WebhookReadinessError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
