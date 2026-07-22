from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import load_settings


class Base(DeclarativeBase):
    pass


settings = load_settings()
engine_options = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    # TestClient serves requests from worker threads. A shared in-memory
    # connection keeps the schema and rows visible to every request.
    engine_options.update(
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
engine = create_engine(settings.database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
