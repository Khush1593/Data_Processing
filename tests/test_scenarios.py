"""Stage 0 acceptance scenarios 1-8 (process.md §13, step 15).

Each scenario exercises one layer end-to-end. Tests parametrised on
``source_uri`` run against BOTH SQLite and (when available) PostgreSQL sources.
"""
from __future__ import annotations

import uuid

import duckdb
import pytest

from app.db import session_scope
from app.llm_engine import register_provider
from app.memory_engine import read_memory, write_memory
from app.models import ColdStartProgress, SyncState
from app.preprocessing import cache_engine
from app.preprocessing.ast_validator import SQLValidationError, validate_cleaning_sql
from app.preprocessing.connector import get_table_metadata
from app.preprocessing.dry_run import run_dry_run
from app.preprocessing.models import CleaningScript
from app.preprocessing.profiler import profile_table
from app.preprocessing.sampler import detect_column_issues, extract_stratified_sample
from app.preprocessing.script_generator import generate_cleaning_script
from tests.fixtures import build_source


def _tbl() -> str:
    return f"sales_{uuid.uuid4().hex[:8]}"


def _clean_sql(table: str) -> str:
    return f"""
    SELECT id,
      CASE WHEN TRIM(amount) IN ('N/A','-','') THEN NULL
           ELSE TRY_CAST(REGEXP_REPLACE(amount,'[^0-9.]','','g') AS DOUBLE) END AS amount,
      CASE WHEN LOWER(TRIM(region)) IN ('n/a','na','null','none','-','') THEN NULL
           ELSE region END AS region,
      discount, price_str, order_date, created_at, updated_at
    FROM {table}"""


# --------------------------------------------------------------------------
# Scenario 1 — Connector: metadata + sync-mode detection (multi-dialect)
# --------------------------------------------------------------------------
def test_scenario_1_connector_metadata(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=300, with_updated=True)
    md = get_table_metadata(source_uri, table)
    assert md.row_count == 300
    assert md.primary_key_column == "id"
    assert md.detected_sync_mode == "upsert"  # pk + updated_at
    assert md.change_tracking_column == "updated_at"
    names = {c.name for c in md.columns}
    assert {"amount", "discount", "region", "updated_at"} <= names


def test_scenario_1b_delete_aware(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=200, with_updated=True, with_deleted=True)
    md = get_table_metadata(source_uri, table)
    assert md.detected_sync_mode == "delete_aware"
    assert any(c.has_deleted_at for c in md.columns)


# --------------------------------------------------------------------------
# Scenario 2 — Sampler: stratification + issue detection (all issue types)
# --------------------------------------------------------------------------
def test_scenario_2_sampler_issues(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=500)
    md = get_table_metadata(source_uri, table)
    sample = extract_stratified_sample(source_uri, md)
    assert not sample.empty and len(sample) <= 1000
    issues = {c.name: detect_column_issues(sample, c) for c in md.columns}
    assert issues["amount"] == "currency_string"
    assert issues["discount"] == "percentage_string"
    assert issues["order_date"] == "mixed_date_format"
    assert issues["region"] == "null_variant"
    assert issues["id"] is None


# --------------------------------------------------------------------------
# Scenario 3 — Profiler: metadata enriched with sample values + issues
# --------------------------------------------------------------------------
def test_scenario_3_profiler(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=300)
    md, sample = profile_table(source_uri, table)
    amount = next(c for c in md.columns if c.name == "amount")
    assert amount.inferred_issue == "currency_string"
    assert len(amount.sample_values) > 0
    assert all(isinstance(v, str) for v in amount.sample_values)


# --------------------------------------------------------------------------
# Scenario 4 — Script generator: LLM path (fake provider) + fallback
# --------------------------------------------------------------------------
def test_scenario_4_script_generator_llm(sqlite_uri):
    table = _tbl()
    build_source(sqlite_uri, table, n_rows=200)
    md, sample = profile_table(sqlite_uri, table)

    sql = _clean_sql(table)

    def fake(prompt, model, api_key, temperature, timeout):
        import json
        return json.dumps({
            "duckdb_sql": sql,
            "explanation": "cleaned amount + region",
            "columns_transformed": ["amount", "region"],
        })

    register_provider("groq", fake)
    script = generate_cleaning_script(md, sample, llm_provider="groq", api_key="dummy")
    assert script.source == "llm"
    assert "amount" in script.columns_transformed
    validate_cleaning_sql(script.duckdb_sql, table)  # must pass safety check


def test_scenario_4b_script_generator_fallback(sqlite_uri):
    table = _tbl()
    build_source(sqlite_uri, table, n_rows=100)
    md, sample = profile_table(sqlite_uri, table)
    # No api key -> deterministic pass-through fallback.
    script = generate_cleaning_script(md, sample, llm_provider="gemini", api_key=None)
    assert script.source == "deterministic_fallback"
    assert script.columns_transformed == []
    validate_cleaning_sql(script.duckdb_sql, table)


# --------------------------------------------------------------------------
# Scenario 5 — AST validator: accept SELECT, block everything destructive
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad_sql", [
    "DELETE FROM sales",
    "DROP TABLE sales",
    "UPDATE sales SET a=1",
    "INSERT INTO sales VALUES (1)",
    "TRUNCATE sales",
    "SELECT * FROM other_table",
    "SELECT * FROM sales; DROP TABLE sales",
    "",
    "SELECT * FRM sales",
])
def test_scenario_5_ast_blocks(bad_sql):
    with pytest.raises(SQLValidationError):
        validate_cleaning_sql(bad_sql, "sales")


