"""Run Stage 0 against the clarum_test dataset (DB_Test_files) and report accuracy.

For each of the 8 tables: analyse -> generate cleaning SQL -> AST validate ->
dry-run diff -> (optionally) lock + cold start. Prints a per-table, per-column
report so you can compare against DB_Test_files/03_test_checklist.md.

Run:  .venv/bin/python -m scripts.run_clarum_test [--cold-start]
"""
from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb

from app.config import get_settings
from app.db import init_db, session_scope
from app.memory_engine import write_memory
from app.preprocessing.ast_validator import SQLValidationError, validate_cleaning_sql
from app.preprocessing.cache_engine import get_project_duckdb_path, run_cold_start
from app.preprocessing.profiler import build_cleaning_script, profile_table

SOURCE_URI = "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres"
SCHEMA = "clarum_test"
PROJECT_ID = "clarum_test_project"

TABLES = [
    "sales_reps",
    "customers",
    "products",
    "orders",
    "returns",
    "marketing_campaigns",
    "inventory_snapshots",
    "support_tickets",
]


def run_table(table: str, do_cold_start: bool) -> dict:
    print(f"\n{'#'*70}\n# TABLE: {table}\n{'#'*70}")

    metadata, sample = profile_table(SOURCE_URI, table, schema=SCHEMA)
    print(f"  rows={metadata.row_count}  pk={metadata.primary_key_column}  "
          f"sync_mode={metadata.detected_sync_mode}  change_col={metadata.change_tracking_column}")

    issues = {c.name: c.inferred_issues for c in metadata.columns if c.inferred_issues}
    print("  detected issues:")
    for name, issue_list in issues.items():
        col = next(c for c in metadata.columns if c.name == name)
        print(f"    {name:22} -> {', '.join(issue_list):18} samples={col.sample_values[:3]}")
    if not issues:
        print("    (none detected)")

    script = build_cleaning_script(metadata, sample)
    print(f"  cleaning script source: {script.source}")
    print(f"  columns_transformed: {script.columns_transformed}")

    try:
        validate_cleaning_sql(script.duckdb_sql, table)
        validation_status = "PASSED"
    except SQLValidationError as e:
        validation_status = f"REJECTED: {e}"
        print(f"  AST validation: {validation_status}")
        return {"table": table, "status": "validation_failed"}
    print(f"  AST validation: {validation_status}")

    from app.preprocessing.dry_run import run_dry_run
    diff = run_dry_run(script, sample)
    print(f"  dry-run: before={diff.row_count_before} after={diff.row_count_after} "
          f"safe_to_lock={diff.safe_to_lock}")
    for w in diff.warnings:
        print(f"    WARNING: {w}")
    for d in diff.column_diffs:
        if d["transformed"]:
            print(f"    {d['column']:22} {d['type_before']:>10} -> {d['type_after']:<10}  "
                  f"nulls {d['null_before']:>4} -> {d['null_after']:<4}")

    result = {
        "table": table,
        "rows": metadata.row_count,
        "sync_mode": metadata.detected_sync_mode,
        "issues_detected": issues,
        "script_source": script.source,
        "columns_transformed": script.columns_transformed,
        "validation": validation_status,
        "safe_to_lock": diff.safe_to_lock,
        "warnings": diff.warnings,
    }

    if do_cold_start:
        with session_scope() as db:
            write_memory(db, project_id=PROJECT_ID, domain="Business_Logic",
                         topic=f"cleaning_script_{table}", content=script.duckdb_sql)
            cold = run_cold_start(PROJECT_ID, SOURCE_URI, metadata, script, db)
        print(f"  cold start: {cold['status']} rows_cached={cold['rows_cached']} "
              f"reconciliation={cold['reconciliation_warning'] or 'OK'}")
        result["cold_start"] = cold

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cold-start", action="store_true",
                        help="Lock script + build DuckDB cache for each table")
    parser.add_argument("--tables", nargs="*", default=TABLES)
    args = parser.parse_args()

    settings = get_settings()
    init_db()
    print(f"Source: {SOURCE_URI}  schema={SCHEMA}")
    print(f"LLM provider: {settings.LLM_PROVIDER}  model: {settings.active_model}  "
          f"key set: {bool(settings.active_api_key)}")

    results = []
    for table in args.tables:
        results.append(run_table(table, args.cold_start))

    print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
    for r in results:
        if r.get("status") == "validation_failed":
            print(f"  {r['table']:22} VALIDATION FAILED")
            continue
        flag = "OK " if r["safe_to_lock"] else "WARN"
        print(f"  {r['table']:22} rows={r['rows']:<6} mode={r['sync_mode']:<14} "
              f"transformed={len(r['columns_transformed']):<2} src={r['script_source']:<20} {flag}")

    if args.cold_start:
        print(f"\nDuckDB cache: {get_project_duckdb_path(PROJECT_ID)}")
        con = duckdb.connect(get_project_duckdb_path(PROJECT_ID))
        for table in args.tables:
            try:
                n = con.execute(f'SELECT COUNT(*) FROM "clean_cache_{table}"').fetchone()[0]
                print(f"  clean_cache_{table}: {n} rows")
            except Exception as e:
                print(f"  clean_cache_{table}: ERROR {e}")
        con.close()


if __name__ == "__main__":
    main()
