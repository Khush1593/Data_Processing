"""Stage 0 — cold-start execution + sync_state management (process.md §9).

Runs the LOCKED cleaning SQL over the full source table in chunks, writing the
cleaned result into a per-project DuckDB cache. The build is done into a
*staging* table and atomically swapped to the live table + view, so Stage 1
never reads a half-built cache.

Hardening vs. the reference spec:
  * Output schema is established from a ``LIMIT 0`` read (no ``read_csv_auto
    ('/dev/null')`` hack; empty source tables are handled correctly).
  * No ``chunks_done == 0`` branch — staging always exists before inserts, so a
    gap in the first PK range can't crash the run.
  * Integer-PK range chunking is used only when the PK is actually integer;
    otherwise we fall back to LIMIT/OFFSET.
  * Each run rebuilds staging from scratch (idempotent), then atomically swaps.
"""
from __future__ import annotations

import os

import duckdb
import pandas as pd
import sqlalchemy
from datetime import datetime, timezone
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.memory_engine import write_memory
from app.models import ColdStartProgress, SyncState
from app.preprocessing.models import CleaningScript, DataQualityDiff, TableMetadata

_settings = get_settings()
CHUNK_SIZE = _settings.PREPROCESSING_CHUNK_SIZE
RECONCILIATION_THRESHOLD = _settings.PREPROCESSING_RECONCILIATION_THRESHOLD
NULL_SPIKE_THRESHOLD = _settings.PREPROCESSING_NULL_SPIKE_THRESHOLD

INTEGER_TYPE_HINTS = {"int", "integer", "bigint", "smallint"}

# Maps pandas/numpy dtype strings (as produced by dry_run.py's
# ``after_types``) to DuckDB column types, used to define the staging table
# schema explicitly from the dry-run's Data Quality Diff rather than
# inferring it from whichever chunk happens to be cold-started first.
_DTYPE_TO_DUCKDB = {
    "int64": "BIGINT",
    "int32": "INTEGER",
    "float64": "DOUBLE",
    "float32": "FLOAT",
    "bool": "BOOLEAN",
    "object": "VARCHAR",
}


def _duckdb_type_for_dtype(dtype_str: str) -> str:
    if dtype_str.startswith("datetime64"):
        return "TIMESTAMP"
    return _DTYPE_TO_DUCKDB.get(dtype_str, "VARCHAR")


def get_project_duckdb_path(project_id: str) -> str:
    os.makedirs(_settings.DUCKDB_CACHE_DIR, exist_ok=True)
    return os.path.join(_settings.DUCKDB_CACHE_DIR, f"{project_id}.duckdb")


def _qd(ident: str) -> str:
    """Quote a DuckDB identifier."""
    return '"' + ident.replace('"', '""') + '"'


def _pk_is_integer(metadata: TableMetadata) -> bool:
    if not metadata.primary_key_column:
        return False
    for col in metadata.columns:
        if col.name == metadata.primary_key_column:
            return any(h in col.declared_type.lower() for h in INTEGER_TYPE_HINTS)
    return False


def _clean_chunk(chunk_df: pd.DataFrame, table_name: str, script: CleaningScript) -> pd.DataFrame:
    """Run the cleaning SQL against one chunk in an isolated in-memory DuckDB."""
    con = duckdb.connect()
    try:
        # Defense-in-depth: LLM-authored SQL must not be able to read/write
        # files or network locations (e.g. read_csv_auto('/etc/passwd')).
        con.execute("SET enable_external_access=false")
        con.register(table_name, chunk_df)
        return con.execute(script.duckdb_sql).df()
    finally:
        con.close()


