# Agent Instructions: Hybrid Data Pre-Processing Layer
## Clarum Insights — MVP Implementation Spec

**Version:** 1.0  
**Target Codebase:** Clarum Insights (FastAPI + DuckDB + Next.js)  
**Scope:** Implement a hybrid metadata + stratified sample data pre-processing layer that runs before Stage 1 of the existing pipeline. This is a new Stage 0 — it does not replace or modify any existing stage.

---

## 0. Context — What Exists, What You Are Adding

### Existing Pipeline (DO NOT MODIFY)
```
Stage 1  → data_engine.py       detect_schema(), normalise_string_dimensions()
Stage 2  → semantic_engine.py   keyword-score heuristics
Stage 3  → understanding_engine.py + llm_engine.py
Stage 3.5→ user blueprint review
Stage 4  → clarification_engine.py
Stage 5  → goal_engine.py
Stage 6  → context_engine.py
Stage 7  → confirmation_engine.py
Stage 8  → dashboard_engine.py
Stage 9  → sql_safety.py + DuckDB execution
Stage 10 → insight_engine.py
Stage 11 → Q&A loop in api.py
```

### What You Are Building — Stage 0
A new pre-processing layer inserted before Stage 1. It:
1. Connects to the client's database and extracts schema metadata
2. Pulls a stratified sample of real rows
3. Sends metadata + sample to the LLM to generate a DuckDB-dialect cleaning SQL script
4. Validates the SQL via AST check
5. Runs a dry-run preview and shows a Data Quality Diff to the user
6. On user confirmation, locks the SQL permanently to `agent_memory`
7. Executes the locked cleaning SQL on full data to produce a clean DuckDB cache
8. Sets up a `sync_state` table to track sync status
9. Passes clean data to Stage 1 exactly as before

### Guiding Principles
- **AI writes cleaning SQL once. It is locked permanently after user confirmation. Never regenerated.**
- **Raw row data never leaves the client's server in Tier 2 mode. Only metadata and sample rows (for script generation) touch our cloud in Tier 1.**
- **The stratified sample is discarded from memory after the cleaning SQL is locked. Never persisted.**
- **All existing pipeline stages remain completely unchanged.**
- **Every new function follows the existing pattern: deterministic fallback first, LLM enriches.**

---

## 1. New Files To Create

```
app/
  preprocessing/
    __init__.py
    connector.py          # DB connection + information_schema extraction
    sampler.py            # stratified sample extraction
    profiler.py           # metadata profiler (wraps existing logic + new)
    script_generator.py   # LLM prompt builder + cleaning SQL generation
    ast_validator.py      # AST safety check for cleaning SQL
    dry_run.py            # execute SQL on sample, compute Data Quality Diff
    cache_engine.py       # cold start execution, sync_state management
    models.py             # Pydantic schemas for this layer

db/
  migrations/
    add_sync_state.sql    # sync_state and cold_start_progress table DDL
    add_cleaning_cache.sql # cleaning_script storage in agent_memory
```

---

## 2. Database Schema Changes

### 2a. sync_state table
Add to your SQLAlchemy models and run migration:

```sql
CREATE TABLE sync_state (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    project_id      TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    sync_mode       TEXT NOT NULL,       -- 'append_only' | 'upsert' | 'full_resync' | 'delete_aware'
    last_sync_utc   TIMESTAMP,
    last_row_count  INTEGER,             -- for reconciliation check
    status          TEXT NOT NULL,       -- 'completed' | 'in_progress' | 'failed'
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, table_name)
);

CREATE TABLE cold_start_progress (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    project_id      TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    last_chunk_id   TEXT,               -- last PK value or timestamp window completed
    chunks_done     INTEGER DEFAULT 0,
    total_chunks    INTEGER,
    status          TEXT NOT NULL,      -- 'in_progress' | 'completed'
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, table_name)
);
```

### 2b. SQLAlchemy Models
Add to `app/models.py` (or wherever your existing models live):

```python
class SyncState(Base):
    __tablename__ = "sync_state"
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    project_id = Column(String, nullable=False)
    table_name = Column(String, nullable=False)
    sync_mode = Column(String, nullable=False)
    last_sync_utc = Column(DateTime, nullable=True)
    last_row_count = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="in_progress")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("project_id", "table_name"),)

class ColdStartProgress(Base):
    __tablename__ = "cold_start_progress"
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    project_id = Column(String, nullable=False)
    table_name = Column(String, nullable=False)
    last_chunk_id = Column(String, nullable=True)
    chunks_done = Column(Integer, default=0)
    total_chunks = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="in_progress")
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("project_id", "table_name"),)
```

---

## 3. Pydantic Schemas — `app/preprocessing/models.py`

```python
from pydantic import BaseModel, Field
from typing import Optional

class ColumnMetadata(BaseModel):
    name: str
    declared_type: str                    # as reported by information_schema
    null_pct: float                       # 0.0 to 1.0
    distinct_count: int
    is_primary_key: bool = False
    has_created_at: bool = False          # True if column name matches created_at pattern
    has_updated_at: bool = False
    has_deleted_at: bool = False
    sample_values: list[str] = Field(default_factory=list)  # from stratified sample
    inferred_issue: Optional[str] = None  # e.g. "currency_string", "mixed_dates", "null_variant"

class TableMetadata(BaseModel):
    table_name: str
    row_count: int
    columns: list[ColumnMetadata]
    detected_sync_mode: str               # 'append_only' | 'upsert' | 'full_resync' | 'delete_aware'
    primary_key_column: Optional[str] = None
    change_tracking_column: Optional[str] = None  # updated_at or created_at column name

class CleaningScript(BaseModel):
    table_name: str
    duckdb_sql: str                       # the full cleaning SELECT statement
    explanation: str                      # plain English summary for user review
    columns_transformed: list[str]        # column names that are transformed
    source: str = "llm"                   # always "llm" for generated scripts

class DataQualityDiff(BaseModel):
    table_name: str
    row_count_before: int
    row_count_after: int
    column_diffs: list[dict]              # [{column, null_before, null_after, type_before, type_after}]
    warnings: list[str]                   # e.g. "null_pct increased by 15% in column revenue"
    safe_to_lock: bool                    # False if warnings are critical

class PreprocessingResult(BaseModel):
    project_id: str
    table_name: str
    cleaning_script: CleaningScript
    diff: DataQualityDiff
    status: str                           # 'pending_review' | 'locked' | 'failed'
```

