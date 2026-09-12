"""Database engine/session management.

Synchronous SQLAlchemy 2.0 engine + sessionmaker, shared by FastAPI routes
(via dependency), background jobs, and tests. Synchronous is deliberate: v1
volume is low, and a single sync engine keeps transactions and locking easy to
reason about. Async is a drop-in refactor later (`create_async_engine`).

Convention: all datetimes stored in the DB are **naive UTC** (SQLite drops
tzinfo, so mixing aware/naive would corrupt comparisons — see DECISIONS.md).
"""
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    """Declarative base; all models register here."""


@lru_cache
def get_engine(database_url: str | None = None) -> Engine:
    """Build (and memoize) the engine for a given URL."""
    from app.config import get_settings

    url = database_url or get_settings().database_url
    if url.startswith("sqlite"):
        kwargs: dict = {
            "future": True,
            "connect_args": {"check_same_thread": False},
        }
        if url == "sqlite://":  # pure in-memory: share a single connection
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, future=True, pool_pre_ping=True)


@lru_cache
def get_sessionmaker(database_url: str | None = None) -> sessionmaker:
    return sessionmaker(bind=get_engine(database_url), expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
