"""SQLAlchemy control-plane models (process.md §2b).

These live in the PostgreSQL control DB — NOT in the per-project DuckDB cache.
They track sync status, cold-start progress, and the permanently-locked
cleaning SQL (agent_memory).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    # timezone-aware UTC (avoids the deprecated naive datetime.utcnow()).
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SyncState(Base):
    __tablename__ = "sync_state"

    id = Column(String, primary_key=True, default=_uuid)
    project_id = Column(String, nullable=False)
    table_name = Column(String, nullable=False)
    # 'append_only' | 'upsert' | 'full_resync' | 'delete_aware'
    sync_mode = Column(String, nullable=False)
    last_sync_utc = Column(DateTime(timezone=True), nullable=True)
    last_row_count = Column(Integer, nullable=True)
    # 'completed' | 'in_progress' | 'failed'
    status = Column(String, nullable=False, default="in_progress")
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (UniqueConstraint("project_id", "table_name"),)


class ColdStartProgress(Base):
    __tablename__ = "cold_start_progress"

    id = Column(String, primary_key=True, default=_uuid)
    project_id = Column(String, nullable=False)
    table_name = Column(String, nullable=False)
    last_chunk_id = Column(String, nullable=True)
    chunks_done = Column(Integer, default=0)
    total_chunks = Column(Integer, nullable=True)
    # 'in_progress' | 'completed'
    status = Column(String, nullable=False, default="in_progress")
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (UniqueConstraint("project_id", "table_name"),)


class Project(Base):
    """A user-initiated review session against one source DB."""

    __tablename__ = "projects"

    id = Column(String, primary_key=True, default=_uuid)
    db_uri = Column(String, nullable=False)
    source_schema = Column(String, nullable=True)
    # 'analyzing' | 'ready' | 'approving' | 'completed' | 'failed'
    status = Column(String, nullable=False, default="analyzing")
    error = Column(Text, nullable=True)
    # Stage 0.5 (stage0_v3_spec.md) — JSON list of cross-table consistency
    # groups found (dates/phones/IDs), canonical format + reason per group,
    # and which tables matched vs. were patched.
    cross_table_summary_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class TableAnalysis(Base):
    """Per-table analysis result for a :class:`Project` (Stage 0 review UI)."""

    __tablename__ = "table_analyses"

    id = Column(String, primary_key=True, default=_uuid)
    project_id = Column(String, nullable=False)
    table_name = Column(String, nullable=False)
    metadata_json = Column(Text, nullable=False)
    cleaning_sql = Column(Text, nullable=False)
    explanation = Column(Text, nullable=False)
    columns_transformed_json = Column(Text, nullable=False)
    diff_json = Column(Text, nullable=False)
    script_source = Column(String, nullable=False)
    # JSON list of {column, question, options: [{id, label}], default}
    clarifications_json = Column(Text, nullable=False, default="[]")
    # JSON dict {column: chosen_option_id}
    clarification_answers_json = Column(Text, nullable=False, default="{}")
    # 'analyzed' | 'approved' | 'cold_start_running' | 'cold_start_done' | 'failed'
    status = Column(String, nullable=False, default="analyzed")
    cold_start_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (UniqueConstraint("project_id", "table_name"),)


class AgentMemory(Base):
    """Permanent store for locked artifacts (e.g. the confirmed cleaning SQL).

    Mirrors the existing pipeline's ``agent_memory`` concept used by
    ``memory_engine.write_memory(domain, topic, content)``. Once a cleaning
    script is locked here it is never regenerated.
    """

    __tablename__ = "agent_memory"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, nullable=True)
    project_id = Column(String, nullable=False)
    domain = Column(String, nullable=False)        # e.g. 'Business_Logic'
    topic = Column(String, nullable=False)          # e.g. 'cleaning_script_<table>'
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (UniqueConstraint("project_id", "domain", "topic"),)