---

## 4. `app/preprocessing/connector.py`

### Purpose
Connect to client's database, extract `information_schema` metadata, detect sync mode.

### Implementation

```python
import sqlalchemy
from sqlalchemy import text
from app.preprocessing.models import ColumnMetadata, TableMetadata

# Sync mode detection patterns
CREATED_AT_PATTERNS = {"created_at", "createdat", "create_date", "insert_date", "inserted_at"}
UPDATED_AT_PATTERNS = {"updated_at", "updatedat", "update_date", "modified_at", "last_modified"}
DELETED_AT_PATTERNS = {"deleted_at", "deletedat", "delete_date", "is_deleted", "soft_delete"}
PRIMARY_KEY_QUERY = """
    SELECT column_name
    FROM information_schema.key_column_usage k
    JOIN information_schema.table_constraints t
        ON k.constraint_name = t.constraint_name
    WHERE t.constraint_type = 'PRIMARY KEY'
      AND k.table_name = :table_name;
"""

def get_table_metadata(db_uri: str, table_name: str) -> TableMetadata:
    """
    Connect to client DB, extract column metadata from information_schema.
    Never reads row data — structural metadata only.
    """
    engine = sqlalchemy.create_engine(db_uri)
    
    with engine.connect() as conn:
        # Get column info
        columns_result = conn.execute(text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = :table_name
            ORDER BY ordinal_position
        """), {"table_name": table_name}).fetchall()
        
        # Get row count (lightweight — no full scan)
        row_count = conn.execute(
            text(f"SELECT COUNT(*) FROM {table_name}")
        ).scalar()
        
        # Get null percentages per column (one query per column — batch if >20 cols)
        null_pcts = {}
        for col_name, _, _ in columns_result:
            null_pcts[col_name] = conn.execute(text(f"""
                SELECT CAST(SUM(CASE WHEN {col_name} IS NULL THEN 1 ELSE 0 END) AS FLOAT)
                       / NULLIF(COUNT(*), 0)
                FROM {table_name}
            """)).scalar() or 0.0
        
        # Get distinct counts (use APPROX_COUNT_DISTINCT if available for large tables)
        distinct_counts = {}
        for col_name, _, _ in columns_result:
            distinct_counts[col_name] = conn.execute(
                text(f"SELECT COUNT(DISTINCT {col_name}) FROM {table_name}")
            ).scalar() or 0
        
        # Detect primary key
        try:
            pk_result = conn.execute(text(PRIMARY_KEY_QUERY), {"table_name": table_name}).fetchone()
            primary_key_col = pk_result[0] if pk_result else None
        except Exception:
            primary_key_col = None
    
    # Build column metadata objects
    columns = []
    change_tracking_col = None
    has_updated_at = False
    has_deleted_at = False
    
    for col_name, data_type, is_nullable in columns_result:
        col_lower = col_name.lower()
        
        is_updated = col_lower in UPDATED_AT_PATTERNS
        is_deleted = col_lower in DELETED_AT_PATTERNS
        is_created = col_lower in CREATED_AT_PATTERNS
        
        if is_updated:
            has_updated_at = True
            change_tracking_col = col_name
        if is_deleted:
            has_deleted_at = True
        
        columns.append(ColumnMetadata(
            name=col_name,
            declared_type=data_type,
            null_pct=null_pcts.get(col_name, 0.0),
            distinct_count=distinct_counts.get(col_name, 0),
            is_primary_key=(col_name == primary_key_col),
            has_created_at=is_created,
            has_updated_at=is_updated,
            has_deleted_at=is_deleted,
        ))
    
    # Assign sync mode
    if has_deleted_at:
        sync_mode = "delete_aware"
    elif primary_key_col and has_updated_at:
        sync_mode = "upsert"
    elif row_count < 10_000 and not has_updated_at:
        sync_mode = "full_resync"
    else:
        sync_mode = "append_only"
    
    return TableMetadata(
        table_name=table_name,
        row_count=row_count,
        columns=columns,
        detected_sync_mode=sync_mode,
        primary_key_column=primary_key_col,
        change_tracking_column=change_tracking_col,
    )
```

---

## 5. `app/preprocessing/sampler.py`

### Purpose
Pull a stratified sample of real rows. NOT random — deliberately targets edge cases.

### Implementation

