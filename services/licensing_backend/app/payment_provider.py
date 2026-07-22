from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CheckoutResult:
    provider_payment_id: str
    checkout_url: str


@dataclass(frozen=True)
class WebhookEvent:
    provider_event_id: str
    event_type: str
    payment_id: str
    provider_payment_id: str
    amount_minor: int
    currency: str
    occurred_at: int
    nonce: str


class PaymentProvider(ABC):
    name: str

    @abstractmethod
    def create_checkout(self, *, payment_id: str, checkout_token: str, amount_minor: int,
                        currency: str, description: str, return_url: str = "") -> CheckoutResult: ...

    @abstractmethod
    def get_payment_status(self, provider_payment_id: str) -> str: ...

    @abstractmethod
    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool: ...

    @abstractmethod
    def parse_webhook_event(self, raw_body: bytes) -> WebhookEvent: ...

    @abstractmethod
    def cancel_payment(self, provider_payment_id: str) -> None: ...

    @abstractmethod
    def refund_payment(self, provider_payment_id: str) -> None: ...

    @abstractmethod
    def healthcheck(self) -> bool: ...


class FakePaymentProvider(PaymentProvider):
    name = "fake"

    def __init__(self, secret: str, public_base_url: str) -> None:
        self.secret = secret
        self.public_base_url = public_base_url.rstrip("/")

    def create_checkout(self, *, payment_id: str, checkout_token: str, amount_minor: int,
                        currency: str, description: str, return_url: str = "") -> CheckoutResult:
        del amount_minor, currency, description, return_url
        return CheckoutResult(
            provider_payment_id="fake_" + secrets.token_urlsafe(18),
            checkout_url=f"{self.public_base_url}/v1/billing/fake/checkout/{checkout_token}",
        )

    def get_payment_status(self, provider_payment_id: str) -> str:
        del provider_payment_id
        return "PENDING"

    def signature(self, raw_body: bytes, timestamp: str) -> str:
        return hmac.new(self.secret.encode("utf-8"), timestamp.encode("ascii") + b"." + raw_body, hashlib.sha256).hexdigest()

    def signed_event(self, *, payment_id: str, provider_payment_id: str, event_type: str,
                     amount_minor: int, currency: str, event_id: str | None = None) -> tuple[bytes, dict[str, str]]:
        timestamp = str(int(time.time()))
        payload = {
            "event_id": event_id or "evt_" + secrets.token_urlsafe(16), "event_type": event_type,
            "payment_id": payment_id, "provider_payment_id": provider_payment_id,
            "amount_minor": amount_minor, "currency": currency,
            "timestamp": int(timestamp), "nonce": secrets.token_urlsafe(12),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return raw, {"x-payment-timestamp": timestamp, "x-payment-signature": self.signature(raw, timestamp)}

    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        timestamp = headers.get("x-payment-timestamp", "")
        signature = headers.get("x-payment-signature", "")
        try:
            if abs(int(time.time()) - int(timestamp)) > 300:
                return False
        except ValueError:
            return False
        return hmac.compare_digest(self.signature(raw_body, timestamp), signature)

    def parse_webhook_event(self, raw_body: bytes) -> WebhookEvent:
        value: dict[str, Any] = json.loads(raw_body)
        return WebhookEvent(
            provider_event_id=str(value["event_id"]), event_type=str(value["event_type"]),
            payment_id=str(value["payment_id"]), provider_payment_id=str(value["provider_payment_id"]),
            amount_minor=int(value["amount_minor"]), currency=str(value["currency"]).upper(),
            occurred_at=int(value["timestamp"]), nonce=str(value["nonce"]),
        )

    def cancel_payment(self, provider_payment_id: str) -> None:
        del provider_payment_id

    def refund_payment(self, provider_payment_id: str) -> None:
        del provider_payment_id

    def healthcheck(self) -> bool:
        return True


class ProductionPaymentProviderAdapter(PaymentProvider):
    """Contract placeholder. A real provider is added without changing billing orchestration."""
    name = "production-placeholder"

    def _missing(self):
        raise RuntimeError("Production payment provider is not configured")

    def create_checkout(self, **kwargs): del kwargs; return self._missing()
    def get_payment_status(self, provider_payment_id): del provider_payment_id; return self._missing()
    def verify_webhook(self, raw_body, headers): del raw_body, headers; return False
    def parse_webhook_event(self, raw_body): del raw_body; return self._missing()
    def cancel_payment(self, provider_payment_id): del provider_payment_id; return self._missing()
    def refund_payment(self, provider_payment_id): del provider_payment_id; return self._missing()
    def healthcheck(self) -> bool: return False
