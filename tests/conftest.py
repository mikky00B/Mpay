"""Pytest fixtures: fresh temp-file DB per test, isolated settings.

No module reloads: app.db memoizes engines per URL, so pointing DATABASE_URL
at a unique temp file gives complete isolation while all models/metadata stay
loaded exactly once.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    """Unique temp SQLite DB + cleared settings cache."""
    import app.config
    from app.db import Base, get_engine

    url = f"sqlite:///{tmp_path / 'mpay.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("RECEIVING_ADDRESS", "0xhot000000000000000000000000000000000009")
    monkeypatch.setenv("USDC_CONTRACT", "0xusdc000000000000000000000000000000000000")
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("RPC_URL", "")
    # Pin every setting a test asserts on: pydantic-settings also reads the
    # developer's .env (e.g. CONFIRMATION_THRESHOLD=3), which would silently
    # change behavior mid-suite. Tests must be deterministic regardless.
    monkeypatch.setenv("CONFIRMATION_THRESHOLD", "12")
    monkeypatch.setenv("INVOICE_TTL_MINUTES", "60")

    app.config.get_settings.cache_clear()
    # Engine/sessionmaker memoize under key None — stale engines from a
    # previous test would silently point at the wrong DB otherwise.
    from app import db as app_db

    app_db.get_engine.cache_clear()
    app_db.get_sessionmaker.cache_clear()
    # Import models BEFORE create_all: metadata must be loaded when tables are
    # created. This used to work only by accident — other test modules happened
    # to import app.models transitively at collection time, so running
    # test_api.py alone created an empty DB ("no such table: merchants").
    import app.models  # noqa: F401  (loads Base.metadata)

    Base.metadata.create_all(get_engine(url))
    yield url
    Base.metadata.drop_all(get_engine(url))
    app.config.get_settings.cache_clear()
    app_db.get_engine.cache_clear()
    app_db.get_sessionmaker.cache_clear()


@pytest.fixture()
def client(db_url):
    """TestClient over the routes WITHOUT background loops (lifespan off)."""
    from app.routes import router

    # docs_url="/api-docs" mirrors app.main.create_app: /docs is the human
    # documentation site, not the default Swagger mount.
    app = FastAPI(docs_url="/api-docs")
    app.include_router(router)
    with TestClient(app) as c:
        yield c
