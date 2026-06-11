"""End-to-end Stage 0 demo.

Walks the full pipeline against a freshly-built messy SQLite source table:

  analyse -> validate -> dry-run -> confirm/lock -> cold start -> read cache

If an LLM key is configured in .env the cleaning SQL is generated live;
otherwise the demo uses a built-in hand-written cleaning SQL so you can still
see real cleaning happen end-to-end. Requires only the Postgres control DB.

Run:  .venv/bin/python -m scripts.demo
"""
from __future__ import annotations

import os
import sys

import duckdb

# Make sure repo root is importable when run as a file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings
from app.db import init_db, session_scope
from app.memory_engine import read_memory, write_memory
from app.models import ColdStartProgress, SyncState
from app.preprocessing.ast_validator import SQLValidationError, validate_cleaning_sql
from app.preprocessing.cache_engine import get_project_duckdb_path, run_cold_start
from app.preprocessing.connector import get_table_metadata
from app.preprocessing.dry_run import run_dry_run
from app.preprocessing.profiler import enrich_metadata_with_sample
from app.preprocessing.sampler import extract_stratified_sample
from app.preprocessing.script_generator import generate_cleaning_script
from tests.fixtures import build_source

PROJECT_ID = "demo_project"
TABLE = "sales"
SOURCE_DB = "/tmp/clarum_demo_source.db"
SOURCE_URI = f"sqlite:///{SOURCE_DB}"

# Built-in cleaning SQL used only when no LLM key is configured.
_FALLBACK_CLEAN_SQL = f"""
SELECT id,
  CASE WHEN TRIM(amount) IN ('N/A','-','') THEN NULL
       ELSE TRY_CAST(REGEXP_REPLACE(amount,'[^0-9.]','','g') AS DOUBLE) END AS amount,
  CASE WHEN TRIM(discount) IN ('N/A','-','') THEN NULL
       ELSE TRY_CAST(REGEXP_REPLACE(discount,'[^0-9.]','','g') AS DOUBLE)/100.0 END AS discount,
  CASE WHEN LOWER(TRIM(region)) IN ('n/a','na','null','none','-','') THEN NULL
       ELSE region END AS region,
  price_str, order_date, created_at, updated_at
FROM {TABLE}"""


def banner(n: int, title: str) -> None:
    print(f"\n{'='*70}\nSTEP {n}: {title}\n{'='*70}")


