"""Pydantic schemas for the Stage 0 pre-processing layer (process.md §3).

These are the wire/contract types that flow between the connector, sampler,
script generator, dry-run, and the (future) API endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ColumnMetadata(BaseModel):
    name: str
    declared_type: str
    null_pct: float = 0.0
    distinct_count: int = 0
    # distinct_count as a fraction of the naive-chunk row count it was
    # estimated from (0-1). distinct_count alone is capped at the chunk size
    # (PREPROCESSING_NAIVE_CHUNK_SIZE), so an absolute cardinality threshold
    # can never fire reliably — FREE_TEXT classification uses this ratio
    # instead (see column_classifier._is_free_text).
    distinct_sample_ratio: float = 0.0
    is_primary_key: bool = False
    has_created_at: bool = False
    has_updated_at: bool = False
    has_deleted_at: bool = False
    sample_values: list[str] = Field(default_factory=list)
    inferred_issues: list[str] = Field(default_factory=list)
    currency_symbols: list[str] = Field(default_factory=list)
    date_format: Optional[str] = None
    null_sentinel_pct: float = 0.0
    # Raw ratios (0-1) for each heuristic, computed regardless of whether they
    # cross the hard thresholds in detect_column_issues, so near-threshold
    # columns can still be surfaced to the LLM for judgment calls.
    issue_ratios: dict[str, float] = Field(default_factory=dict)
    # Stage 0.5 (cross-table consistency, stage0_v3_spec.md) — a short,
    # PII-free pattern descriptor for date/phone-like columns, e.g.
    # "native_timestamp", "%d/%m/%Y", "intl_12d", "local_10d". Never derived
    # from or containing actual row values.
    format_signature: Optional[str] = None


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
    # LLM-flagged ambiguities: [{column, question, options: [{id, label}], default}]
    clarification_questions: list[dict[str, Any]] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# v3.0 — Selective Intelligence Architecture (stage0_v3_spec.md)
# ---------------------------------------------------------------------------

class ColumnClass(str, Enum):
    PII = "pii"               # Personal data — excluded from LLM, never cleaned
    IDENTIFIER = "identifier"  # IDs, UUIDs, FKs — excluded from LLM, never cleaned
    FREE_TEXT = "free_text"    # High-cardinality text — excluded from LLM, never cleaned
    STRUCTURAL = "structural"  # JSON/ARRAY/BINARY — excluded from LLM, never cleaned
    OBSERVE = "observe"        # No issues — passthrough in generated SQL
    CLEAN_DET = "clean_det"    # Deterministic rules fully handle cleaning
    CLEAN_AMBIG = "clean_ambig"  # Needs LLM judgment to resolve


@dataclass
class ClassifiedColumn:
    column: ColumnMetadata
    classification: ColumnClass
    reasons: list[str]
    # Subset of column.inferred_issues that are data-changing and relevant
    # to this classification.
    active_issues: list[str]


ColumnSource = Literal[
    "passthrough",           # SKIP/OBSERVE — SELECT col AS col
    "deterministic",         # CLEAN_DET — deterministic expression
    "llm",                   # CLEAN_AMBIG — LLM resolved it
    "llm_fallback_det",      # CLEAN_AMBIG — LLM failed, deterministic stepped in
    # v3.1 Self-Healing Exception Capture sources
    "llm_patch",             # Step 4.7: AI patch passed verification
    "llm_patch_fallback_det",  # Step 4.7: AI patch failed — deterministic TRY_CAST used
]


@dataclass
class ColumnExpression:
    col_name: str                # Input column name (from source table)
    output_names: list[str]      # Output alias(es)
    sql_exprs: list[str]         # One DuckDB expression per output_name
    source: ColumnSource
    issues_handled: list[str] = field(default_factory=list)
    clarification_needed: bool = False
    clarification_question: Optional[str] = None
    clarification_options: list[str] = field(default_factory=list)
