"""Pydantic schemas for the Stage 0 pre-processing layer (process.md §3).

These are the wire/contract types that flow between the connector, sampler,
script generator, dry-run, and the (future) API endpoints.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ColumnMetadata(BaseModel):
    name: str
    declared_type: str
    null_pct: float = 0.0
    distinct_count: int = 0
    is_primary_key: bool = False
    has_created_at: bool = False
    has_updated_at: bool = False
    has_deleted_at: bool = False
    sample_values: list[str] = Field(default_factory=list)
    inferred_issue: Optional[str] = None
    currency_symbols: list[str] = Field(default_factory=list)


class TableMetadata(BaseModel):
    table_name: str
    row_count: int
    columns: list[ColumnMetadata]
    detected_sync_mode: str
    primary_key_column: Optional[str] = None
    change_tracking_column: Optional[str] = None
    source_schema: Optional[str] = None


class CleaningScript(BaseModel):
    table_name: str
    duckdb_sql: str
    explanation: str
    columns_transformed: list[str] = Field(default_factory=list)
    # 'llm' | 'deterministic_fallback' | 'llm_locked'
    source: str = "llm"


class DataQualityDiff(BaseModel):
    table_name: str
    row_count_before: int
    row_count_after: int
    column_diffs: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    safe_to_lock: bool = True


class PreprocessingResult(BaseModel):
    project_id: str
    table_name: str
    cleaning_script: CleaningScript
    diff: DataQualityDiff
    status: str
