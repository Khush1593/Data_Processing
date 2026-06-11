## Agent Instructions: Hybrid Data Pre-Processing Layer

## Clarum Insights — MVP Implementation Spec

**Version:** 1.1 (Production Ready)
**Target Codebase:** Clarum Insights (FastAPI + DuckDB + Next.js)
**Scope:** Implement a hybrid metadata + stratified sample data pre-processing layer that runs before Stage 1 of the existing pipeline. This is a new Stage 0 — it does not replace or modify any existing stage.

---

## 0. Context — What Exists, What You Are Adding

### Existing Pipeline (DO NOT MODIFY)

* **Stage 1** → `data_engine.py` (`detect_schema()`, `normalise_string_dimensions()`)
* **Stage 2** → `semantic_engine.py` (keyword-score heuristics)
* **Stage 3** → `understanding_engine.py` + `llm_engine.py`
* **Stage 3.5**→ User blueprint review
* **Stage 4** → `clarification_engine.py`
* **Stage 5** → `goal_engine.py`
* **Stage 6** → `context_engine.py`
* **Stage 7** → `confirmation_engine.py`
* **Stage 8** → `dashboard_engine.py`
* **Stage 9** → `sql_safety.py` + DuckDB execution
* **Stage 10** → `insight_engine.py`
* **Stage 11** → Q&A loop in `api.py`

### What You Are Building — Stage 0

A new pre-processing layer inserted before Stage 1. It:

1. Connects to the client's database and extracts schema metadata.
2. Pulls a naive chunk of rows and creates a stratified sample locally.
3. Sends metadata + sample to the LLM to generate a DuckDB-dialect cleaning SQL script.
4. Validates the SQL via AST check.
5. Runs a dry-run preview and shows a Data Quality Diff to the user.
6. On user confirmation, locks the SQL permanently to `agent_memory`.
7. Executes the locked cleaning SQL on full data to produce a clean DuckDB cache.
8. Sets up a `sync_state` table to track sync status.
9. Passes clean data to Stage 1 exactly as before.

### Guiding Principles

- **AI writes cleaning SQL once. It is locked permanently after user confirmation. Never regenerated.**
- **Raw row data never leaves the client's server in Tier 2 mode. Only metadata and sample rows (for script generation) touch our cloud in Tier 1.**
- **The stratified sample is discarded from memory after the cleaning SQL is locked. Never persisted.**
- **All existing pipeline stages remain completely unchanged.**
- **Every new function follows the existing pattern: deterministic fallback first, LLM enriches.**

---

## 1. New Files To Create

```text
app/
  preprocessing/
    __init__.py
    connector.py          # DB connection + information_schema extraction
    sampler.py            # in-memory stratified sample extraction
    profiler.py           # metadata profiler
    script_generator.py   # LLM prompt builder + cleaning SQL generation
    ast_validator.py      # AST safety check for cleaning SQL
    dry_run.py            # execute SQL on sample, compute Data Quality Diff
    cache_engine.py       # cold start execution, sync_state management
    models.py             # Pydantic schemas for this layer

db/
  migrations/
    add_sync_state.sql     # sync_state and cold_start_progress table DDL
    add_cleaning_cache.sql # cleaning_script storage in agent_memory
```


## 2. Database Schema Changes

### 2a. sync_state table

Add to your SQLAlchemy models and run migration:

**SQL**

```
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

Add to `app/models.py`:

**Python**

```
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

## 3. Pydantic Schemas — `app/preprocessing/models.py`

**Python**

```
from pydantic import BaseModel, Field
from typing import Optional

class ColumnMetadata(BaseModel):
    name: str
    declared_type: str
    null_pct: float
    distinct_count: int
    is_primary_key: bool = False
    has_created_at: bool = False
    has_updated_at: bool = False
    has_deleted_at: bool = False
    sample_values: list[str] = Field(default_factory=list)
    inferred_issue: Optional[str] = None

class TableMetadata(BaseModel):
    table_name: str
    row_count: int
    columns: list[ColumnMetadata]
    detected_sync_mode: str
    primary_key_column: Optional[str] = None
    change_tracking_column: Optional[str] = None

class CleaningScript(BaseModel):
    table_name: str
    duckdb_sql: str
    explanation: str
    columns_transformed: list[str]
    source: str = "llm"

class DataQualityDiff(BaseModel):
    table_name: str
    row_count_before: int
    row_count_after: int
    column_diffs: list[dict]
    warnings: list[str]
    safe_to_lock: bool

class PreprocessingResult(BaseModel):
    project_id: str
    table_name: str
    cleaning_script: CleaningScript
    diff: DataQualityDiff
    status: str
```

