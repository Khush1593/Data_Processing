"""Pytest fixtures: control DB lifecycle + source DB URIs (SQLite + Postgres)."""
from __future__ import annotations

import os

import pytest
import sqlalchemy

from app.config import get_settings
from app.db import init_db, reset_engine, session_scope
from app.models import AgentMemory, ColdStartProgress, SyncState

POSTGRES_SOURCE_URI = os.environ.get(
    "TEST_PG_SOURCE_URI",
    "postgresql+psycopg2://clarum:clarum@localhost:5433/clarum_source",
)


def _pg_available(uri: str) -> bool:
    try:
        eng = sqlalchemy.create_engine(uri)
        with eng.connect() as c:
            c.execute(sqlalchemy.text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def _control_db():
    """Ensure control-plane tables exist for the whole test session."""
    init_db()
    yield
    reset_engine()


@pytest.fixture(scope="session", autouse=True)
def _fast_llm_throttle():
    """Tests register fake LLM providers (no real API, no real rate limit) —
    the production RPM-based throttle in app.llm.engine has nothing to
    protect here and would otherwise add ~12s of real sleep per LLM call
    across the suite. Lift it for the test session only."""
    s = get_settings()
    s.LLM_REQUESTS_PER_MINUTE = 6000
    s.LLM_MIN_INTERVAL_SECONDS = 0.0


@pytest.fixture(autouse=True)
def _clean_control_tables():
    """Wipe control tables before each test for isolation."""
    with session_scope() as s:
        s.query(ColdStartProgress).delete()
        s.query(SyncState).delete()
        s.query(AgentMemory).delete()
    yield


@pytest.fixture
def sqlite_uri(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'source.db'}"


@pytest.fixture(params=["sqlite", "postgres"])
def source_uri(request, tmp_path) -> str:
    """Parametrised source URI: runs each test on SQLite and (if up) Postgres."""
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'source.db'}"
    if not _pg_available(POSTGRES_SOURCE_URI):
        pytest.skip("Postgres source not available")
    # Use a unique table per test to avoid cross-test collisions on shared PG.
    return POSTGRES_SOURCE_URI
