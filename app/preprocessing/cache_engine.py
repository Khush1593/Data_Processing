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
from app.models import ColdStartProgress, SyncState
from app.preprocessing.models import CleaningScript, TableMetadata

_settings = get_settings()
CHUNK_SIZE = _settings.PREPROCESSING_CHUNK_SIZE
RECONCILIATION_THRESHOLD = _settings.PREPROCESSING_RECONCILIATION_THRESHOLD

INTEGER_TYPE_HINTS = {"int", "integer", "bigint", "smallint"}


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
) -> dict:
    table_name = metadata.table_name
    duckdb_path = get_project_duckdb_path(project_id)

    _upsert_sync_state(db_session, project_id, table_name, metadata.detected_sync_mode, "in_progress")

    source_engine = sqlalchemy.create_engine(db_uri)
    qsrc = source_engine.dialect.identifier_preparer.quote(table_name)
    if metadata.source_schema:
        qsrc = f"{source_engine.dialect.identifier_preparer.quote(metadata.source_schema)}.{qsrc}"
    duck_con = duckdb.connect(duckdb_path)

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

        # Staging is created from the FIRST non-empty cleaned chunk so column
        # types come from real data (a 0-row frame loses pandas/DuckDB types).
        duck_con.execute(f"DROP TABLE IF EXISTS {_qd(staging)}")
        state = {"created": False}

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
            offset = 0
            while True:
                with source_engine.connect() as conn:
                    chunk_df = pd.read_sql(
                        sqlalchemy.text(f"SELECT * FROM {qsrc} LIMIT :lim OFFSET :off"),
                        conn,
                        params={"lim": CHUNK_SIZE, "off": offset},
                    )
                if chunk_df.empty:
                    break
                _append_chunk(duck_con, staging, chunk_df, table_name, script, state)
                chunks_done += 1
                offset += CHUNK_SIZE
                _update_cold_start_progress(db_session, project_id, table_name, str(offset), chunks_done)

        # Empty source table: create an empty staging from the output schema so
        # the live table + view still exist for Stage 1.
        if not state["created"]:
            with source_engine.connect() as conn:
                schema_df = pd.read_sql(sqlalchemy.text(f"SELECT * FROM {qsrc} LIMIT 0"), conn)
            empty_clean = _clean_chunk(schema_df, table_name, script)
            duck_con.register("empty_clean", empty_clean)
            duck_con.execute(f"CREATE TABLE {_qd(staging)} AS SELECT * FROM empty_clean")
            duck_con.unregister("empty_clean")

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
        if not state["created"]:
            # First non-empty chunk defines the staging schema (real types).
            duck_con.execute(f"CREATE TABLE {_qd(staging)} AS SELECT * FROM chunk_data")
            state["created"] = True
        else:
            duck_con.execute(f"INSERT INTO {_qd(staging)} SELECT * FROM chunk_data")
    finally:
        duck_con.unregister("chunk_data")


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