## 4. `app/preprocessing/connector.py`

**Python**

```
import sqlalchemy
from sqlalchemy import text
from app.preprocessing.models import ColumnMetadata, TableMetadata

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
    engine = sqlalchemy.create_engine(db_uri)
  
    with engine.connect() as conn:
        columns_result = conn.execute(text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = :table_name
            ORDER BY ordinal_position
        """), {"table_name": table_name}).fetchall()
      
        row_count = conn.execute(
            text(f"SELECT COUNT(*) FROM {table_name}")
        ).scalar()
      
        null_pcts = {}
        for col_name, _, _ in columns_result:
            null_pcts[col_name] = conn.execute(text(f"""
                SELECT CAST(SUM(CASE WHEN {col_name} IS NULL THEN 1 ELSE 0 END) AS FLOAT)
                       / NULLIF(COUNT(*), 0)
                FROM {table_name}
            """)).scalar() or 0.0
      
        distinct_counts = {}
        for col_name, _, _ in columns_result:
            distinct_counts[col_name] = conn.execute(
                text(f"SELECT COUNT(DISTINCT {col_name}) FROM {table_name}")
            ).scalar() or 0
      
        try:
            pk_result = conn.execute(text(PRIMARY_KEY_QUERY), {"table_name": table_name}).fetchone()
            primary_key_col = pk_result[0] if pk_result else None
        except Exception:
            primary_key_col = None
  
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

## 5. `app/preprocessing/sampler.py`

### Purpose

Pull a naive chunk, then stratify it in memory. DO NOT query the live DB with `ORDER BY`.

**Python**

```
import pandas as pd
import sqlalchemy
from app.preprocessing.models import TableMetadata

SAMPLE_TARGET_ROWS = 1000
NAIVE_CHUNK_SIZE = 10000

def extract_stratified_sample(
    db_uri: str,
    metadata: TableMetadata,
) -> pd.DataFrame:
    """
    Pull a naive 10k chunk locally, then stratify to find edge cases.
    Protects client DB from heavy ORDER BY analytics.
    """
    engine = sqlalchemy.create_engine(db_uri)
    table = metadata.table_name
  
    # 1. Pull naive chunk quickly
    with engine.connect() as conn:
        try:
            raw_chunk = pd.read_sql(
                f"SELECT * FROM {table} LIMIT {NAIVE_CHUNK_SIZE}",
                conn
            )
        except Exception:
            return pd.DataFrame()
          
    if raw_chunk.empty:
        return raw_chunk

    frames = []
  
    # 2. Add random baseline (400 rows)
    frames.append(raw_chunk.sample(n=min(400, len(raw_chunk))))
  
    # 3. Add null-revealing rows locally
    for col in metadata.columns:
        if col.name in raw_chunk.columns and col.null_pct > 0:
            nulls = raw_chunk[raw_chunk[col.name].isna()]
            if not nulls.empty:
                frames.append(nulls.head(10))
              
    # 4. Add boundary rows locally (numeric outliers)
    numeric_types = {"integer", "bigint", "numeric", "decimal", "float", "real", "double precision"}
    for col in metadata.columns:
        if col.name in raw_chunk.columns and col.declared_type.lower() in numeric_types:
            try:
                # Convert to numeric locally to find boundaries safely
                temp_series = pd.to_numeric(raw_chunk[col.name], errors='coerce')
                valid_idx = temp_series.dropna().index
                if not valid_idx.empty:
                    sorted_idx = temp_series.loc[valid_idx].sort_values()
                    frames.append(raw_chunk.loc[sorted_idx.head(20).index])
                    frames.append(raw_chunk.loc[sorted_idx.tail(20).index])
            except Exception:
                pass
              
    # Combine, deduplicate, cap
    combined = pd.concat(frames, ignore_index=True).drop_duplicates()
    return combined.head(SAMPLE_TARGET_ROWS)


