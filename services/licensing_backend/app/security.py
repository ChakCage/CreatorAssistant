from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def activation_code() -> str:
    chunks = ["".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3)]
    return "CA-" + "-".join(chunks)


def normalize_code(value: str) -> str:
    return "".join(ch for ch in value.upper().strip() if ch.isalnum())


def secret_hash(value: str, pepper: str) -> str:
    return hmac.new(pepper.encode("utf-8"), normalize_code(value).encode("ascii"), hashlib.sha256).hexdigest()


def opaque_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def keyed_hash(value: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def mint_service_token(identity: str, permissions: list[str], product_scope: str, secret: str, ttl_seconds: int = 300) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {"identity": identity, "permissions": permissions, "product_scope": product_scope,
               "iat": now, "exp": now + min(max(ttl_seconds, 30), 600), "nonce": secrets.token_urlsafe(12)}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"bs1.{b64(raw)}.{b64(signature)}"


def verify_service_token(token: str, secret: str, permission: str, product_scope: str = "creator_assistant") -> dict[str, Any]:
    try:
        prefix, payload_text, signature_text = token.split(".", 2)
        raw = unb64(payload_text)
        expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
        if prefix != "bs1" or not hmac.compare_digest(expected, unb64(signature_text)):
            raise ValueError("signature")
        payload = json.loads(raw)
        now = int(datetime.now(timezone.utc).timestamp())
        if payload.get("iat", 0) > now + 30 or payload.get("exp", 0) < now:
            raise ValueError("expired")
        if payload.get("product_scope") != product_scope or permission not in payload.get("permissions", []):
            raise ValueError("permission")
        return payload
    except Exception as exc:
        raise ValueError("INVALID_SERVICE_CREDENTIAL") from exc


class EntitlementSigner:
    def __init__(self, key_id: str, private_seed: bytes) -> None:
        self.key_id = key_id
        self.private = Ed25519PrivateKey.from_private_bytes(private_seed)

    def public_key_b64(self) -> str:
        raw = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return b64(raw)

    def sign(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"ca1.{self.key_id}.{b64(raw)}.{b64(self.private.sign(raw))}"


def verify_token(token: str, public_keys: dict[str, str]) -> dict[str, Any]:
    try:
        prefix, key_id, payload_text, signature_text = token.split(".", 3)
        if prefix != "ca1" or key_id not in public_keys: raise ValueError("unknown key")
        raw = unb64(payload_text)
        Ed25519PublicKey.from_public_bytes(unb64(public_keys[key_id])).verify(unb64(signature_text), raw)
        payload = json.loads(raw)
    except Exception as exc:
        raise ValueError("INVALID_ENTITLEMENT_SIGNATURE") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("UNSUPPORTED_ENTITLEMENT_SCHEMA")
    return payload