def run_cold_start(
    project_id: str,
    db_uri: str,
    metadata: TableMetadata,
    script: CleaningScript,
    db_session: Session,
    diff: DataQualityDiff | None = None,
) -> dict:
    table_name = metadata.table_name
    duckdb_path = get_project_duckdb_path(project_id)

    _upsert_sync_state(db_session, project_id, table_name, metadata.detected_sync_mode, "in_progress")

    source_engine = sqlalchemy.create_engine(db_uri)
    qsrc = source_engine.dialect.identifier_preparer.quote(table_name)
    if metadata.source_schema:
        qsrc = f"{source_engine.dialect.identifier_preparer.quote(metadata.source_schema)}.{qsrc}"
    duck_con = duckdb.connect(duckdb_path)
    duck_con.execute("SET enable_external_access=false")

    staging = f"clean_cache_{table_name}_staging"
    live = f"clean_cache_{table_name}_v1"
    view = f"clean_cache_{table_name}"

    try:
        with source_engine.connect() as conn:
            total_rows = conn.execute(
                sqlalchemy.text(f"SELECT COUNT(*) FROM {qsrc}")
            ).scalar() or 0

        total_chunks = max((total_rows // CHUNK_SIZE) + (1 if total_rows % CHUNK_SIZE else 0), 1)
        _upsert_cold_start_progress(db_session, project_id, table_name, total_chunks)

        # The staging schema is defined explicitly from the dry-run's Data
        # Quality Diff (the cleaning SQL's output types over the sample),
        # NOT inferred from whichever chunk happens to be cold-started
        # first — a later chunk with different inferred types (e.g. an
        # all-null column in chunk 1 vs. populated in chunk 2) must not be
        # able to change the staging schema mid-run.
        duck_con.execute(f"DROP TABLE IF EXISTS {_qd(staging)}")
        if diff is not None and diff.column_diffs:
            column_types = {
                cd["column"]: _duckdb_type_for_dtype(cd.get("type_after", "object"))
                for cd in diff.column_diffs
            }
            cols_sql = ", ".join(f"{_qd(col)} {dtype}" for col, dtype in column_types.items())
            duck_con.execute(f"CREATE TABLE {_qd(staging)} ({cols_sql})")
        else:
            # No dry-run diff available (e.g. called directly, not via the
            # orchestrator): defer schema creation to the first non-empty
            # chunk, inferring types from its cleaned output.
            column_types = None
        state = {"column_types": column_types}

        # --- Stream chunks into staging ---
        chunks_done = 0
        if _pk_is_integer(metadata):
            pk = metadata.primary_key_column
            qpk = source_engine.dialect.identifier_preparer.quote(pk)
            with source_engine.connect() as conn:
                min_pk = conn.execute(sqlalchemy.text(f"SELECT MIN({qpk}) FROM {qsrc}")).scalar()
                max_pk = conn.execute(sqlalchemy.text(f"SELECT MAX({qpk}) FROM {qsrc}")).scalar()
            if min_pk is not None:
                current = int(min_pk)
                max_pk = int(max_pk)
                while current <= max_pk:
                    nxt = current + CHUNK_SIZE
                    with source_engine.connect() as conn:
                        chunk_df = pd.read_sql(
                            sqlalchemy.text(
                                f"SELECT * FROM {qsrc} WHERE {qpk} >= :lo AND {qpk} < :hi"
                            ),
                            conn,
                            params={"lo": current, "hi": nxt},
                        )
                    _append_chunk(duck_con, staging, chunk_df, table_name, script, state)
                    chunks_done += 1
                    _update_cold_start_progress(db_session, project_id, table_name, str(current), chunks_done)
                    current = nxt
        else:
            # Tables without a (usable) primary key have no stable range to
            # chunk over, so LIMIT/OFFSET is used. To make repeated
            # LIMIT/OFFSET pages consistent (no duplicated/skipped rows from
            # concurrent writes or per-page connection churn), the whole
            # non-PK cold start runs inside a single connection/transaction
            # at REPEATABLE READ isolation (where supported) with an
            # explicit ORDER BY for deterministic pagination.
            order_cols = [metadata.primary_key_column] if metadata.primary_key_column else [
                c.name for c in metadata.columns
            ]
            order_by_sql = ", ".join(
                source_engine.dialect.identifier_preparer.quote(c) for c in order_cols
            )
            conn_options = {}
            if source_engine.dialect.name == "postgresql":
                conn_options["isolation_level"] = "REPEATABLE READ"

            with source_engine.connect().execution_options(**conn_options) as conn:
                with conn.begin():
                    offset = 0
                    while True:
                        chunk_df = pd.read_sql(
                            sqlalchemy.text(
                                f"SELECT * FROM {qsrc} ORDER BY {order_by_sql} LIMIT :lim OFFSET :off"
                            ),
                            conn,
                            params={"lim": CHUNK_SIZE, "off": offset},
                        )
                        if chunk_df.empty:
                            break
                        _append_chunk(duck_con, staging, chunk_df, table_name, script, state)
                        chunks_done += 1
                        offset += CHUNK_SIZE
                        _update_cold_start_progress(db_session, project_id, table_name, str(offset), chunks_done)

        # Empty source table + no diff: staging was never created above (no
        # chunks ran to infer types from). Fall back to VARCHAR per the
        # source metadata so the live table + view still exist for Stage 1.
        if state["column_types"] is None:
            column_types = {col.name: "VARCHAR" for col in metadata.columns}
            cols_sql = ", ".join(f"{_qd(col)} {dtype}" for col, dtype in column_types.items())
            duck_con.execute(f"CREATE TABLE {_qd(staging)} ({cols_sql})")

        # --- Atomic swap: staging -> live, recreate view ---
        duck_con.execute("BEGIN TRANSACTION")
        try:
            duck_con.execute(f"DROP TABLE IF EXISTS {_qd(live)}")
            duck_con.execute(f"ALTER TABLE {_qd(staging)} RENAME TO {_qd(live)}")
            duck_con.execute(f"CREATE OR REPLACE VIEW {_qd(view)} AS SELECT * FROM {_qd(live)}")
            duck_con.execute("COMMIT")
        except Exception:
            duck_con.execute("ROLLBACK")
            raise

        cached_row_count = duck_con.execute(f"SELECT COUNT(*) FROM {_qd(view)}").fetchone()[0]
        row_delta_pct = abs(cached_row_count - total_rows) / max(total_rows, 1)
        reconciliation_warning = (
            f"Row count mismatch! Source: {total_rows}, Cache: {cached_row_count}"
            if row_delta_pct > RECONCILIATION_THRESHOLD
            else None
        )

        _post_cold_start_quality_check(
            duck_con, view, source_engine, qsrc, metadata, diff,
            total_rows, cached_row_count, db_session, project_id, table_name,
        )

        _upsert_sync_state(
            db_session, project_id, table_name, metadata.detected_sync_mode, "completed",
            datetime.now(timezone.utc), cached_row_count,
        )
        _complete_cold_start_progress(db_session, project_id, table_name)

        return {
            "status": "completed",
            "rows_cached": cached_row_count,
            "source_rows": total_rows,
            "reconciliation_warning": reconciliation_warning,
            "duckdb_path": duckdb_path,
            "view": view,
        }
    except Exception as e:
        _upsert_sync_state(db_session, project_id, table_name, metadata.detected_sync_mode, "failed")
        raise RuntimeError(f"Cold start failed: {e}") from e
    finally:
        duck_con.close()
        source_engine.dispose()


def _append_chunk(duck_con, staging: str, chunk_df: pd.DataFrame, table_name: str,
                  script: CleaningScript, state: dict) -> None:
    if chunk_df.empty:
        return
    cleaned = _clean_chunk(chunk_df, table_name, script)
    duck_con.register("chunk_data", cleaned)
    try:
        if state["column_types"] is None:
            # No dry-run diff was available: define the staging schema from
            # this (first non-empty) chunk's cleaned output types.
            column_types = {
                col: _duckdb_type_for_dtype(str(cleaned[col].dtype)) for col in cleaned.columns
            }
            cols_sql = ", ".join(f"{_qd(col)} {dtype}" for col, dtype in column_types.items())
            duck_con.execute(f"CREATE TABLE {_qd(staging)} ({cols_sql})")
            state["column_types"] = column_types

        # The staging table's schema is fixed up front, so every chunk is
        # explicitly cast to it on insert rather than letting DuckDB infer
        # types per chunk.
        column_types = state["column_types"]
        select_cols = ", ".join(
            f"CAST({_qd(col)} AS {column_types.get(col, 'VARCHAR')}) AS {_qd(col)}"
            for col in cleaned.columns
        )
        duck_con.execute(f"INSERT INTO {_qd(staging)} SELECT {select_cols} FROM chunk_data")
    finally:
        duck_con.unregister("chunk_data")


def _post_cold_start_quality_check(
    duck_con, view: str, source_engine: Engine, qsrc: str, metadata: TableMetadata,
    diff: DataQualityDiff | None, total_rows: int, cached_row_count: int,
    db_session: Session, project_id: str, table_name: str,
) -> None:
    """Lightweight full-data sanity check after cold start: compare each
    column's null rate in the cache against the source, and flag any spike
    that wasn't predicted by the dry-run sample (e.g. a TRY_CAST that works
    on the sample but silently nulls out a chunk of real data). Findings are
    written to the audit log (``agent_memory`` domain ``Audit_Log``) rather
    than failing the run — the cache is already built and swapped in.
    """
    if total_rows == 0 or cached_row_count == 0 or diff is None or not diff.column_diffs:
        return

    try:
        col_names = [cd["column"] for cd in diff.column_diffs if cd["column"] in {c.name for c in metadata.columns}]
        if not col_names:
            return

        select_parts = [
            f"SUM(CASE WHEN {source_engine.dialect.identifier_preparer.quote(c)} IS NULL THEN 1 ELSE 0 END)"
            for c in col_names
        ]
        with source_engine.connect() as conn:
            source_row = conn.execute(
                sqlalchemy.text(f"SELECT {', '.join(select_parts)} FROM {qsrc}")
            ).fetchone()

        cache_select_parts = [
            f"SUM(CASE WHEN {_qd(c)} IS NULL THEN 1 ELSE 0 END)" for c in col_names
        ]
        cache_row = duck_con.execute(
            f"SELECT {', '.join(cache_select_parts)} FROM {_qd(view)}"
        ).fetchone()

        expected_increase = {
            cd["column"]: (cd["null_after"] - cd["null_before"]) / max(diff.row_count_before, 1)
            for cd in diff.column_diffs
        }

        findings = []
        for i, col in enumerate(col_names):
            source_null_pct = (source_row[i] or 0) / total_rows
            cache_null_pct = (cache_row[i] or 0) / cached_row_count
            actual_increase = cache_null_pct - source_null_pct
            if actual_increase - expected_increase.get(col, 0.0) > NULL_SPIKE_THRESHOLD:
                findings.append(
                    f"Column '{col}': full-data null rate increased by "
                    f"{actual_increase:.1%} (source={source_null_pct:.1%} -> "
                    f"cache={cache_null_pct:.1%}), but the dry-run sample only "
                    f"predicted {expected_increase.get(col, 0.0):.1%} — possible "
                    f"unexpected nullification on real data (e.g. a TRY_CAST "
                    f"that fails on values not present in the sample)."
                )

        if findings:
            import json

            write_memory(
                db_session, project_id=project_id, domain="Audit_Log",
                topic=f"post_cold_start_quality_{table_name}",
                content=json.dumps(findings),
            )
    except Exception:
        # The quality check is best-effort and must never fail a completed
        # cold start.
        pass


# --------------------------------------------------------------------------
# Control-plane bookkeeping helpers
# --------------------------------------------------------------------------
def _upsert_sync_state(session: Session, project_id: str, table_name: str, sync_mode: str,
                       status: str, last_sync_utc: datetime | None = None,
                       last_row_count: int | None = None) -> None:
    existing = session.query(SyncState).filter_by(project_id=project_id, table_name=table_name).first()
    if existing:
        existing.sync_mode = sync_mode
        existing.status = status
        if last_sync_utc:
            existing.last_sync_utc = last_sync_utc
        if last_row_count is not None:
            existing.last_row_count = last_row_count
    else:
        session.add(SyncState(
            project_id=project_id, table_name=table_name, sync_mode=sync_mode, status=status,
            last_sync_utc=last_sync_utc, last_row_count=last_row_count,
        ))
    session.commit()


def _upsert_cold_start_progress(session: Session, project_id: str, table_name: str,
                                total_chunks: int) -> None:
    existing = session.query(ColdStartProgress).filter_by(
        project_id=project_id, table_name=table_name
    ).first()
    if existing:
        existing.total_chunks = total_chunks
        existing.chunks_done = 0
        existing.status = "in_progress"
    else:
        session.add(ColdStartProgress(
            project_id=project_id, table_name=table_name, total_chunks=total_chunks,
            chunks_done=0, status="in_progress",
        ))
    session.commit()


def _update_cold_start_progress(session: Session, project_id: str, table_name: str,
                                last_chunk_id: str, chunks_done: int) -> None:
    record = session.query(ColdStartProgress).filter_by(
        project_id=project_id, table_name=table_name
    ).first()
    if record:
        record.last_chunk_id = last_chunk_id
        record.chunks_done = chunks_done
        session.commit()


def _complete_cold_start_progress(session: Session, project_id: str, table_name: str) -> None:
    record = session.query(ColdStartProgress).filter_by(
        project_id=project_id, table_name=table_name
    ).first()
    if record:
        record.status = "completed"
        record.chunks_done = record.total_chunks or record.chunks_done
        session.commit()