```python
import pandas as pd
import sqlalchemy
from sqlalchemy import text
from app.preprocessing.models import TableMetadata

SAMPLE_TARGET_ROWS = 2000

def extract_stratified_sample(
    db_uri: str,
    metadata: TableMetadata,
) -> pd.DataFrame:
    """
    Pull a stratified sample from the client's table.
    Strategy:
      - 200 most recent rows (current format patterns)
      - 800 random rows (general distribution)
      - Up to 20 null rows per column (real null representations)
      - 50 boundary rows per numeric column (min/max outliers)
    
    IMPORTANT: This sample is used only for cleaning script generation.
    It must be discarded after Phase 2 completes. Never persist it.
    """
    engine = sqlalchemy.create_engine(db_uri)
    table = metadata.table_name
    pk = metadata.primary_key_column
    
    frames = []
    
    with engine.connect() as conn:
        # 1. Recent rows — catches current format
        if pk:
            recent = pd.read_sql(
                f"SELECT * FROM {table} ORDER BY {pk} DESC LIMIT 200",
                conn
            )
            frames.append(recent)
        
        # 2. Random rows — general distribution
        # Use TABLESAMPLE if supported (PostgreSQL), otherwise ORDER BY RANDOM()
        try:
            random_rows = pd.read_sql(
                f"SELECT * FROM {table} TABLESAMPLE SYSTEM (1) LIMIT 800",
                conn
            )
        except Exception:
            # Fallback for databases that don't support TABLESAMPLE
            random_rows = pd.read_sql(
                f"SELECT * FROM {table} ORDER BY RANDOM() LIMIT 800",
                conn
            )
        frames.append(random_rows)
        
        # 3. Null-revealing rows — per column
        for col in metadata.columns:
            if col.null_pct > 0:
                null_rows = pd.read_sql(
                    f"SELECT * FROM {table} WHERE {col.name} IS NULL LIMIT 20",
                    conn
                )
                frames.append(null_rows)
        
        # 4. Boundary rows — numeric columns only
        numeric_types = {"integer", "bigint", "numeric", "decimal", "float", "real", "double precision"}
        for col in metadata.columns:
            if col.declared_type.lower() in numeric_types:
                try:
                    boundary = pd.read_sql(f"""
                        (SELECT * FROM {table} ORDER BY {col.name} ASC  LIMIT 50)
                        UNION ALL
                        (SELECT * FROM {table} ORDER BY {col.name} DESC LIMIT 50)
                    """, conn)
                    frames.append(boundary)
                except Exception:
                    pass
    
    if not frames:
        return pd.DataFrame()
    
    # Combine, deduplicate, cap at SAMPLE_TARGET_ROWS
    combined = pd.concat(frames, ignore_index=True)
    if pk and pk in combined.columns:
        combined = combined.drop_duplicates(subset=[pk])
    else:
        combined = combined.drop_duplicates()
    
    return combined.head(SAMPLE_TARGET_ROWS)


def detect_column_issues(sample: pd.DataFrame, metadata_col) -> str | None:
    """
    Inspect sample values for a column and return detected issue type.
    Returns None if column looks clean.
    """
    if sample.empty or metadata_col.name not in sample.columns:
        return None
    
    col_series = sample[metadata_col.name].dropna().astype(str).str.strip()
    if len(col_series) == 0:
        return None
    
    # Check for currency strings
    currency_pattern = col_series.str.contains(r'[$€£₹¥]', regex=True)
    if currency_pattern.mean() > 0.3:
        return "currency_string"
    
    # Check for percentage strings
    pct_pattern = col_series.str.contains(r'%$', regex=True)
    if pct_pattern.mean() > 0.3:
        return "percentage_string"
    
    # Check for non-standard null representations
    null_variants = {"n/a", "na", "null", "none", "-", "–", "—", "", "nan", "#n/a"}
    null_variant_pattern = col_series.str.lower().isin(null_variants)
    if null_variant_pattern.mean() > 0.05:
        return "null_variant"
    
    # Check for mixed date formats (VARCHAR column that looks like dates)
    if metadata_col.declared_type.lower() in {"varchar", "text", "character varying"}:
        date_like = col_series.str.match(
            r'\d{1,4}[-/\.]\d{1,2}[-/\.]\d{1,4}|\w+ \d{1,2},? \d{4}',
            na=False
        )
        if date_like.mean() > 0.5:
            return "mixed_date_format"
    
    # Check for numeric stored as string
    if metadata_col.declared_type.lower() in {"varchar", "text", "character varying"}:
        numeric_like = pd.to_numeric(
            col_series.str.replace(r'[,$€£₹¥%]', '', regex=True),
            errors='coerce'
        ).notna()
        if numeric_like.mean() > 0.85:
            return "numeric_as_string"
    
    return None
```

---

## 6. `app/preprocessing/script_generator.py`

### Purpose
Build the combined metadata + sample prompt. Call LLM. Return cleaning SQL.

### Implementation

```python
import json
import pandas as pd
from app.preprocessing.models import TableMetadata, CleaningScript, ColumnMetadata
from app.llm_engine import _generate_structured  # reuse existing LLM caller
from pydantic import BaseModel

# Pydantic schema for LLM response
class CleaningScriptResponse(BaseModel):
    duckdb_sql: str           # Full DuckDB SELECT statement with all transformations
    explanation: str          # Plain English — what each transformation does and why
    columns_transformed: list[str]

    class Config:
        extra = "ignore"
        str_strip_whitespace = True


def _build_sample_summary(sample: pd.DataFrame, metadata: TableMetadata) -> str:
    """
    Build a compact, per-column sample summary for the LLM prompt.
    Never sends full DataFrame — sends top 10 unique values per column only.
    """
    lines = []
    for col in metadata.columns:
        if col.name not in sample.columns:
            continue
        col_series = sample[col.name].dropna().astype(str).str.strip()
        unique_vals = col_series.unique()[:10].tolist()
        lines.append(
            f"  - {col.name} ({col.declared_type}): "
            f"null_pct={col.null_pct:.1%}, "
            f"distinct={col.distinct_count}, "
            f"issue={col.inferred_issue or 'none'}, "
            f"sample_values={unique_vals}"
        )
    return "\n".join(lines)


def _build_prompt(metadata: TableMetadata, sample: pd.DataFrame) -> str:
    sample_summary = _build_sample_summary(sample, metadata)
    
    return f"""You are a senior data engineer. Your task is to write a DuckDB-dialect SQL SELECT statement that cleans and normalises the table described below.

## Table: {metadata.table_name}
## Row Count: {metadata.row_count:,}

## Column Metadata + Sample Values
{sample_summary}

## Your Task
Write a single DuckDB SELECT statement that:
1. Selects ALL columns (do not drop any columns)
2. Applies cleaning transformations ONLY to columns where an issue is detected
3. For columns with no issue, passes them through unchanged: just `column_name`

## Transformation Rules — Follow These Exactly

### Currency strings (issue: currency_string)
```sql
CASE
    WHEN TRIM(col) IN ('N/A', '-', '', 'null', 'none', 'NA') THEN NULL
    WHEN col REGEXP '^[$€£₹¥][\\d,]+\\.?\\d*$'
        THEN CAST(REGEXP_REPLACE(REGEXP_REPLACE(col, '[$€£₹¥]', ''), ',', '') AS DOUBLE)
    ELSE TRY_CAST(REGEXP_REPLACE(col, '[^\\d.]', '') AS DOUBLE)
END AS col
```

### Percentage strings (issue: percentage_string)
```sql
CASE
    WHEN TRIM(col) IN ('N/A', '-', '', 'null', 'none', 'NA') THEN NULL
    WHEN col LIKE '%\\%'
        THEN TRY_CAST(REGEXP_REPLACE(col, '[^\\d.]', '') AS DOUBLE) / 100.0
    ELSE TRY_CAST(col AS DOUBLE)
