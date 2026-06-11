"""Control-plane DB session management.

The control DB (PostgreSQL) holds Stage 0 bookkeeping tables. Source client
databases are connected separately and per-request inside the preprocessing
modules — never through this engine.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        uri = get_settings().CONTROL_DB_URI
        # pool_pre_ping guards against stale connections in long-running workers.
        _engine = create_engine(uri, pool_pre_ping=True, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _SessionLocal


def init_db() -> None:
    """Create all control-plane tables if they do not exist.

    For production migrations use db/migrations/*.sql; this is a convenience
    for tests and first-run bootstrap.
    """
    Base.metadata.create_all(bind=get_engine())


def get_db() -> Iterator[Session]:
    """FastAPI-style dependency: yields a session and always closes it."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for non-request code (e.g. the cold-start worker)."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop cached engine/session factory (used by tests that switch DBs)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