def test_scenario_5b_ast_accepts():
    assert validate_cleaning_sql("SELECT a, TRY_CAST(b AS DOUBLE) AS b FROM sales", "sales")


# --------------------------------------------------------------------------
# Scenario 6 — Dry run: safe passthrough vs. row-drop / column-drop / null-spike
# --------------------------------------------------------------------------
def test_scenario_6_dry_run(sqlite_uri):
    table = _tbl()
    build_source(sqlite_uri, table, n_rows=300)
    md, sample = profile_table(sqlite_uri, table)

    def script(sql, cols=None):
        return CleaningScript(table_name=table, duckdb_sql=sql, explanation="",
                              columns_transformed=cols or [])

    safe = run_dry_run(script(f"SELECT * FROM {table}"), sample)
    assert safe.safe_to_lock and safe.row_count_before == safe.row_count_after

    rowdrop = run_dry_run(script(f"SELECT * FROM {table} WHERE region='North'"), sample)
    assert not rowdrop.safe_to_lock
    assert any("Row count changed" in w for w in rowdrop.warnings)

    coldrop = run_dry_run(script(f"SELECT id, amount FROM {table}"), sample)
    assert not coldrop.safe_to_lock
    assert any("missing from output" in w for w in coldrop.warnings)


# --------------------------------------------------------------------------
# Scenario 7 — Cold start (single chunk, integer PK): cache + reconciliation
# --------------------------------------------------------------------------
def test_scenario_7_cold_start(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=800)
    md = get_table_metadata(source_uri, table)
    project = f"proj_{uuid.uuid4().hex[:8]}"
    script = CleaningScript(table_name=table, duckdb_sql=_clean_sql(table),
                            explanation="locked", columns_transformed=["amount", "region"],
                            source="llm_locked")
    with session_scope() as s:
        result = cache_engine.run_cold_start(project, source_uri, md, script, s)
    assert result["status"] == "completed"
    assert result["rows_cached"] == 800
    assert result["reconciliation_warning"] is None

    con = duckdb.connect(cache_engine.get_project_duckdb_path(project))
    n = con.execute(f'SELECT COUNT(*) FROM "clean_cache_{table}"').fetchone()[0]
    typ = con.execute(f'SELECT typeof(amount) FROM "clean_cache_{table}" WHERE amount IS NOT NULL LIMIT 1').fetchone()[0]
    con.close()
    assert n == 800 and typ == "DOUBLE"

    with session_scope() as s:
        ss = s.query(SyncState).filter_by(project_id=project, table_name=table).first()
        assert ss.status == "completed" and ss.last_row_count == 800


# --------------------------------------------------------------------------
# Scenario 8 — Multi-chunk PK + offset(no-PK) path + memory lock + idempotency
# --------------------------------------------------------------------------
def test_scenario_8_multichunk_offset_memory(sqlite_uri, monkeypatch):
    table = _tbl()
    build_source(sqlite_uri, table, n_rows=500)
    md = get_table_metadata(sqlite_uri, table)
    script = CleaningScript(table_name=table, duckdb_sql=_clean_sql(table),
                            explanation="locked", columns_transformed=["amount", "region"],
                            source="llm_locked")

    # Force tiny chunks -> exercises INSERT-into-staging across many chunks.
    monkeypatch.setattr(cache_engine, "CHUNK_SIZE", 100)

    project = f"proj_{uuid.uuid4().hex[:8]}"
    with session_scope() as s:
        r1 = cache_engine.run_cold_start(project, sqlite_uri, md, script, s)
    assert r1["rows_cached"] == 500
    with session_scope() as s:
        cp = s.query(ColdStartProgress).filter_by(project_id=project, table_name=table).first()
        assert cp.total_chunks >= 5  # 500/100

    # Re-run is idempotent (rebuild + atomic swap), not a duplicate.
    with session_scope() as s:
        r2 = cache_engine.run_cold_start(project, sqlite_uri, md, script, s)
    assert r2["rows_cached"] == 500

    # Offset path: force PK=None so chunking falls back to LIMIT/OFFSET.
    md_no_pk = md.model_copy(deep=True)
    md_no_pk.primary_key_column = None
    project2 = f"proj_{uuid.uuid4().hex[:8]}"
    with session_scope() as s:
        r3 = cache_engine.run_cold_start(project2, sqlite_uri, md_no_pk, script, s)
    assert r3["rows_cached"] == 500

    # memory_engine: lock the script, confirm idempotent upsert.
    with session_scope() as s:
        write_memory(s, project_id=project, domain="Business_Logic",
                     topic=f"cleaning_script_{table}", content=script.duckdb_sql)
        write_memory(s, project_id=project, domain="Business_Logic",
                     topic=f"cleaning_script_{table}", content=script.duckdb_sql)
        locked = read_memory(s, project, "Business_Logic", f"cleaning_script_{table}")
        assert locked == script.duckdb_sql
        from app.models import AgentMemory
        cnt = s.query(AgentMemory).filter_by(project_id=project, topic=f"cleaning_script_{table}").count()
        assert cnt == 1  # upsert, not duplicate