END AS col
```

### Null variants (issue: null_variant)
```sql
CASE
    WHEN LOWER(TRIM(col)) IN ('n/a', 'na', 'null', 'none', '-', '–', '—', '', 'nan', '#n/a')
        THEN NULL
    ELSE col
END AS col
```

### Mixed date formats (issue: mixed_date_format)
```sql
TRY_CAST(col AS TIMESTAMP) AS col
```

### Numeric stored as string (issue: numeric_as_string)
```sql
TRY_CAST(REGEXP_REPLACE(REGEXP_REPLACE(col, ',', ''), '\\s', '') AS DOUBLE) AS col
```

## Critical Rules
- Use ONLY DuckDB-compatible SQL syntax
- Use TRY_CAST (not CAST) for all type conversions — never throw errors on bad values
- Never use DROP, DELETE, UPDATE, INSERT, or any destructive operation
- Never remove rows — only transform column values
- All timestamps must be cast using AT TIME ZONE 'UTC' where applicable
- If a column has no detected issue, include it as-is: just write the column name

## Output Format
Return a JSON object with exactly these fields:
- duckdb_sql: the complete SELECT statement (no markdown fences, just raw SQL)
- explanation: plain English description of what each transformation does
- columns_transformed: list of column names that were transformed

Return ONLY the JSON object. No preamble, no markdown, no extra text."""


def generate_cleaning_script(
    metadata: TableMetadata,
    sample: pd.DataFrame,
    llm_provider: str,
    llm_model: str,
    api_key: str,
) -> CleaningScript:
    """
    Call the LLM with combined metadata + sample to generate cleaning SQL.
    Returns a CleaningScript with the generated SQL and explanation.
    Falls back to a pass-through SELECT if LLM fails.
    """
    prompt = _build_prompt(metadata, sample)
    
    try:
        # Reuse your existing _generate_structured from llm_engine.py
        result: CleaningScriptResponse = _generate_structured(
            prompt=prompt,
            response_schema=CleaningScriptResponse,
            provider=llm_provider,
            model=llm_model,
            api_key=api_key,
            temperature=0.1,  # Low temperature — deterministic output preferred
        )
        
        return CleaningScript(
            table_name=metadata.table_name,
            duckdb_sql=result.duckdb_sql,
            explanation=result.explanation,
            columns_transformed=result.columns_transformed,
            source="llm",
        )
    
    except Exception as e:
        # Deterministic fallback: pass-through SELECT with no transformation
        col_names = ", ".join(col.name for col in metadata.columns)
        return CleaningScript(
            table_name=metadata.table_name,
            duckdb_sql=f"SELECT {col_names} FROM {metadata.table_name}",
            explanation=f"LLM generation failed ({str(e)}). Pass-through SELECT with no transformations applied.",
            columns_transformed=[],
            source="deterministic_fallback",
        )
```

---

## 7. `app/preprocessing/ast_validator.py`

### Purpose
Validate AI-generated cleaning SQL. Block destructive operations. Verify it is a SELECT.

### Implementation

```python
import sqlglot
from sqlglot import exp

DESTRUCTIVE_NODE_TYPES = (
    exp.Drop, exp.Delete, exp.Insert, exp.Update,
    exp.Truncate, exp.Create, exp.AlterTable,
    exp.Command,  # catches raw DDL not parsed as structured nodes
)

class SQLValidationError(Exception):
    pass

def validate_cleaning_sql(sql: str, table_name: str) -> str:
    """
    Validate the AI-generated cleaning SQL.
    Returns the validated SQL string if safe.
    Raises SQLValidationError with a descriptive message if not.
    """
    if not sql or not sql.strip():
        raise SQLValidationError("Generated SQL is empty.")
    
    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except Exception as e:
        raise SQLValidationError(f"SQL could not be parsed: {e}")
    
    if len(statements) != 1:
        raise SQLValidationError(
            f"Expected exactly 1 SQL statement. Got {len(statements)}. "
            "Multi-statement scripts are not allowed."
        )
    
    statement = statements[0]
    
    # Must be a SELECT
    if not isinstance(statement, exp.Select):
        raise SQLValidationError(
            f"Cleaning SQL must be a SELECT statement. Got: {type(statement).__name__}"
        )
    
    # Check for any destructive nodes anywhere in the AST
    for node in statement.walk():
        if isinstance(node, DESTRUCTIVE_NODE_TYPES):
            raise SQLValidationError(
                f"Destructive operation detected in cleaning SQL: {type(node).__name__}. "
                "Cleaning scripts may only use SELECT with transformation expressions."
            )
    
    # Verify it references the correct table (not some other table)
    from_tables = [
        t.name.lower()
        for t in statement.find_all(exp.Table)
    ]
    if table_name.lower() not in from_tables and from_tables:
        raise SQLValidationError(
            f"Cleaning SQL references table(s) {from_tables} "
            f"but should reference {table_name}."
        )
    
    return sql.strip()
```

---

## 8. `app/preprocessing/dry_run.py`

### Purpose
Execute the validated cleaning SQL on the stratified sample. Compute Data Quality Diff.

### Implementation

