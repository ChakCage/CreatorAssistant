from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from creator_assistant.infrastructure.release_updates import (
    ReleaseManifest, UpdateError, UpdatePolicy, sign_manifest,
)


def key_pair():
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(private_raw).decode(), base64.b64encode(public_raw).decode()


def payload(**changes):
    value = {
        "schema_version": 1, "edition": "commercial", "channel": "beta",
        "version": "0.3.1", "build_number": 101, "minimum_supported_version": "0.3.0",
        "architecture": "x86_64", "download_url": "https://updates.example.test/setup.exe",
        "sha256": hashlib.sha256(b"installer").hexdigest(), "file_size": 9,
        "release_notes": "test", "published_at": "2026-07-27T10:00:00Z",
        "mandatory": False,
    }
    value.update(changes)
    return value


def policy(public: str, **changes):
    values = {
        "edition": "commercial", "channel": "beta", "architecture": "x86_64",
        "current_version": "0.3.0", "current_build": 100,
        "public_keys": {"beta-2026": public}, "allowed_hosts": ("updates.example.test",),
    }
    values.update(changes)
    return UpdatePolicy(**values)


def test_signed_manifest_is_accepted_and_tampering_is_rejected():
    private, public = key_pair()
    signed = sign_manifest(payload(), private, "beta-2026")
    manifest = ReleaseManifest.from_dict(signed)
    policy(public).validate(manifest)
    tampered = ReleaseManifest.from_dict({**signed, "file_size": 10})
    with pytest.raises(UpdateError, match="Подпись"):
        policy(public).validate(tampered)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"edition": "developer"}, "WRONG_EDITION"),
        ({"channel": "stable"}, "WRONG_CHANNEL"),
        ({"architecture": "arm64"}, "WRONG_ARCHITECTURE"),
        ({"download_url": "http://updates.example.test/setup.exe"}, "INSECURE_URL"),
        ({"version": "0.2.9"}, "DOWNGRADE_BLOCKED"),
    ],
)
def test_update_policy_rejects_cross_profile_insecure_and_downgrade(change, code):
    private, public = key_pair()
    manifest = ReleaseManifest.from_dict(sign_manifest(payload(**change), private, "beta-2026"))
    with pytest.raises(UpdateError) as error:
        policy(public).validate(manifest)
    assert error.value.code == code


def test_unknown_staging_key_is_not_trusted_by_stable_client():
    private, _public = key_pair()
    _, stable_public = key_pair()
    manifest = ReleaseManifest.from_dict(sign_manifest(payload(channel="stable"), private, "beta-2026"))
    with pytest.raises(UpdateError) as error:
        policy(stable_public, channel="stable", public_keys={"stable-2026": stable_public}).validate(manifest)
    assert error.value.code == "UNKNOWN_UPDATE_KEY"
