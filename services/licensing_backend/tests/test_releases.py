from __future__ import annotations

import base64
from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.db import SessionLocal
from app.models import Release, utcnow
from app.release_signing import sign_release
from app.api import settings
from app.security import mint_service_token


def private_key() -> str:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return base64.b64encode(raw).decode("ascii")


def add_release(edition="commercial", channel="beta", architecture="x86_64", active=True, version="0.3.1"):
    with SessionLocal() as db:
        row = Release(
            edition=edition, channel=channel, architecture=architecture,
            version=version, build_number=101, download_url="https://updates.example.test/setup.exe",
            sha256="a" * 64, file_size=123, release_notes="notes",
            minimum_supported_version="0.3.0", mandatory=False, published_at=utcnow(),
            is_active=active,
        )
        db.add(row); db.flush()
        sign_release(row, private_key(), "test-update-key")
        db.commit()
        return row.id


def test_public_latest_release_is_partitioned_by_edition_channel_and_architecture(client):
    add_release()
    ok = client.get("/v1/releases/latest", params={
        "edition": "commercial", "channel": "beta", "current_version": "0.3.0", "architecture": "x86_64",
    })
    assert ok.status_code == 200
    assert ok.json()["version"] == "0.3.1"
    assert ok.json()["signature"]
    for changes in (
        {"edition": "developer"},
        {"channel": "stable"},
        {"architecture": "arm64"},
        {"current_version": "0.3.1"},
    ):
        params = {"edition": "commercial", "channel": "beta", "current_version": "0.3.0", "architecture": "x86_64"}
        params.update(changes)
        assert client.get("/v1/releases/latest", params=params).json() == {}


def test_inactive_release_is_not_served(client):
    add_release(active=False)
    response = client.get("/v1/releases/latest", params={
        "edition": "commercial", "channel": "beta", "current_version": "0.3.0", "architecture": "x86_64",
    })
    assert response.json() == {}


def test_beta_bot_serves_only_signed_commercial_beta_release(client):
    add_release(edition="developer", channel="beta", version="9.9.9")
    add_release(edition="commercial", channel="stable", version="9.9.8")
    add_release(edition="commercial", channel="beta", version="0.3.1")
    token = mint_service_token(
        "test-bot", ["release:read"], "creator_assistant", settings.bot_service_secret,
    )
    response = client.get("/v1/bot/release", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json()["edition"] == "commercial"
    assert response.json()["channel"] == "beta"
    assert response.json()["version"] == "0.3.1"
