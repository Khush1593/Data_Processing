"""Shared test fixtures: build deliberately messy source databases.

Covers every issue type the sampler/script-generator must handle:
  * currency_string     -> amount      ("$1,234.50", "N/A")
  * percentage_string   -> discount    ("15%")
  * numeric_as_string   -> price_str   ("1,000")
  * mixed_date_format   -> order_date  ("2021-01-05", "Jan 5, 2021")
  * null_variant        -> region      ("N/A", "-", "")
plus created_at / updated_at (sync-mode signals) and an optional deleted_at.
"""
from __future__ import annotations

import sqlalchemy
from sqlalchemy import text

_CLEAN_REGIONS = ["North", "South", "East", "West"]
_NULL_VARIANTS = ["N/A", "-", "", "null", "none"]


def _rows(n: int, with_deleted: bool) -> list[dict]:
    rows = []
    for i in range(1, n + 1):
        # Scatter messy values deterministically.
        amount = f"${1000 + i:,}.50" if i % 7 else "N/A"
        discount = f"{(i % 30)}%" if i % 5 else "N/A"
        price_str = f"{(i * 13) % 100000:,}" if i % 6 else "-"
        order_date = "2021-01-05" if i % 2 else "Jan 5, 2021"
        region = _CLEAN_REGIONS[i % 4] if i % 9 else _NULL_VARIANTS[i % len(_NULL_VARIANTS)]
        row = {
            "id": i,
            "amount": amount,
            "discount": discount,
            "price_str": price_str,
            "order_date": order_date,
            "region": region,
            "created_at": "2021-01-01 00:00:00",
            "updated_at": "2021-06-01 12:00:00",
        }
        if with_deleted:
            row["deleted_at"] = "2021-07-01 00:00:00" if i % 10 == 0 else None
        rows.append(row)
    return rows


def build_source(db_uri: str, table: str = "sales", n_rows: int = 500,
                 with_updated: bool = True, with_deleted: bool = False) -> None:
    """Create ``table`` in the target DB (SQLite or Postgres) with messy data."""
    engine = sqlalchemy.create_engine(db_uri)
    cols = [
        "id INTEGER PRIMARY KEY",
        "amount VARCHAR(64)",
        "discount VARCHAR(32)",
        "price_str VARCHAR(64)",
        "order_date VARCHAR(64)",
        "region VARCHAR(64)",
        "created_at VARCHAR(64)",
    ]
    if with_updated:
        cols.append("updated_at VARCHAR(64)")
    if with_deleted:
        cols.append("deleted_at VARCHAR(64)")

    insert_cols = [c.split()[0] for c in cols]
    placeholders = ", ".join(f":{c}" for c in insert_cols)

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        conn.execute(text(f"CREATE TABLE {table} ({', '.join(cols)})"))
        rows = _rows(n_rows, with_deleted)
        for r in rows:
            payload = {k: r.get(k) for k in insert_cols}
            conn.execute(
                text(f"INSERT INTO {table} ({', '.join(insert_cols)}) VALUES ({placeholders})"),
                payload,
            )
    engine.dispose()