def detect_column_issues(sample: pd.DataFrame, metadata_col) -> str | None:
    if sample.empty or metadata_col.name not in sample.columns:
        return None
  
    col_series = sample[metadata_col.name].dropna().astype(str).str.strip()
    if len(col_series) == 0:
        return None
  
    currency_pattern = col_series.str.contains(r'[$€£₹¥]', regex=True)
    if currency_pattern.mean() > 0.3:
        return "currency_string"
  
    pct_pattern = col_series.str.contains(r'%$', regex=True)
    if pct_pattern.mean() > 0.3:
        return "percentage_string"
  
    null_variants = {"n/a", "na", "null", "none", "-", "–", "—", "", "nan", "#n/a"}
    null_variant_pattern = col_series.str.lower().isin(null_variants)
    if null_variant_pattern.mean() > 0.05:
        return "null_variant"
  
    if metadata_col.declared_type.lower() in {"varchar", "text", "character varying"}:
        date_like = col_series.str.match(
            r'\d{1,4}[-/\.]\d{1,2}[-/\.]\d{1,4}|\w+ \d{1,2},? \d{4}',
            na=False
        )
        if date_like.mean() > 0.5:
            return "mixed_date_format"
          
        numeric_like = pd.to_numeric(
            col_series.str.replace(r'[,$€£₹¥%]', '', regex=True),
            errors='coerce'
        ).notna()
        if numeric_like.mean() > 0.85:
            return "numeric_as_string"
          
    return None
```

## 6. `app/preprocessing/script_generator.py`

**Python**

```
import json
import pandas as pd
from app.preprocessing.models import TableMetadata, CleaningScript, ColumnMetadata
from app.llm_engine import _generate_structured
from pydantic import BaseModel

class CleaningScriptResponse(BaseModel):
    duckdb_sql: str
    explanation: str
    columns_transformed: list[str]

    class Config:
        extra = "ignore"
        str_strip_whitespace = True

def _build_sample_summary(sample: pd.DataFrame, metadata: TableMetadata) -> str:
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

## Column Metadata + Top Unique Sample Values
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

**SQL**

```
CASE
    WHEN TRIM(col) IN ('N/A', '-', '', 'null', 'none', 'NA') THEN NULL
    WHEN col LIKE '%\\%'
        THEN TRY_CAST(REGEXP_REPLACE(col, '[^\\d.]', '') AS DOUBLE) / 100.0
    ELSE TRY_CAST(col AS DOUBLE)
END AS col
```

### Null variants (issue: null_variant)

**SQL**

```
CASE
    WHEN LOWER(TRIM(col)) IN ('n/a', 'na', 'null', 'none', '-', '–', '—', '', 'nan', '#n/a')
        THEN NULL
    ELSE col
END AS col
```

### Mixed date formats (issue: mixed_date_format)

**SQL**

```
TRY_CAST(col AS TIMESTAMP) AS col
```

### Numeric stored as string (issue: numeric_as_string)

**SQL**

```
TRY_CAST(REGEXP_REPLACE(REGEXP_REPLACE(col, ',', ''), '\\s', '') AS DOUBLE) AS col
```

## Critical Rules

* Use ONLY DuckDB-compatible SQL syntax
* Use TRY_CAST (not CAST) for all type conversions
* Never use DROP, DELETE, UPDATE, INSERT
* Never remove rows
* All timestamps must be cast using AT TIME ZONE 'UTC' where applicable
* Include non-issue columns as-is