def main() -> None:
    settings = get_settings()

    banner(0, "Bootstrap: control DB tables + messy source table")
    init_db()
    if os.path.exists(SOURCE_DB):
        os.remove(SOURCE_DB)
    build_source(SOURCE_URI, TABLE, n_rows=2000, with_updated=True)
    print(f"  source: {SOURCE_URI}  (2000 rows of deliberately messy data)")
    print(f"  control DB: {settings.CONTROL_DB_URI}")
    print(f"  LLM provider: {settings.LLM_PROVIDER}  model: {settings.active_model}")
    print(f"  LLM key set: {bool(settings.active_api_key)}")

    # ---------------------------------------------------------------- analyse
    banner(1, "ANALYSE — metadata + stratified sample + issue detection")
    metadata = get_table_metadata(SOURCE_URI, TABLE)
    sample = extract_stratified_sample(SOURCE_URI, metadata)
    enrich_metadata_with_sample(metadata, sample)
    print(f"  rows={metadata.row_count}  pk={metadata.primary_key_column}  "
          f"sync_mode={metadata.detected_sync_mode}")
    print(f"  stratified sample held in memory: {len(sample)} rows")
    for c in metadata.columns:
        if c.inferred_issue:
            print(f"    issue: {c.name:12} -> {c.inferred_issue:18} e.g. {c.sample_values[:2]}")

    # -------------------------------------------------------- generate script
    banner(2, "GENERATE — cleaning SQL (LLM if key set, else built-in)")
    if settings.active_api_key:
        script = generate_cleaning_script(metadata, sample)
        print(f"  source={script.source}  (live LLM)")
        if script.source == "deterministic_fallback":
            print("  LLM call failed; using built-in cleaning SQL instead.")
            script.duckdb_sql = _FALLBACK_CLEAN_SQL
            script.columns_transformed = ["amount", "discount", "region"]
    else:
        from app.preprocessing.models import CleaningScript
        script = CleaningScript(
            table_name=TABLE, duckdb_sql=_FALLBACK_CLEAN_SQL,
            explanation="Built-in demo cleaning SQL (no LLM key configured).",
            columns_transformed=["amount", "discount", "region"], source="builtin_demo",
        )
        print("  no LLM key -> using built-in cleaning SQL")
    print("  --- cleaning SQL ---")
    print("  " + script.duckdb_sql.strip().replace("\n", "\n  "))

    # -------------------------------------------------------------- validate
    banner(3, "VALIDATE — AST safety check (SELECT-only, no DML/DDL)")
    try:
        validate_cleaning_sql(script.duckdb_sql, TABLE)
        print("  PASSED safety check")
    except SQLValidationError as e:
        print(f"  REJECTED: {e}")
        return

    # --------------------------------------------------------------- dry-run
    banner(4, "DRY-RUN — Data Quality Diff over the in-memory sample")
    diff = run_dry_run(script, sample)
    print(f"  rows before={diff.row_count_before}  after={diff.row_count_after}  "
          f"safe_to_lock={diff.safe_to_lock}")
    for d in diff.column_diffs:
        if d["transformed"]:
            print(f"    {d['column']:10} type {d['type_before']} -> {d['type_after']}  "
                  f"nulls {d['null_before']} -> {d['null_after']}")
    for w in diff.warnings:
        print(f"    WARNING: {w}")
    print("  (warnings here are advisory — the user reviews and confirms)")

    # --------------------------------------------------------- confirm + lock
    banner(5, "CONFIRM — lock cleaning SQL permanently to agent_memory")
    with session_scope() as db:
        write_memory(db, project_id=PROJECT_ID, domain="Business_Logic",
                     topic=f"cleaning_script_{TABLE}", content=script.duckdb_sql)
        locked = read_memory(db, PROJECT_ID, "Business_Logic", f"cleaning_script_{TABLE}")
    print(f"  locked {len(locked)} chars of SQL (never regenerated)")

    # ------------------------------------------------------------- cold start
    banner(6, "COLD START — run locked SQL over full data -> DuckDB cache")
    with session_scope() as db:
        result = run_cold_start(PROJECT_ID, SOURCE_URI, metadata, script, db)
    print(f"  status={result['status']}  rows_cached={result['rows_cached']}  "
          f"source_rows={result['source_rows']}")
    print(f"  reconciliation: {result['reconciliation_warning'] or 'OK (within threshold)'}")
    print(f"  cache file: {result['duckdb_path']}  view: {result['view']}")

    # ------------------------------------------------ read cache (Stage 1 view)
    banner(7, "STAGE 1 HANDOFF — read clean cache the way Stage 1 will")
    con = duckdb.connect(get_project_duckdb_path(PROJECT_ID))
    rows = con.execute(
        f'SELECT id, amount, discount, region FROM "clean_cache_{TABLE}" ORDER BY id LIMIT 5'
    ).fetchall()
    types = con.execute(
        f'SELECT typeof(amount), typeof(discount) FROM "clean_cache_{TABLE}" LIMIT 1'
    ).fetchone()
    con.close()
    print(f"  amount typeof={types[0]}  discount typeof={types[1]}  (were VARCHAR at source)")
    for r in rows:
        print(f"    id={r[0]}  amount={r[1]}  discount={r[2]}  region={r[3]}")

    # ----------------------------------------------------------- control state
    banner(8, "CONTROL STATE — sync_state + cold_start_progress in Postgres")
    with session_scope() as db:
        ss = db.query(SyncState).filter_by(project_id=PROJECT_ID, table_name=TABLE).first()
        cp = db.query(ColdStartProgress).filter_by(project_id=PROJECT_ID, table_name=TABLE).first()
        print(f"  sync_state: status={ss.status} mode={ss.sync_mode} rows={ss.last_row_count} "
              f"last_sync={ss.last_sync_utc}")
        print(f"  cold_start_progress: status={cp.status} {cp.chunks_done}/{cp.total_chunks} chunks")

    print(f"\n{'='*70}\nDEMO COMPLETE — Stage 0 produced a clean, query-ready cache.\n{'='*70}")


if __name__ == "__main__":
    main()
