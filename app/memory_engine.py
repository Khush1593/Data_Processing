"""Minimal ``agent_memory`` writer (host pipeline compatibility shim).

The spec's confirm endpoint calls
``write_memory(db, user_id, project_id, domain, topic, content)`` to lock the
confirmed cleaning SQL permanently. This implementation upserts on
(project_id, domain, topic) so re-confirming the same table is idempotent.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AgentMemory


def write_memory(
    db: Session,
    project_id: str,
    domain: str,
    topic: str,
    content: str,
    user_id: str | None = None,
) -> AgentMemory:
    existing = (
        db.query(AgentMemory)
        .filter_by(project_id=project_id, domain=domain, topic=topic)
        .first()
    )
    if existing:
        existing.content = content
        existing.user_id = user_id
        db.commit()
        db.refresh(existing)
        return existing

    record = AgentMemory(
        user_id=user_id, project_id=project_id, domain=domain, topic=topic, content=content
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def read_memory(db: Session, project_id: str, domain: str, topic: str) -> str | None:
    record = (
        db.query(AgentMemory)
        .filter_by(project_id=project_id, domain=domain, topic=topic)
        .first()
    )
    return record.content if record else None