Return ONLY a JSON object with: duckdb_sql, explanation, columns_transformed."""

def generate_cleaning_script(

metadata: TableMetadata, sample: pd.DataFrame, llm_provider: str, llm_model: str, api_key: str,

) -> CleaningScript:

prompt = _build_prompt(metadata, sample)

try:

result: CleaningScriptResponse = _generate_structured(

prompt=prompt,

response_schema=CleaningScriptResponse,

provider=llm_provider,

model=llm_model,

api_key=api_key,

temperature=0.1,

)

return CleaningScript(

table_name=metadata.table_name,

duckdb_sql=result.duckdb_sql,

explanation=result.explanation,

columns_transformed=result.columns_transformed,

)

except Exception as e:

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

```python
import sqlglot
from sqlglot import exp

DESTRUCTIVE_NODE_TYPES = (
    exp.Drop, exp.Delete, exp.Insert, exp.Update,
    exp.Truncate, exp.Create, exp.AlterTable, exp.Command,
)

class SQLValidationError(Exception):
    pass

def validate_cleaning_sql(sql: str, table_name: str) -> str:
    if not sql or not sql.strip():
        raise SQLValidationError("Generated SQL is empty.")
    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except Exception as e:
        raise SQLValidationError(f"SQL could not be parsed: {e}")
    if len(statements) != 1:
        raise SQLValidationError("Expected exactly 1 SQL statement.")
  
    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise SQLValidationError("Cleaning SQL must be a SELECT statement.")
      
    for node in statement.walk():
        if isinstance(node, DESTRUCTIVE_NODE_TYPES):
            raise SQLValidationError(f"Destructive operation detected: {type(node).__name__}")
          
    from_tables = [t.name.lower() for t in statement.find_all(exp.Table)]
    if table_name.lower() not in from_tables and from_tables:
        raise SQLValidationError(f"SQL references incorrect tables: {from_tables}")
      
    return sql.strip()
```

## 8. `app/preprocessing/dry_run.py`

**Python**

```
import duckdb
import pandas as pd
from app.preprocessing.models import DataQualityDiff, CleaningScript

NULL_SPIKE_THRESHOLD = 0.10

def run_dry_run(script: CleaningScript, sample: pd.DataFrame) -> DataQualityDiff:
    table_name = script.table_name
    warnings = []
  
    con = duckdb.connect()
    con.register(table_name, sample)
  
    before_row_count = len(sample)
    before_nulls = {col: int(sample[col].isna().sum()) for col in sample.columns}
    before_types = {col: str(sample[col].dtype) for col in sample.columns}
  
    try:
        result_df = con.execute(script.duckdb_sql).df()
    except Exception as e:
        return DataQualityDiff(
            table_name=table_name, row_count_before=before_row_count, row_count_after=0,
            column_diffs=[], warnings=[f"Dry-run failed: {str(e)}"], safe_to_lock=False,
        )
    finally:
        con.close()
      
    after_row_count = len(result_df)
    after_nulls = {col: int(result_df[col].isna().sum()) for col in result_df.columns}
    after_types = {col: str(result_df[col].dtype) for col in result_df.columns}
  
    if after_row_count != before_row_count:
        warnings.append("Row count changed. Cleaning SQL must not drop rows.")
      
    column_diffs = []
    safe_to_lock = True
  
    for col in sample.columns:
        if col not in result_df.columns:
            warnings.append(f"Column '{col}' missing from output.")
            safe_to_lock = False
            continue
          
        null_before = before_nulls.get(col, 0)
        null_after = after_nulls.get(col, 0)
        null_increase = (null_after - null_before) / max(before_row_count, 1)
      
        if null_increase > NULL_SPIKE_THRESHOLD:
            warnings.append(f"Column '{col}': nulls increased by {null_increase:.1%}.")
            safe_to_lock = False
          
        column_diffs.append({
            "column": col, "null_before": null_before, "null_after": null_after,
            "type_before": before_types.get(col, "unknown"), "type_after": after_types.get(col, "unknown"),
            "transformed": col in script.columns_transformed,
        })
      
    return DataQualityDiff(
        table_name=table_name, row_count_before=before_row_count, row_count_after=after_row_count,
        column_diffs=column_diffs, warnings=warnings, safe_to_lock=safe_to_lock,
    )
```

## 9. `app/preprocessing/cache_engine.py`

**Python**

```
import duckdb
import sqlalchemy
import pandas as pd
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.preprocessing.models import CleaningScript, TableMetadata
from app.models import SyncState, ColdStartProgress

CHUNK_SIZE = 100_000

def get_project_duckdb_path(project_id: str) -> str:
    return f"projects/{project_id}.duckdb"

def run_cold_start(
    project_id: str, db_uri: str, metadata: TableMetadata, script: CleaningScript, db_session: Session,
) -> dict:
    table_name = metadata.table_name
    pk = metadata.primary_key_column
    duckdb_path = get_project_duckdb_path(project_id)
  
    _upsert_sync_state(db_session, project_id, table_name, metadata.detected_sync_mode, "in_progress")
  
    existing_progress = db_session.query(ColdStartProgress).filter_by(
        project_id=project_id, table_name=table_name, status="in_progress"
    ).first()
    resume_from_chunk_id = existing_progress.last_chunk_id if existing_progress else None
  
    source_engine = sqlalchemy.create_engine(db_uri)
    duck_con = duckdb.connect(duckdb_path)
  
    try:
        with source_engine.connect() as conn:
            total_rows = conn.execute(sqlalchemy.text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
          
        total_chunks = (total_rows // CHUNK_SIZE) + 1
        _upsert_cold_start_progress(db_session, project_id, table_name, total_chunks, resume_from_chunk_id)
      
        staging_table = f"clean_cache_{table_name}_staging"
        live_table = f"clean_cache_{table_name}_v1"
        view_name = f"clean_cache_{table_name}"
      
        duck_con.execute(f"DROP TABLE IF EXISTS {staging_table}")
        duck_con.execute(f"CREATE TABLE {staging_table} AS SELECT * FROM read_csv_auto('/dev/null') LIMIT 0")
      
        chunks_done = 0
        if pk:
            with source_engine.connect() as conn:
                min_pk = conn.execute(sqlalchemy.text(f"SELECT MIN({pk}) FROM {table_name}")).scalar()
                max_pk = conn.execute(sqlalchemy.text(f"SELECT MAX({pk}) FROM {table_name}")).scalar()
            current_pk = int(resume_from_chunk_id) if resume_from_chunk_id else min_pk
          
            while current_pk <= max_pk:
                next_pk = current_pk + CHUNK_SIZE
                with source_engine.connect() as conn:
                    chunk_df = pd.read_sql(f"SELECT * FROM {table_name} WHERE {pk} >= {current_pk} AND {pk} < {next_pk}", conn)
                if not chunk_df.empty:
                    chunk_con = duckdb.connect()
                    chunk_con.register(table_name, chunk_df)
                    cleaned_chunk = chunk_con.execute(script.duckdb_sql).df()
                    chunk_con.close()
                  
                    duck_con.register("chunk_data", cleaned_chunk)
                    if chunks_done == 0:
                        duck_con.execute(f"DROP TABLE IF EXISTS {staging_table}")
                        duck_con.execute(f"CREATE TABLE {staging_table} AS SELECT * FROM chunk_data")
                    else:
                        duck_con.execute(f"INSERT INTO {staging_table} SELECT * FROM chunk_data")
                      
                chunks_done += 1
                _update_cold_start_progress(db_session, project_id, table_name, str(current_pk), chunks_done)
                current_pk = next_pk
        else:
            offset = 0
            while True:
                with source_engine.connect() as conn:
                    chunk_df = pd.read_sql(f"SELECT * FROM {table_name} LIMIT {CHUNK_SIZE} OFFSET {offset}", conn)
                if chunk_df.empty: break
              
                chunk_con = duckdb.connect()
                chunk_con.register(table_name, chunk_df)
                cleaned_chunk = chunk_con.execute(script.duckdb_sql).df()
                chunk_con.close()
              
                duck_con.register("chunk_data", cleaned_chunk)
                if chunks_done == 0:
                    duck_con.execute(f"DROP TABLE IF EXISTS {staging_table}")
                    duck_con.execute(f"CREATE TABLE {staging_table} AS SELECT * FROM chunk_data")
                else:
                    duck_con.execute(f"INSERT INTO {staging_table} SELECT * FROM chunk_data")
                  
                chunks_done += 1
                offset += CHUNK_SIZE
                _update_cold_start_progress(db_session, project_id, table_name, str(offset), chunks_done)
      
        # Atomic Swap
        duck_con.execute("BEGIN TRANSACTION")
        try:
            duck_con.execute(f"DROP TABLE IF EXISTS {live_table}")
            duck_con.execute(f"ALTER TABLE {staging_table} RENAME TO {live_table}")
            duck_con.execute(f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM {live_table}")
            duck_con.execute("COMMIT")
        except Exception:
            duck_con.execute("ROLLBACK")
            raise
          
        cached_row_count = duck_con.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]
        row_delta_pct = abs(cached_row_count - total_rows) / max(total_rows, 1)
        reconciliation_warning = (f"Row count mismatch! Source: {total_rows}, Cache: {cached_row_count}") if row_delta_pct > 0.005 else None
      
        _upsert_sync_state(db_session, project_id, table_name, metadata.detected_sync_mode, "completed", datetime.now(timezone.utc), cached_row_count)
        _complete_cold_start_progress(db_session, project_id, table_name)
      
        return {"status": "completed", "rows_cached": cached_row_count, "reconciliation_warning": reconciliation_warning}
    except Exception as e:
        _upsert_sync_state(db_session, project_id, table_name, metadata.detected_sync_mode, "failed")
        raise RuntimeError(f"Cold start failed: {e}")
    finally:
        duck_con.close()

def _upsert_sync_state(session: Session, project_id: str, table_name: str, sync_mode: str, status: str, last_sync_utc: datetime = None, last_row_count: int = None):
    existing = session.query(SyncState).filter_by(project_id=project_id, table_name=table_name).first()
    if existing:
        existing.sync_mode = sync_mode
        existing.status = status
        if last_sync_utc: existing.last_sync_utc = last_sync_utc
        if last_row_count is not None: existing.last_row_count = last_row_count
    else:
        session.add(SyncState(project_id=project_id, table_name=table_name, sync_mode=sync_mode, status=status, last_sync_utc=last_sync_utc, last_row_count=last_row_count))
    session.commit()

def _upsert_cold_start_progress(session: Session, project_id: str, table_name: str, total_chunks: int, last_chunk_id: str = None):
    existing = session.query(ColdStartProgress).filter_by(project_id=project_id, table_name=table_name).first()
    if not existing:
        session.add(ColdStartProgress(project_id=project_id, table_name=table_name, total_chunks=total_chunks, last_chunk_id=last_chunk_id, status="in_progress"))
        session.commit()

def _update_cold_start_progress(session: Session, project_id: str, table_name: str, last_chunk_id: str, chunks_done: int):
    record = session.query(ColdStartProgress).filter_by(project_id=project_id, table_name=table_name).first()
    if record:
        record.last_chunk_id = last_chunk_id
        record.chunks_done = chunks_done
        session.commit()

def _complete_cold_start_progress(session: Session, project_id: str, table_name: str):
    record = session.query(ColdStartProgress).filter_by(project_id=project_id, table_name=table_name).first()
    if record:
        session.delete(record)
        session.commit()
```

## 10. New API Endpoints — Add to `api.py`

**Python**

```
from app.preprocessing.connector import get_table_metadata
from app.preprocessing.sampler import extract_stratified_sample, detect_column_issues
from app.preprocessing.script_generator import generate_cleaning_script
from app.preprocessing.ast_validator import validate_cleaning_sql, SQLValidationError
from app.preprocessing.dry_run import run_dry_run
from app.preprocessing.cache_engine import run_cold_start
from app.preprocessing.models import PreprocessingResult

@app.post("/api/projects/{project_id}/preprocess/analyse")
async def analyse_table(project_id: str, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    table_name = body.get("table_name")
    db_uri = body.get("db_uri")
  
    if not table_name or not db_uri:
        raise HTTPException(status_code=400, detail="table_name and db_uri required")
    try:
        metadata = get_table_metadata(db_uri, table_name)
        sample = extract_stratified_sample(db_uri, metadata)
      
        for col in metadata.columns:
            col.sample_values = sample[col.name].dropna().astype(str).unique()[:10].tolist() if col.name in sample.columns else []
            col.inferred_issue = detect_column_issues(sample, col)
          
        script = generate_cleaning_script(metadata, sample, settings.LLM_PROVIDER, settings.OPENAI_MODEL, settings.OPENAI_API_KEY)
      
        try: validate_cleaning_sql(script.duckdb_sql, table_name)
        except SQLValidationError as e: raise HTTPException(status_code=422, detail=f"SQL failed safety check: {e}")
          
        diff = run_dry_run(script, sample)
        return PreprocessingResult(project_id=project_id, table_name=table_name, cleaning_script=script, diff=diff, status="pending_review")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/projects/{project_id}/preprocess/confirm")
async def confirm_cleaning_script(project_id: str, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    table_name, duckdb_sql, db_uri = body.get("table_name"), body.get("duckdb_sql"), body.get("db_uri")
  
    if not all([table_name, duckdb_sql, db_uri]):
        raise HTTPException(status_code=400, detail="table_name, duckdb_sql, db_uri required")
      
    user = get_current_user(request, db)
    from app.memory_engine import write_memory
    write_memory(db=db, user_id=user.user_id, project_id=project_id, domain="Business_Logic", topic=f"cleaning_script_{table_name}", content=duckdb_sql)
  
    metadata = get_table_metadata(db_uri, table_name)
    from app.preprocessing.models import CleaningScript
    locked_script = CleaningScript(table_name=table_name, duckdb_sql=duckdb_sql, explanation="Confirmed", columns_transformed=[], source="llm_locked")
  
    import asyncio
    asyncio.create_task(_run_cold_start_async(project_id, db_uri, metadata, locked_script, db))
    return {"status": "cold_start_initiated", "project_id": project_id, "table_name": table_name}

async def _run_cold_start_async(project_id, db_uri, metadata, script, db):
    try: run_cold_start(project_id, db_uri, metadata, script, db)
    except Exception as e: print(f"Cold start failed: {e}")

@app.get("/api/projects/{project_id}/preprocess/status")
async def get_preprocessing_status(project_id: str, table_name: str, db: Session = Depends(get_db)):
    sync = db.query(SyncState).filter_by(project_id=project_id, table_name=table_name).first()
    progress = db.query(ColdStartProgress).filter_by(project_id=project_id, table_name=table_name).first()
    if not sync: return {"status": "not_started"}
    return {"status": sync.status, "progress_pct": round(progress.chunks_done / progress.total_chunks * 100, 1) if progress and progress.total_chunks else None}
```

## 11. How Stage 0 Connects to Stage 1

**Add this check before Stage 1 (`api.py`):**

**Python**

```
from app.models import SyncState

sync_state = db.query(SyncState).filter_by(project_id=project_id, table_name=table_name, status="completed").first()

if sync_state:
    duckdb_path = f"projects/{project_id}.duckdb"
    con = duckdb.connect(duckdb_path)
    view_name = f"clean_cache_{table_name}"
    df = con.execute(f"SELECT * FROM {view_name} LIMIT 100000").df()
    con.close()
else:
    df = load_from_source(...)

schema_info = detect_schema(df)
```

## 12. Frontend Changes & .env

**State Machine:**

**TypeScript**

```
| "preprocessing_analysis"
| "preprocessing_review"
| "preprocessing_cold_start"
```

**.env Additions:**

**Code snippet**

```
PREPROCESSING_SAMPLE_SIZE=1000
PREPROCESSING_CHUNK_SIZE=100000
PREPROCESSING_NULL_SPIKE_THRESHOLD=0.10
PREPROCESSING_RECONCILIATION_THRESHOLD=0.005
PREPROCESSING_ENABLED=true
```

## 13. Build Order for the Agent

Step 1:  Create `app/preprocessing/__init__.py`

Step 2:  Create `app/preprocessing/models.py`

Step 3:  Add `SyncState` and `ColdStartProgress` to `app/models.py`

Step 4:  Run database migration

Step 5:  Create `app/preprocessing/connector.py`

Step 6:  Create `app/preprocessing/sampler.py`

Step 7:  Create `app/preprocessing/script_generator.py`

Step 8:  Create `app/preprocessing/ast_validator.py`

Step 9:  Create `app/preprocessing/dry_run.py`

Step 10: Create `app/preprocessing/cache_engine.py`

Step 11: Add three new endpoints to `api.py`

Step 12: Modify Stage 1 entry point

Step 13: Add `.env` variables

Step 14: Add new states to `useAppStore.ts`

Step 15: Run Test Scenarios 1 through 8.