```python
import duckdb
import pandas as pd
from app.preprocessing.models import DataQualityDiff, CleaningScript

NULL_SPIKE_THRESHOLD = 0.10  # warn if nulls increase by more than 10%

def run_dry_run(
    script: CleaningScript,
    sample: pd.DataFrame,
) -> DataQualityDiff:
    """
    Execute the cleaning SQL against the in-memory stratified sample using DuckDB.
    Compute before/after comparison.
    Returns a DataQualityDiff for user review.
    """
    table_name = script.table_name
    warnings = []
    
    # Register sample as in-memory DuckDB table
    con = duckdb.connect()
    con.register(table_name, sample)
    
    # Capture "before" stats
    before_row_count = len(sample)
    before_nulls = {
        col: int(sample[col].isna().sum())
        for col in sample.columns
    }
    before_types = {col: str(sample[col].dtype) for col in sample.columns}
    
    # Execute cleaning SQL on sample
    try:
        result_df = con.execute(script.duckdb_sql).df()
    except Exception as e:
        # SQL failed on actual data — not safe to lock
        return DataQualityDiff(
            table_name=table_name,
            row_count_before=before_row_count,
            row_count_after=0,
            column_diffs=[],
            warnings=[f"Dry-run execution failed: {str(e)}"],
            safe_to_lock=False,
        )
    finally:
        con.close()
    
    # Capture "after" stats
    after_row_count = len(result_df)
    after_nulls = {
        col: int(result_df[col].isna().sum())
        for col in result_df.columns
        if col in result_df.columns
    }
    after_types = {col: str(result_df[col].dtype) for col in result_df.columns}
    
    # Check row count preservation
    if after_row_count != before_row_count:
        warnings.append(
            f"Row count changed: {before_row_count} → {after_row_count}. "
            "Cleaning SQL should never drop rows."
        )
    
    # Build per-column diff
    column_diffs = []
    safe_to_lock = True
    
    for col in sample.columns:
        if col not in result_df.columns:
            warnings.append(f"Column '{col}' is missing from cleaned output.")
            safe_to_lock = False
            continue
        
        null_before = before_nulls.get(col, 0)
        null_after = after_nulls.get(col, 0)
        null_increase = (null_after - null_before) / max(before_row_count, 1)
        
        if null_increase > NULL_SPIKE_THRESHOLD:
            warnings.append(
                f"Column '{col}': null count increased by "
                f"{null_increase:.1%} ({null_before} → {null_after}). "
                "Check the transformation — TRY_CAST may be failing on many values."
            )
            safe_to_lock = False  # require explicit user acknowledgement
        
        column_diffs.append({
            "column": col,
            "null_before": null_before,
            "null_after": null_after,
            "type_before": before_types.get(col, "unknown"),
            "type_after": after_types.get(col, "unknown"),
            "transformed": col in script.columns_transformed,
        })
    
    return DataQualityDiff(
        table_name=table_name,
        row_count_before=before_row_count,
        row_count_after=after_row_count,
        column_diffs=column_diffs,
        warnings=warnings,
        safe_to_lock=safe_to_lock,
    )
```

---

## 9. `app/preprocessing/cache_engine.py`

### Purpose
Execute the locked cleaning SQL on the full dataset. Store clean data in DuckDB. Manage sync_state.

### Implementation

