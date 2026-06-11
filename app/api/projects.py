"""Stage 0 review UI — projects API (process.md addendum).

Endpoints:
  POST   /api/projects                          create + start analysis
  GET    /api/projects/{id}                     status + per-table summaries
  GET    /api/projects/{id}/tables/{table}      full per-table detail
  POST   /api/projects/{id}/approve             lock + cold-start tables
  GET    /api/projects/{id}/tables/{table}/diff paginated changed-rows diff
"""
from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Project, TableAnalysis
from app.preprocessing.cache_engine import get_project_duckdb_path
from app.preprocessing.diff_engine import get_diff_page
from app.preprocessing.models import TableMetadata
from app.preprocessing.orchestrator import analyze_project, approve_and_process

router = APIRouter(prefix="/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    db_uri: str
    schema_name: str | None = None


class ClarificationAnswer(BaseModel):
    option: str
    note: str | None = None


class ApproveRequest(BaseModel):
    tables: list[str] | None = None
    # table_name -> {column_name: {option, note?}}
    clarification_answers: dict[str, dict[str, ClarificationAnswer]] | None = None


@router.post("")
def create_project(
    req: CreateProjectRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    project = Project(db_uri=req.db_uri, source_schema=req.schema_name, status="analyzing")
    db.add(project)
    db.commit()
    db.refresh(project)

    background_tasks.add_task(analyze_project, project.id, req.db_uri, req.schema_name)
    return {"project_id": project.id, "status": project.status}


@router.get("/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    analyses = (
        db.query(TableAnalysis).filter_by(project_id=project_id).order_by(TableAnalysis.table_name).all()
    )

    tables = []
    for a in analyses:
        metadata = json.loads(a.metadata_json) if a.metadata_json != "{}" else {}
        diff = json.loads(a.diff_json) if a.diff_json != "{}" else {}
        issues = [
            {"column": c["name"], "issue": c["inferred_issue"]}
            for c in metadata.get("columns", [])
            if c.get("inferred_issue")
        ]
        tables.append({
            "table_name": a.table_name,
            "row_count": metadata.get("row_count"),
            "sync_mode": metadata.get("detected_sync_mode"),
            "issues": issues,
            "columns_transformed": json.loads(a.columns_transformed_json),
            "script_source": a.script_source,
            "safe_to_lock": diff.get("safe_to_lock"),
            "warnings": diff.get("warnings", []),
            "status": a.status,
            "cold_start_error": a.cold_start_error,
            "clarifications": json.loads(a.clarifications_json or "[]"),
        })

    return {
        "project_id": project.id,
        "db_uri": project.db_uri,
        "schema": project.source_schema,
        "status": project.status,
        "error": project.error,
        "tables": tables,
    }


@router.get("/{project_id}/tables/{table_name}")
def get_table_detail(project_id: str, table_name: str, db: Session = Depends(get_db)):
    a = (
        db.query(TableAnalysis)
        .filter_by(project_id=project_id, table_name=table_name)
        .first()
    )
    if not a:
        raise HTTPException(404, "Table analysis not found")

    return {
        "table_name": a.table_name,
        "metadata": json.loads(a.metadata_json) if a.metadata_json != "{}" else {},
        "cleaning_sql": a.cleaning_sql,
        "explanation": a.explanation,
        "columns_transformed": json.loads(a.columns_transformed_json),
        "diff": json.loads(a.diff_json) if a.diff_json != "{}" else {},
        "script_source": a.script_source,
        "status": a.status,
        "cold_start_error": a.cold_start_error,
    }


@router.post("/{project_id}/approve")
def approve_project(
    project_id: str, req: ApproveRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.status not in ("ready", "completed"):
        raise HTTPException(409, f"Project not ready for approval (status={project.status})")

    clarification_answers = (
        {
            table: {col: ans.model_dump() for col, ans in cols.items()}
            for table, cols in req.clarification_answers.items()
        }
        if req.clarification_answers
        else None
    )
    background_tasks.add_task(
        approve_and_process, project_id, req.tables, clarification_answers
    )
    return {"status": "approving"}


@router.get("/{project_id}/tables/{table_name}/diff")
def get_table_diff(
    project_id: str,
    table_name: str,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    a = (
        db.query(TableAnalysis)
        .filter_by(project_id=project_id, table_name=table_name)
        .first()
    )
    if not a:
        raise HTTPException(404, "Table analysis not found")
    if a.status != "cold_start_done":
        raise HTTPException(409, f"Table not yet processed (status={a.status})")

    metadata = TableMetadata.model_validate_json(a.metadata_json)
    duckdb_path = get_project_duckdb_path(project_id)

    return get_diff_page(duckdb_path, project.db_uri, metadata, table_name, page, page_size)
