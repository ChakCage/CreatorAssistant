from __future__ import annotations

import base64
import json
from datetime import timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import Release


def release_manifest(row: Release, *, include_signature: bool = True) -> dict:
    value = {
        "schema_version": int(row.manifest_schema_version),
        "edition": row.edition,
        "channel": row.channel,
        "version": row.version,
        "build_number": int(row.build_number),
        "minimum_supported_version": row.minimum_supported_version,
        "architecture": row.architecture,
        "download_url": row.download_url,
        "sha256": row.sha256,
        "file_size": int(row.file_size),
        "release_notes": row.release_notes,
        "published_at": row.published_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mandatory": bool(row.mandatory),
        "key_id": row.key_id,
    }
    if include_signature:
        value["signature"] = row.signature
    return value


def canonical_payload(row: Release) -> bytes:
    return json.dumps(
        release_manifest(row, include_signature=False),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sign_release(row: Release, private_key_b64: str, key_id: str) -> str:
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64.strip()))
    row.key_id = key_id
    row.signature = base64.b64encode(private.sign(canonical_payload(row))).decode("ascii")
    return row.signature