```python
import duckdb
import sqlalchemy
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.preprocessing.models import CleaningScript, TableMetadata
from app.models import SyncState, ColdStartProgress  # your SQLAlchemy models

CHUNK_SIZE = 100_000  # rows per chunk for cold start

def get_project_duckdb_path(project_id: str) -> str:
    """Match your existing DuckDB path convention."""
    return f"projects/{project_id}.duckdb"


def run_cold_start(
    project_id: str,
    db_uri: str,
    metadata: TableMetadata,
    script: CleaningScript,
    db_session: Session,
) -> dict:
    """
    Execute the locked cleaning SQL on the full source table in chunks.
    Stores clean data in the project's DuckDB file.
    Manages sync_state and cold_start_progress for crash recovery.
    """
    table_name = metadata.table_name
    pk = metadata.primary_key_column
    duckdb_path = get_project_duckdb_path(project_id)
    
    # Mark sync as in_progress
    _upsert_sync_state(db_session, project_id, table_name, metadata.detected_sync_mode, "in_progress")
    
    # Check for existing cold start progress (crash recovery)
    existing_progress = db_session.query(ColdStartProgress).filter_by(
        project_id=project_id,
        table_name=table_name,
        status="in_progress"
    ).first()
    
    resume_from_chunk_id = existing_progress.last_chunk_id if existing_progress else None
    
    source_engine = sqlalchemy.create_engine(db_uri)
    duck_con = duckdb.connect(duckdb_path)
    
    try:
        # Get total row count for progress tracking
        with source_engine.connect() as conn:
            total_rows = conn.execute(
                sqlalchemy.text(f"SELECT COUNT(*) FROM {table_name}")
            ).scalar()
        
        total_chunks = (total_rows // CHUNK_SIZE) + 1
        
        # Create or update cold_start_progress record
        _upsert_cold_start_progress(
            db_session, project_id, table_name,
            total_chunks=total_chunks,
            last_chunk_id=resume_from_chunk_id
        )
        
        # Create staging table in DuckDB
        # We build the staging table first, then atomic rename
        staging_table = f"clean_cache_{table_name}_staging"
        live_table = f"clean_cache_{table_name}_v1"
        view_name = f"clean_cache_{table_name}"
        
        duck_con.execute(f"DROP TABLE IF EXISTS {staging_table}")
        duck_con.execute(f"""
            CREATE TABLE {staging_table} AS
            SELECT * FROM read_csv_auto('/dev/null') LIMIT 0
        """)
        # Note: table structure will be created from first chunk insertion
        
        chunks_done = 0
        last_chunk_id = resume_from_chunk_id
        
        # Chunk extraction by primary key range (if PK exists) or offset
        if pk:
            with source_engine.connect() as conn:
                min_pk = conn.execute(
                    sqlalchemy.text(f"SELECT MIN({pk}) FROM {table_name}")
                ).scalar()
                max_pk = conn.execute(
                    sqlalchemy.text(f"SELECT MAX({pk}) FROM {table_name}")
                ).scalar()
            
            current_pk = int(resume_from_chunk_id) if resume_from_chunk_id else min_pk
            
            while current_pk <= max_pk:
                next_pk = current_pk + CHUNK_SIZE
                
                with source_engine.connect() as conn:
                    chunk_df = pd.read_sql(
                        f"SELECT * FROM {table_name} "
                        f"WHERE {pk} >= {current_pk} AND {pk} < {next_pk}",
                        conn
                    )
                
                if not chunk_df.empty:
                    # Apply cleaning SQL to this chunk using DuckDB
                    chunk_con = duckdb.connect()
                    chunk_con.register(table_name, chunk_df)
                    
                    # Rewrite the cleaning SQL to reference our registered table
                    cleaned_chunk = chunk_con.execute(script.duckdb_sql).df()
                    chunk_con.close()
                    
                    # Insert into staging table
                    duck_con.register("chunk_data", cleaned_chunk)
                    if chunks_done == 0:
                        # First chunk — create the staging table with real schema
                        duck_con.execute(f"DROP TABLE IF EXISTS {staging_table}")
                        duck_con.execute(
                            f"CREATE TABLE {staging_table} AS SELECT * FROM chunk_data"
                        )
                    else:
                        duck_con.execute(
                            f"INSERT INTO {staging_table} SELECT * FROM chunk_data"
                        )
                
                chunks_done += 1
                last_chunk_id = str(current_pk)
                
                # Update progress after every chunk
                _update_cold_start_progress(
                    db_session, project_id, table_name,
                    last_chunk_id=last_chunk_id,
                    chunks_done=chunks_done,
                )
                
                current_pk = next_pk
        
        else:
            # No PK — use OFFSET/LIMIT (slower but works for all tables)
            offset = 0
            
            while True:
                with source_engine.connect() as conn:
                    chunk_df = pd.read_sql(
                        f"SELECT * FROM {table_name} LIMIT {CHUNK_SIZE} OFFSET {offset}",
                        conn
                    )
                
                if chunk_df.empty:
                    break
                
                chunk_con = duckdb.connect()
                chunk_con.register(table_name, chunk_df)
                cleaned_chunk = chunk_con.execute(script.duckdb_sql).df()
                chunk_con.close()
                
                duck_con.register("chunk_data", cleaned_chunk)
                if chunks_done == 0:
                    duck_con.execute(f"DROP TABLE IF EXISTS {staging_table}")
                    duck_con.execute(
                        f"CREATE TABLE {staging_table} AS SELECT * FROM chunk_data"
                    )
                else:
                    duck_con.execute(
                        f"INSERT INTO {staging_table} SELECT * FROM chunk_data"
                    )
                
                chunks_done += 1
                last_chunk_id = str(offset)
                offset += CHUNK_SIZE
                
                _update_cold_start_progress(
                    db_session, project_id, table_name,
                    last_chunk_id=last_chunk_id,
                    chunks_done=chunks_done,
                )
        
        # Atomic view swap — rename first, then repoint view
        duck_con.execute("BEGIN TRANSACTION")
        try:
            duck_con.execute(f"DROP TABLE IF EXISTS {live_table}")
            duck_con.execute(f"ALTER TABLE {staging_table} RENAME TO {live_table}")
            duck_con.execute(
                f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM {live_table}"
            )
            duck_con.execute("COMMIT")
        except Exception:
            duck_con.execute("ROLLBACK")
            raise
        
        # Reconciliation check
        cached_row_count = duck_con.execute(
            f"SELECT COUNT(*) FROM {view_name}"
        ).fetchone()[0]
        
        row_delta_pct = abs(cached_row_count - total_rows) / max(total_rows, 1)
        reconciliation_warning = None
        if row_delta_pct > 0.005:  # 0.5% threshold
            reconciliation_warning = (
                f"Row count mismatch: source={total_rows:,}, "
                f"cache={cached_row_count:,}, "
                f"delta={row_delta_pct:.2%}. Investigate before using dashboard."
            )
        
        # Mark sync as completed
        _upsert_sync_state(
            db_session, project_id, table_name,
            metadata.detected_sync_mode, "completed",
            last_sync_utc=datetime.now(timezone.utc),
            last_row_count=cached_row_count,
        )
        
        # Clean up cold_start_progress
        _complete_cold_start_progress(db_session, project_id, table_name)
        
        return {
            "status": "completed",
            "rows_cached": cached_row_count,
            "chunks_processed": chunks_done,
            "view_name": view_name,
            "reconciliation_warning": reconciliation_warning,
        }
    
    except Exception as e:
        _upsert_sync_state(
            db_session, project_id, table_name,
            metadata.detected_sync_mode, "failed"
        )
        duck_con.close()
        raise RuntimeError(f"Cold start failed for {table_name}: {e}") from e
    
    finally:
        duck_con.close()


# --- State management helpers ---

def _upsert_sync_state(
    session: Session,
    project_id: str,
    table_name: str,
    sync_mode: str,
    status: str,
    last_sync_utc: datetime = None,
    last_row_count: int = None,
):
    existing = session.query(SyncState).filter_by(
        project_id=project_id, table_name=table_name
    ).first()
    
    if existing:
        existing.sync_mode = sync_mode
        existing.status = status
        if last_sync_utc:
            existing.last_sync_utc = last_sync_utc
        if last_row_count is not None:
            existing.last_row_count = last_row_count
        existing.updated_at = datetime.utcnow()
    else:
        session.add(SyncState(
            project_id=project_id,
            table_name=table_name,
            sync_mode=sync_mode,
            status=status,
            last_sync_utc=last_sync_utc,
            last_row_count=last_row_count,
        ))
    session.commit()


def _upsert_cold_start_progress(
    session: Session,
    project_id: str,
    table_name: str,
    total_chunks: int,
    last_chunk_id: str = None,
):
    existing = session.query(ColdStartProgress).filter_by(
        project_id=project_id, table_name=table_name
    ).first()
    
    if not existing:
        session.add(ColdStartProgress(
            project_id=project_id,
            table_name=table_name,
            total_chunks=total_chunks,
            last_chunk_id=last_chunk_id,
            chunks_done=0,
            status="in_progress",
        ))
        session.commit()


def _update_cold_start_progress(
    session: Session,
    project_id: str,
    table_name: str,
    last_chunk_id: str,
    chunks_done: int,
):
    record = session.query(ColdStartProgress).filter_by(
        project_id=project_id, table_name=table_name
    ).first()
    if record:
        record.last_chunk_id = last_chunk_id
        record.chunks_done = chunks_done
        session.commit()


def _complete_cold_start_progress(session: Session, project_id: str, table_name: str):
    record = session.query(ColdStartProgress).filter_by(
        project_id=project_id, table_name=table_name
    ).first()
    if record:
        record.status = "completed"
        session.commit()
        # Archive or delete — delete for MVP simplicity
        session.delete(record)
        session.commit()
```

---

## 10. New API Endpoints — Add to `api.py`

Add these endpoints to your existing `api.py`. Follow your existing endpoint patterns exactly.

