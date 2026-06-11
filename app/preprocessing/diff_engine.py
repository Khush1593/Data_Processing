"""Stage 0 review UI — full-table "before vs after" diff (changed rows only).

Computes the diff entirely inside DuckDB: the source table is ATTACHed
read-only via the ``postgres``/``sqlite`` scanner extensions alongside the
already-built ``clean_cache_<table>`` view (see cache_engine.run_cold_start),
and a single SQL query joins + compares the two row-for-row.

Only rows where at least one column differs (after casting both sides to
VARCHAR) are returned, paginated.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import duckdb

from app.preprocessing.models import TableMetadata


class UnsupportedSourceError(Exception):
    pass


def _qd(ident: str) -> str:
    """Quote a DuckDB identifier."""
    return '"' + ident.replace('"', '""') + '"'


def _attach_source(con: duckdb.DuckDBPyConnection, db_uri: str, alias: str) -> tuple[str, str]:
    """ATTACH the source DB read-only as ``alias``. Returns (db_type, schema_prefix)."""
    parsed = urlparse(db_uri)
    scheme = parsed.scheme.split("+")[0]

    if scheme == "postgresql":
        conn_parts = []
        if parsed.path and parsed.path != "/":
            conn_parts.append(f"dbname={parsed.path.lstrip('/')}")
        if parsed.hostname:
            conn_parts.append(f"host={parsed.hostname}")
        if parsed.port:
            conn_parts.append(f"port={parsed.port}")
        if parsed.username:
            conn_parts.append(f"user={parsed.username}")
        if parsed.password:
            conn_parts.append(f"password={parsed.password}")
        connstr = " ".join(conn_parts)

        con.execute("INSTALL postgres")
        con.execute("LOAD postgres")
        con.execute(f"ATTACH '{connstr}' AS {alias} (TYPE POSTGRES, READ_ONLY)")
        return "postgres", alias
    elif scheme == "sqlite":
        path = db_uri.split("///")[-1]
        con.execute("INSTALL sqlite")
        con.execute("LOAD sqlite")
        con.execute(f"ATTACH '{path}' AS {alias} (TYPE SQLITE, READ_ONLY)")
        return "sqlite", alias
    else:
        raise UnsupportedSourceError(f"Unsupported source DB scheme: {scheme!r}")


def _source_table_ref(alias: str, table_name: str, schema: str | None, db_type: str) -> str:
    if db_type == "postgres" and schema:
        return f"{alias}.{_qd(schema)}.{_qd(table_name)}"
    return f"{alias}.{_qd(table_name)}"


_PY_SCALAR = (str, int, float, bool, type(None))


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, _PY_SCALAR):
        return value
    # pandas/duckdb Timestamp, date, Decimal, etc.
    if value is None:
        return None
    try:
        import pandas as pd
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def get_diff_page(
    duckdb_path: str,
    db_uri: str,
    metadata: TableMetadata,
    table_name: str,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Return a page of changed rows comparing the source table to the
    cleaned ``clean_cache_<table_name>`` view.

    Returns:
        {
          "table": table_name,
          "page": page,
          "page_size": page_size,
          "total_changed": int,
          "columns": [col_name, ...],
          "rows": [
            {"key": <pk value or row index>,
             "before": {col: val, ...},
             "after": {col: val, ...},
             "changed_columns": [col, ...]},
            ...
          ],
        }
    """
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 50

    con = duckdb.connect(duckdb_path)
    alias = "src"
    columns = [c.name for c in metadata.columns]
    cache_view = f"clean_cache_{table_name}"

    try:
        db_type, _ = _attach_source(con, db_uri, alias)
        src_ref = _source_table_ref(alias, table_name, metadata.source_schema, db_type)
        cache_ref = _qd(cache_view)

        pk = metadata.primary_key_column
        if pk and pk in columns:
            join_key_src = f"s.{_qd(pk)}"
            join_key_cache = f"c.{_qd(pk)}"
            order_by = "key_col"
            key_select = f"{join_key_src} AS key_col"
        else:
            # No reliable PK: synthesize positional row numbers on both sides.
            # Valid only while the source table is unchanged since cold start
            # (true for this single-session review flow).
            join_key_src = "s.__rownum"
            join_key_cache = "c.__rownum"
            order_by = "key_col"
            key_select = "s.__rownum AS key_col"

        diff_predicates = " OR ".join(
            f"CAST(s.{_qd(col)} AS VARCHAR) IS DISTINCT FROM CAST(c.{_qd(col)} AS VARCHAR)"
            for col in columns
        )

        before_select = ", ".join(f"s.{_qd(col)} AS {_qd('before__' + col)}" for col in columns)
        after_select = ", ".join(f"c.{_qd(col)} AS {_qd('after__' + col)}" for col in columns)
        changed_flags = ", ".join(
            f"(CAST(s.{_qd(col)} AS VARCHAR) IS DISTINCT FROM CAST(c.{_qd(col)} AS VARCHAR)) AS {_qd('chg__' + col)}"
            for col in columns
        )

        if pk and pk in columns:
            src_cte = f"SELECT * FROM {src_ref}"
            cache_cte = f"SELECT * FROM {cache_ref}"
        else:
            src_cte = f"SELECT *, ROW_NUMBER() OVER () - 1 AS __rownum FROM {src_ref}"
            cache_cte = f"SELECT *, ROW_NUMBER() OVER () - 1 AS __rownum FROM {cache_ref}"

        base_query = f"""
        WITH src_t AS ({src_cte}),
             cache_t AS ({cache_cte}),
             joined AS (
                 SELECT {key_select}, {before_select}, {after_select}, {changed_flags}
                 FROM src_t s
                 JOIN cache_t c ON {join_key_src} = {join_key_cache}
                 WHERE {diff_predicates}
             )
        """

        total_changed = con.execute(
            base_query + " SELECT COUNT(*) FROM joined"
        ).fetchone()[0]

        offset = (page - 1) * page_size
        page_rows = con.execute(
            base_query
            + f" SELECT * FROM joined ORDER BY {order_by} LIMIT {page_size} OFFSET {offset}"
        ).fetchdf()

        rows: list[dict] = []
        for _, r in page_rows.iterrows():
            before = {col: _to_jsonable(r[f"before__{col}"]) for col in columns}
            after = {col: _to_jsonable(r[f"after__{col}"]) for col in columns}
            changed_columns = [col for col in columns if bool(r[f"chg__{col}"])]
            rows.append(
                {
                    "key": _to_jsonable(r["key_col"]),
                    "before": before,
                    "after": after,
                    "changed_columns": changed_columns,
                }
            )

        return {
            "table": table_name,
            "page": page,
            "page_size": page_size,
            "total_changed": int(total_changed),
            "columns": columns,
            "rows": rows,
        }
    finally:
        try:
            con.execute(f"DETACH {alias}")
        except Exception:
            pass
        con.close()
