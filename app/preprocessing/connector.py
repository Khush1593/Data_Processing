"""Stage 0 — DB connection + schema metadata extraction (process.md §4).

Multi-dialect by design: structure (columns, types, primary key, nullability)
is read through SQLAlchemy's dialect-agnostic Inspector, so the same code path
works for PostgreSQL *and* SQLite source databases. Per-column statistics
(null %, distinct count, row count) are computed with **safely quoted**
identifiers — never raw f-string interpolation of caller-supplied names.

Deterministic-first: every signal here is computed without an LLM. The sync
mode is inferred by the same rules as the spec.
"""
from __future__ import annotations

import sqlalchemy
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.preprocessing.models import ColumnMetadata, TableMetadata

CREATED_AT_PATTERNS = {"created_at", "createdat", "create_date", "insert_date", "inserted_at"}
UPDATED_AT_PATTERNS = {"updated_at", "updatedat", "update_date", "modified_at", "last_modified"}
DELETED_AT_PATTERNS = {"deleted_at", "deletedat", "delete_date", "is_deleted", "soft_delete"}

# Row-count threshold below which a small, change-trackless table is full-resynced.
FULL_RESYNC_ROW_LIMIT = 10_000


class TableNotFoundError(Exception):
    pass


def _q(engine: Engine, identifier: str) -> str:
    """Safely quote a SQL identifier for the engine's dialect."""
    return engine.dialect.identifier_preparer.quote(identifier)


def get_table_metadata(db_uri: str, table_name: str, schema: str | None = None) -> TableMetadata:
    engine = sqlalchemy.create_engine(db_uri)
    try:
        return _extract(engine, table_name, schema)
    finally:
        engine.dispose()


def _extract(engine: Engine, table_name: str, schema: str | None) -> TableMetadata:
    inspector = inspect(engine)

    # Validate the table exists — guards against typos and injection of
    # non-existent names before we ever interpolate the identifier.
    available = set(inspector.get_table_names(schema=schema))
    available |= set(inspector.get_view_names(schema=schema))
    if table_name not in available:
        raise TableNotFoundError(
            f"Table {table_name!r} not found. Available: {sorted(available)[:20]}"
        )

    col_defs = inspector.get_columns(table_name, schema=schema)  # portable
    try:
        pk_cols = inspector.get_pk_constraint(table_name, schema=schema).get(
            "constrained_columns", []
        ) or []
    except Exception:
        pk_cols = []
    primary_key_col = pk_cols[0] if pk_cols else None

    qtable = _q(engine, table_name)
    if schema:
        qtable = f"{_q(engine, schema)}.{qtable}"

    with engine.connect() as conn:
        row_count = conn.execute(text(f"SELECT COUNT(*) FROM {qtable}")).scalar() or 0

        null_pcts: dict[str, float] = {}
        if row_count > 0 and col_defs:
            # Single full-table-scan query computing every column's null
            # count, instead of one scan per column (N scans on an N-column
            # table). Deliberately does NOT compute COUNT(DISTINCT ...) — a
            # hash-aggregate per column over the full table is exactly the
            # kind of heavy analytics this connector must not impose on the
            # client's live DB. distinct_count is instead estimated locally
            # from the in-memory sample (see sampler.extract_stratified_sample).
            select_parts = []
            for cd in col_defs:
                qcol = _q(engine, cd["name"])
                select_parts.append(f"SUM(CASE WHEN {qcol} IS NULL THEN 1 ELSE 0 END)")
            agg_query = f"SELECT {', '.join(select_parts)} FROM {qtable}"
            row = conn.execute(text(agg_query)).fetchone()
            for i, cd in enumerate(col_defs):
                col = cd["name"]
                null_count = row[i] or 0
                null_pcts[col] = float(null_count) / row_count
        else:
            for cd in col_defs:
                null_pcts[cd["name"]] = 0.0

    columns: list[ColumnMetadata] = []
    change_tracking_col: str | None = None
    has_updated_at = False
    has_deleted_at = False

    for cd in col_defs:
        col_name = cd["name"]
        data_type = str(cd["type"])
        col_lower = col_name.lower()

        is_created = col_lower in CREATED_AT_PATTERNS
        is_updated = col_lower in UPDATED_AT_PATTERNS
        is_deleted = col_lower in DELETED_AT_PATTERNS

        if is_updated:
            has_updated_at = True
            change_tracking_col = col_name
        if is_deleted:
            has_deleted_at = True

        columns.append(
            ColumnMetadata(
                name=col_name,
                declared_type=data_type,
                null_pct=float(null_pcts.get(col_name, 0.0)),
                distinct_count=0,  # filled in locally from the sample (see sampler)
                is_primary_key=(col_name == primary_key_col),
                has_created_at=is_created,
                has_updated_at=is_updated,
                has_deleted_at=is_deleted,
            )
        )

    # Sync-mode inference (identical rules to process.md §4).
    if has_deleted_at:
        sync_mode = "delete_aware"
    elif primary_key_col and has_updated_at:
        sync_mode = "upsert"
    elif row_count < FULL_RESYNC_ROW_LIMIT and not has_updated_at:
        sync_mode = "full_resync"
    else:
        sync_mode = "append_only"

    return TableMetadata(
        table_name=table_name,
        row_count=int(row_count),
        columns=columns,
        detected_sync_mode=sync_mode,
        primary_key_column=primary_key_col,
        change_tracking_column=change_tracking_col,
        source_schema=schema,
    )