```python
# ============================================================
# STAGE 0 — DATA PRE-PROCESSING ENDPOINTS
# ============================================================

from app.preprocessing.connector import get_table_metadata
from app.preprocessing.sampler import extract_stratified_sample, detect_column_issues
from app.preprocessing.script_generator import generate_cleaning_script
from app.preprocessing.ast_validator import validate_cleaning_sql, SQLValidationError
from app.preprocessing.dry_run import run_dry_run
from app.preprocessing.cache_engine import run_cold_start
from app.preprocessing.models import PreprocessingResult


@app.post("/api/projects/{project_id}/preprocess/analyse")
async def analyse_table(project_id: str, request: Request, db: Session = Depends(get_db)):
    """
    Stage 0, Step 1-3: Extract metadata + stratified sample, detect column issues,
    generate cleaning SQL, run AST check, execute dry-run.
    Returns PreprocessingResult for user review — nothing is locked yet.
    """
    body = await request.json()
    table_name = body.get("table_name")
    db_uri = body.get("db_uri")  # or retrieve from project record
    
    if not table_name or not db_uri:
        raise HTTPException(status_code=400, detail="table_name and db_uri are required")
    
    try:
        # Step 1: Extract metadata
        metadata = get_table_metadata(db_uri, table_name)
        
        # Step 2: Pull stratified sample
        sample = extract_stratified_sample(db_uri, metadata)
        
        # Step 3: Detect issues per column and attach to metadata
        for col in metadata.columns:
            col.sample_values = (
                sample[col.name].dropna().astype(str).unique()[:10].tolist()
                if col.name in sample.columns else []
            )
            col.inferred_issue = detect_column_issues(sample, col)
        
        # Step 4: Generate cleaning SQL (metadata + sample combined)
        script = generate_cleaning_script(
            metadata=metadata,
            sample=sample,
            llm_provider=settings.LLM_PROVIDER,
            llm_model=settings.OPENAI_MODEL,  # or whichever is active
            api_key=settings.OPENAI_API_KEY,
        )
        
        # Step 5: AST safety check
        try:
            validate_cleaning_sql(script.duckdb_sql, table_name)
        except SQLValidationError as e:
            raise HTTPException(status_code=422, detail=f"Generated SQL failed safety check: {e}")
        
        # Step 6: Dry-run on sample
        diff = run_dry_run(script, sample)
        
        # DO NOT discard sample yet — it's still in memory, will be GC'd after response
        # The sample is never persisted to disk or DB
        
        return PreprocessingResult(
            project_id=project_id,
            table_name=table_name,
            cleaning_script=script,
            diff=diff,
            status="pending_review",
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/projects/{project_id}/preprocess/confirm")
async def confirm_cleaning_script(project_id: str, request: Request, db: Session = Depends(get_db)):
    """
    Stage 0, Step 4: User has reviewed the dry-run diff and confirmed the cleaning script.
    Lock it permanently to agent_memory under Business_Logic domain.
    Then trigger the cold start cache hydration.
    """
    body = await request.json()
    table_name = body.get("table_name")
    duckdb_sql = body.get("duckdb_sql")
    db_uri = body.get("db_uri")
    
    if not all([table_name, duckdb_sql, db_uri]):
        raise HTTPException(status_code=400, detail="table_name, duckdb_sql, db_uri are required")
    
    # Get current user — follow your existing auth pattern
    user = get_current_user(request, db)
    
    # Lock cleaning SQL to agent_memory — Business_Logic domain
    # Reuse your existing write_memory() function from memory_engine.py
    from app.memory_engine import write_memory
    write_memory(
        db=db,
        user_id=user.user_id,
        project_id=project_id,
        domain="Business_Logic",
        topic=f"cleaning_script_{table_name}",
        content=duckdb_sql,
    )
    
    # Retrieve full metadata for cold start
    metadata = get_table_metadata(db_uri, table_name)
    
    # Build a CleaningScript object from the confirmed SQL
    from app.preprocessing.models import CleaningScript
    locked_script = CleaningScript(
        table_name=table_name,
        duckdb_sql=duckdb_sql,
        explanation="User-confirmed cleaning script",
        columns_transformed=[],
        source="llm_locked",
    )
    
    # Run cold start in background
    import asyncio
    asyncio.create_task(
        _run_cold_start_async(project_id, db_uri, metadata, locked_script, db)
    )
    
    return {
        "status": "cold_start_initiated",
        "message": "Cleaning script locked. Cache hydration started in background.",
        "project_id": project_id,
        "table_name": table_name,
    }


async def _run_cold_start_async(project_id, db_uri, metadata, script, db):
    """Background task for cold start — runs without blocking the API response."""
    try:
        result = run_cold_start(project_id, db_uri, metadata, script, db)
        print(f"Cold start completed for {project_id}/{metadata.table_name}: {result}")
    except Exception as e:
        print(f"Cold start failed for {project_id}/{metadata.table_name}: {e}")


@app.get("/api/projects/{project_id}/preprocess/status")
async def get_preprocessing_status(project_id: str, table_name: str, db: Session = Depends(get_db)):
    """
    Poll this endpoint to check cold start progress.
    Frontend uses this to show progress bar and unlock Stage 1 when ready.
    """
    sync = db.query(SyncState).filter_by(
        project_id=project_id, table_name=table_name
    ).first()
    
    progress = db.query(ColdStartProgress).filter_by(
        project_id=project_id, table_name=table_name
    ).first()
    
    if not sync:
        return {"status": "not_started"}
    
    return {
        "status": sync.status,
        "last_sync_utc": sync.last_sync_utc,
        "last_row_count": sync.last_row_count,
        "chunks_done": progress.chunks_done if progress else None,
        "total_chunks": progress.total_chunks if progress else None,
        "progress_pct": (
            round(progress.chunks_done / progress.total_chunks * 100, 1)
            if progress and progress.total_chunks
            else None
        ),
    }
```

---

## 11. How Stage 0 Connects to Stage 1

After cold start completes, Stage 1 must read from the DuckDB view, not from the raw source.

**Find this in `api.py` (your existing `/api/upload` or `/api/projects/{id}/connect-db` handler):**

```python
# EXISTING code loads data like this:
df = load_from_source(...)
schema_info = detect_schema(df)
```

**Add this check before Stage 1:**

