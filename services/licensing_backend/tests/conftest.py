from __future__ import annotations

import base64
import hashlib
import os
import secrets

os.environ["LICENSE_ENV"] = "test"
os.environ["LICENSE_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["LICENSE_ACTIVATION_PEPPER"] = "test-pepper-that-is-long-and-never-production"
os.environ["LICENSE_SIGNING_KEY_ID"] = "test-key"
os.environ["LICENSE_SIGNING_PRIVATE_KEY"] = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
os.environ["LICENSE_ADMIN_TOKEN_HASH"] = hashlib.sha256(b"test-admin-token").hexdigest()
os.environ["LICENSE_PAYMENTS_ENABLED"] = "true"
os.environ["LICENSE_FREE_ACCESS_ADMIN_TELEGRAM_ID"] = "424403653"
os.environ["CREATOR_RELEASE_VERSION"] = "0.3.1-beta.4"
os.environ["CREATOR_RELEASE_COMMIT"] = "test-release-commit"

import pytest
from fastapi.testclient import TestClient

from app.api import app, manager
from app.db import Base, SessionLocal, engine
from app.models import User


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    with SessionLocal() as db:
        manager.bootstrap(db); db.commit()
    yield


@pytest.fixture
def client(): return TestClient(app)


@pytest.fixture
def admin_headers(): return {"X-Admin-Token": "test-admin-token"}
