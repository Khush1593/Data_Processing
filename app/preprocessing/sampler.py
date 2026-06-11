"""Stage 0 — in-memory stratified sampling + issue detection (process.md §5).

Purpose: pull a *naive* chunk from the client DB (cheap, no ORDER BY), then
stratify it locally to surface edge cases — nulls and numeric boundaries — that
the cleaning-SQL generator needs to see. The live DB is never asked to do heavy
analytics.

The stratified sample is held only in memory by the caller and is discarded
once the cleaning SQL is locked (Guiding Principle, process.md §0).
"""
from __future__ import annotations

import re

import pandas as pd
import sqlalchemy

from app.config import get_settings
from app.preprocessing.models import ColumnMetadata, TableMetadata

_settings = get_settings()
SAMPLE_TARGET_ROWS = _settings.PREPROCESSING_SAMPLE_SIZE
NAIVE_CHUNK_SIZE = _settings.PREPROCESSING_NAIVE_CHUNK_SIZE

NUMERIC_TYPE_HINTS = {
    "int", "integer", "bigint", "smallint", "numeric", "decimal",
    "float", "real", "double", "double precision",
}
STRING_TYPE_HINTS = {"varchar", "text", "character varying", "char", "string"}
NULL_VARIANTS = {"n/a", "na", "null", "none", "-", "–", "—", "", "nan", "#n/a"}
CURRENCY_SYMBOL_RE = re.compile(r"[$€£₹¥]")


def _q(engine, ident: str) -> str:
    return engine.dialect.identifier_preparer.quote(ident)


def _type_matches(declared: str, hints: set[str]) -> bool:
    d = declared.lower()
    return any(h in d for h in hints)


def extract_stratified_sample(db_uri: str, metadata: TableMetadata) -> pd.DataFrame:
    """Pull a naive chunk locally, then stratify to find edge cases.

    Protects the client DB from heavy ORDER BY analytics.
    """
    engine = sqlalchemy.create_engine(db_uri)
    table = metadata.table_name
    qtable = _q(engine, table)
    if metadata.source_schema:
        qtable = f"{_q(engine, metadata.source_schema)}.{qtable}"

    try:
        with engine.connect() as conn:
            try:
                raw_chunk = pd.read_sql(
                    sqlalchemy.text(f"SELECT * FROM {qtable} LIMIT {NAIVE_CHUNK_SIZE}"),
                    conn,
                )
            except Exception:
                return pd.DataFrame()
    finally:
        engine.dispose()

    if raw_chunk.empty:
        return raw_chunk

    frames: list[pd.DataFrame] = []

    # 1. Random baseline (up to 400 rows).
    frames.append(raw_chunk.sample(n=min(400, len(raw_chunk)), random_state=42))

    # 2. Null-revealing rows (locally).
    for col in metadata.columns:
        if col.name in raw_chunk.columns and col.null_pct > 0:
            nulls = raw_chunk[raw_chunk[col.name].isna()]
            if not nulls.empty:
                frames.append(nulls.head(10))

    # 3. Numeric boundary rows (locally; coerce to find min/max safely).
    for col in metadata.columns:
        if col.name in raw_chunk.columns and _type_matches(col.declared_type, NUMERIC_TYPE_HINTS):
            try:
                temp_series = pd.to_numeric(raw_chunk[col.name], errors="coerce")
                valid_idx = temp_series.dropna().index
                if not valid_idx.empty:
                    sorted_idx = temp_series.loc[valid_idx].sort_values()
                    frames.append(raw_chunk.loc[sorted_idx.head(20).index])
                    frames.append(raw_chunk.loc[sorted_idx.tail(20).index])
            except Exception:
                pass

    combined = pd.concat(frames, ignore_index=True).drop_duplicates()
    return combined.head(SAMPLE_TARGET_ROWS)


def detect_currency_symbols(col_series: pd.Series) -> list[str]:
    """Returns the sorted set of distinct currency symbols ($€£₹¥) found in a
    string column's values, used to flag mixed-currency ambiguity."""
    found: set[str] = set()
    for sym in CURRENCY_SYMBOL_RE.findall("".join(col_series)):
        found.add(sym)
    return sorted(found)


def detect_column_issues(sample: pd.DataFrame, metadata_col: ColumnMetadata) -> str | None:
    """Deterministic, LLM-free heuristic that labels a column's cleaning issue.

    Returns one of: currency_string | percentage_string | null_variant |
    mixed_date_format | numeric_as_string | None.
    """
    if sample.empty or metadata_col.name not in sample.columns:
        return None

    col_series = sample[metadata_col.name].dropna().astype(str).str.strip()
    if len(col_series) == 0:
        return None

    # Currency symbols anywhere in a meaningful share of values.
    if col_series.str.contains(r"[$€£₹¥]", regex=True).mean() > 0.3:
        return "currency_string"

    # Trailing percent sign.
    if col_series.str.contains(r"%$", regex=True).mean() > 0.3:
        return "percentage_string"

    # Null-variant sentinels showing up at a small but real rate.
    if col_series.str.lower().isin(NULL_VARIANTS).mean() > 0.05:
        return "null_variant"

    # String-typed columns that are really dates or numbers.
    if _type_matches(metadata_col.declared_type, STRING_TYPE_HINTS):
        date_like = col_series.str.match(
            r"\d{1,4}[-/\.]\d{1,2}[-/\.]\d{1,4}|\w+ \d{1,2},? \d{4}",
            na=False,
        )
        if date_like.mean() > 0.5:
            return "mixed_date_format"

        numeric_like = pd.to_numeric(
            col_series.str.replace(r"[,$€£₹¥%]", "", regex=True),
            errors="coerce",
        ).notna()
        if numeric_like.mean() > 0.85:
            return "numeric_as_string"

    return None