```python
# Check if Stage 0 has already produced a clean cache for this table
from app.models import SyncState

sync_state = db.query(SyncState).filter_by(
    project_id=project_id,
    table_name=table_name,
    status="completed"
).first()

if sync_state:
    # Read from clean DuckDB view instead of raw source
    duckdb_path = f"projects/{project_id}.duckdb"
    con = duckdb.connect(duckdb_path)
    view_name = f"clean_cache_{table_name}"
    df = con.execute(f"SELECT * FROM {view_name} LIMIT 100000").df()
    con.close()
else:
    # No clean cache yet — use raw source (existing behaviour)
    df = load_from_source(...)

# Stage 1 continues unchanged from here
schema_info = detect_schema(df)
```

---

## 12. Frontend Changes — Minimal for MVP

### 12a. New component: `PreprocessingReview.tsx`
Location: `frontend/components/preprocessing/PreprocessingReview.tsx`

Display the Data Quality Diff before the user confirms the cleaning script.

```tsx
// What to show the user:
// 1. Plain English explanation of what the script does
// 2. Data Quality Diff table: column | type before | type after | nulls before | nulls after
// 3. Warnings (highlighted in amber if present)
// 4. "Confirm & Start Processing" button — disabled if safe_to_lock is false
// 5. "Edit Transformations" link (optional for MVP — skip if time-constrained)
// 6. Progress bar (polls /api/projects/{id}/preprocess/status every 3 seconds)
```

### 12b. State machine addition
In `useAppStore.ts`, add a new state before your existing first state:

```typescript
// Add to AppState type:
| "preprocessing_analysis"    // Stage 0 running
| "preprocessing_review"      // User reviewing diff
| "preprocessing_cold_start"  // Cold start in progress
// existing states follow...
```

---

## 13. `.env` Additions

Add these to your `.env` and `pydantic-settings` config:

```
# Stage 0 — Data Pre-Processing
PREPROCESSING_SAMPLE_SIZE=2000        # rows in stratified sample
PREPROCESSING_CHUNK_SIZE=100000       # rows per cold start chunk
PREPROCESSING_NULL_SPIKE_THRESHOLD=0.10  # warn if nulls increase by this %
PREPROCESSING_RECONCILIATION_THRESHOLD=0.005  # 0.5% row count delta threshold
PREPROCESSING_ENABLED=true            # feature flag — set false to skip Stage 0
```

---

## 14. Build Order for the Agent

Execute in this exact order. Do not skip steps.

```
Step 1:  Create app/preprocessing/__init__.py (empty)
Step 2:  Create app/preprocessing/models.py
Step 3:  Add SyncState and ColdStartProgress to app/models.py
Step 4:  Run database migration (add_sync_state.sql)
Step 5:  Create app/preprocessing/connector.py
Step 6:  Create app/preprocessing/sampler.py
Step 7:  Create app/preprocessing/script_generator.py
Step 8:  Create app/preprocessing/ast_validator.py
Step 9:  Create app/preprocessing/dry_run.py
Step 10: Create app/preprocessing/cache_engine.py
Step 11: Add three new endpoints to api.py
Step 12: Modify Stage 1 entry point in api.py to check for clean cache
Step 13: Add .env variables to settings
Step 14: Create frontend/components/preprocessing/PreprocessingReview.tsx
Step 15: Add new states to useAppStore.ts
Step 16: End-to-end test with a messy PostgreSQL table
```

---

## 15. Test Scenarios — Run All Before Marking Complete

### Test 1 — Clean data (no transformations needed)
- Source: PostgreSQL table with all correct types
- Expected: Script is a pass-through SELECT, diff shows no changes, cold start completes

### Test 2 — Currency strings
- Source: Table with `revenue VARCHAR` containing `"$1,234.56"` values
- Expected: Script generates REGEXP_REPLACE logic, diff shows VARCHAR→DOUBLE, nulls stable

### Test 3 — Mixed date formats
- Source: Table with `order_date VARCHAR` containing mixed `"01/02/23"` and `"2023-Feb-01"`
- Expected: Script generates TRY_CAST(col AS TIMESTAMP), diff shows conversion

### Test 4 — Null variants
- Source: Table with `"N/A"`, `"-"`, `""` mixed with real values
- Expected: Script generates CASE WHEN LOWER(TRIM(col)) IN (...) THEN NULL

### Test 5 — Crash recovery
- Kill the process midway through cold start
- Restart — system should resume from last_chunk_id, not from row 0

### Test 6 — Large table (>1M rows)
- Source: Table with 2M rows
- Expected: Chunked extraction, progress visible in status endpoint, no timeout

### Test 7 — AST rejection
- Manually inject a SQL with `DROP TABLE` in the transformation
- Expected: ast_validator raises SQLValidationError, endpoint returns 422

### Test 8 — LLM failure fallback
- Disable LLM API key
- Expected: Pass-through SELECT returned, user is informed, flow continues

---

## 16. Critical Rules for the Agent

1. **Never modify any file in `app/understanding_engine.py`, `app/llm_engine.py`, `app/dashboard_engine.py`, `app/insight_engine.py`, `app/clarification_engine.py`, `app/goal_engine.py`, `app/context_engine.py`, `app/confirmation_engine.py`, `app/sql_safety.py`.** These are existing pipeline stages. Stage 0 is additive only.

2. **Always use `TRY_CAST` not `CAST` in generated SQL.** Failed casts must produce NULL, never throw an error.

3. **The stratified sample must never be written to disk, to the database, or to `agent_memory`.** It exists in memory only for the duration of the analysis endpoint call.

4. **The cleaning SQL is locked to `agent_memory` under domain `Business_Logic`, topic `cleaning_script_{table_name}`.** Use the existing `write_memory()` function. Do not create a new storage mechanism.

5. **Always rename the staging table before repointing the view.** Never drop the view. `CREATE OR REPLACE VIEW` is the correct pattern.

6. **The cold start must update `cold_start_progress` after every chunk commit.** Not at the end — after every chunk. This is what enables crash recovery.

7. **Reuse `_generate_structured` from `llm_engine.py` for all LLM calls.** Do not create a new HTTP client or LLM abstraction.

8. **Follow the existing FastAPI endpoint pattern exactly** — same error handling style, same `Depends(get_db)`, same auth pattern as existing endpoints in `api.py`.
