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
# Currency symbol -> ISO 4217 code, covering the world's most commonly used
# currency symbols (not just the handful that happen to appear in any one
# dataset) so detection/labelling generalises across datasets.
CURRENCY_SYMBOL_TO_CODE = {
    "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR",
    "₩": "KRW", "₽": "RUB", "₪": "ILS", "₫": "VND", "₴": "UAH",
    "₦": "NGN", "₱": "PHP", "฿": "THB", "₲": "PYG", "₡": "CRC",
    "₵": "GHS", "₸": "KZT", "₮": "MNT", "₭": "LAK", "₼": "AZN",
    "₾": "GEL", "₺": "TRY",
}
_CURRENCY_SYMBOL_CHARS = "".join(CURRENCY_SYMBOL_TO_CODE)
CURRENCY_SYMBOL_RE = re.compile(f"[{re.escape(_CURRENCY_SYMBOL_CHARS)}]")


def _q(engine, ident: str) -> str:
    return engine.dialect.identifier_preparer.quote(ident)


def _type_matches(declared: str, hints: set[str]) -> bool:
    d = declared.lower()
    return any(h in d for h in hints)


def extract_stratified_sample(db_uri: str, metadata: TableMetadata) -> pd.DataFrame:
    """Pull a naive chunk locally, then stratify to find edge cases.

    Protects the client DB from heavy ORDER BY analytics.

    Every column is pulled into the sample so that ``dry_run`` can execute the
    full assembled SELECT (which includes passthrough PII/IDENTIFIER columns)
    against it. Privacy isolation for PII columns is enforced one step later,
    in :func:`app.preprocessing.profiler.enrich_metadata_with_sample` — only
    candidate (non-PII/IDENTIFIER/STRUCTURAL) columns get ``sample_values`` /
    ``inferred_issues`` populated, so PII values never reach the LLM resolver
    even though they are present in this in-memory DataFrame.
    """
    engine = sqlalchemy.create_engine(db_uri)
    table = metadata.table_name
    qtable = _q(engine, table)
    if metadata.source_schema:
        qtable = f"{_q(engine, metadata.source_schema)}.{qtable}"

    col_sql = "*"

    raw_chunk = pd.DataFrame()
    try:
        if engine.dialect.name == "postgresql":
            # TABLESAMPLE SYSTEM scans random disk pages instead of always
            # returning the physically-first N rows, so the naive chunk
            # isn't blind to anomalies clustered later in the table (e.g.
            # all bad data loaded in a later batch). Run it on its own
            # connection so a failure (unsupported on a view, etc.) can't
            # poison the plain-LIMIT fallback below.
            try:
                with engine.connect() as conn:
                    raw_chunk = pd.read_sql(
                        sqlalchemy.text(
                            f"SELECT {col_sql} FROM {qtable} TABLESAMPLE SYSTEM (1) "
                            f"LIMIT {NAIVE_CHUNK_SIZE}"
                        ),
                        conn,
                    )
            except Exception:
                raw_chunk = pd.DataFrame()

        if raw_chunk.empty:
            try:
                with engine.connect() as conn:
                    raw_chunk = pd.read_sql(
                        sqlalchemy.text(f"SELECT {col_sql} FROM {qtable} LIMIT {NAIVE_CHUNK_SIZE}"),
                        conn,
                    )
            except Exception:
                return pd.DataFrame()
    finally:
        engine.dispose()

    if raw_chunk.empty:
        return raw_chunk

    # distinct_count is estimated locally from the naive chunk (in-memory,
    # pandas-side) rather than via COUNT(DISTINCT ...) on the live DB, which
    # would force a per-column hash-aggregate over the full table. This is an
    # estimate on up to NAIVE_CHUNK_SIZE rows, not an exact full-table count.
    for col in metadata.columns:
        if col.name in raw_chunk.columns:
            try:
                nunique = raw_chunk[col.name].nunique(dropna=True)
            except TypeError:
                # nunique() hashes values; JSON/JSONB columns come back from
                # psycopg2 as native dict/list objects, which aren't
                # hashable. Fall back to counting distinct stringified
                # values for these structural columns.
                nunique = raw_chunk[col.name].dropna().map(repr).nunique()
            col.distinct_count = int(nunique)
            col.distinct_sample_ratio = col.distinct_count / len(raw_chunk)

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

    # 4. PK-range endpoints: rows from both ends of the table's primary-key
    # ordering. Cheap (indexed ORDER BY + LIMIT, no full scan) and surfaces
    # anomalies clustered at the start/end of the table (e.g. a bad batch
    # loaded most-recently) that a naive LIMIT chunk or random sample of the
    # first NAIVE_CHUNK_SIZE rows would never see — useful for non-Postgres
    # dialects too, where TABLESAMPLE isn't available at all.
    if metadata.primary_key_column:
        pk = _q(engine, metadata.primary_key_column)
        try:
            engine2 = sqlalchemy.create_engine(db_uri)
            try:
                with engine2.connect() as conn:
                    head = pd.read_sql(
                        sqlalchemy.text(
                            f"SELECT {col_sql} FROM {qtable} ORDER BY {pk} ASC LIMIT 20"
                        ),
                        conn,
                    )
                    tail = pd.read_sql(
                        sqlalchemy.text(
                            f"SELECT {col_sql} FROM {qtable} ORDER BY {pk} DESC LIMIT 20"
                        ),
                        conn,
                    )
                if not head.empty:
                    frames.append(head)
                if not tail.empty:
                    frames.append(tail)
            finally:
                engine2.dispose()
        except Exception:
            pass

    combined = pd.concat(frames, ignore_index=True)
    combined = _drop_duplicates_safe(combined)
    return combined.head(SAMPLE_TARGET_ROWS)


def _drop_duplicates_safe(df: pd.DataFrame) -> pd.DataFrame:
    """``DataFrame.drop_duplicates`` raises ``TypeError: unhashable type:
    'dict'`` if any column holds JSON/JSONB values (psycopg2 returns these as
    native Python ``dict``/``list`` objects). Fall back to deduplicating on a
    stringified copy of those columns, while keeping the original values in
    the returned frame."""
    try:
        return df.drop_duplicates()
    except TypeError:
        key = df.apply(lambda col: col.map(
            lambda v: repr(v) if isinstance(v, (dict, list)) else v
        ))
        return df[~key.duplicated()]


_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]")


def select_diverse_sample_values(values: list[str], n: int) -> list[str]:
    """Pick a representative slice of ``n`` unique values to show the LLM,
    rather than just the first ``n`` (which hides anomalies/edge cases that
    happen to sort later in the column, e.g. one row with a stray currency
    symbol or an unusually long free-text value).

    Always includes (when available): the longest value, the shortest
    value, and values containing non-alphanumeric characters (symbols,
    punctuation, whitespace) — these are exactly the values most likely to
    reveal a cleaning issue. The remainder is filled from the front of the
    list in original order.
    """
    if len(values) <= n:
        return list(values)

    picked: list[str] = []
    seen: set[str] = set()

    def _add(v: str) -> None:
        if v not in seen and len(picked) < n:
            picked.append(v)
            seen.add(v)

    by_length = sorted(values, key=len)
    _add(by_length[-1])  # longest
    _add(by_length[0])  # shortest

    for v in values:
        if len(picked) >= n:
            break
        if _NON_ALNUM_RE.search(v):
            _add(v)

    for v in values:
        if len(picked) >= n:
            break
        _add(v)

    return picked


def detect_currency_symbols(col_series: pd.Series) -> list[str]:
    """Returns the sorted set of distinct currency symbols (see
    CURRENCY_SYMBOL_TO_CODE) found in a string column's values, used to flag
    mixed-currency ambiguity."""
    found: set[str] = set()
    for sym in CURRENCY_SYMBOL_RE.findall("".join(col_series)):
        found.add(sym)
    return sorted(found)


_DATE_LIKE_RE = r"\d{1,4}[-/\.]\d{1,2}[-/\.]\d{1,4}|\w+ \d{1,2},? \d{4}"

# Boolean-like tokens (case/whitespace-insensitive). A string column whose
# values are ENTIRELY drawn from this set is a boolean stored as text, often
# with inconsistent encodings ('Y'/'yes'/'1'/'true' all meaning TRUE).
_BOOLEAN_TRUTHY = {"y", "yes", "true", "1", "t"}
_BOOLEAN_FALSY = {"n", "no", "false", "0", "f"}
_BOOLEAN_TOKENS = _BOOLEAN_TRUTHY | _BOOLEAN_FALSY

# inconsistent_casing is restricted to genuinely categorical columns
# (low cardinality, e.g. status/region codes) — lowercasing a free-text
# column with proper nouns (names, cities, product titles) would destroy
# meaningful data.
_CASING_DISTINCT_LIMIT = 20

# detect_column_issues has moved from threshold-based ("> 0.3 of rows") to
# presence-based ("any occurrence") detection for currency/percentage/null
# sentinels/whitespace/casing/booleans — a single dirty row in a million is
# still a dirty row and must not be silently ignored. Only the two ratio-based
# checks below remain, with a low 0.1 bar (date-vs-numeric strings are
# inherently ambiguous and a hard "any occurrence" rule would misfire on
# columns containing the occasional date-shaped or numeric-shaped string).
_DATE_LIKE_THRESHOLD = 0.1
_NUMERIC_LIKE_THRESHOLD = 0.1


def compute_issue_ratios(sample: pd.DataFrame, metadata_col: ColumnMetadata) -> dict[str, float]:
    """Compute the raw 0-1 ratios/flags behind each heuristic in
    ``detect_column_issues``. Surfaced in the LLM prompt for context even when
    the corresponding issue wasn't triggered (e.g. a low but non-zero
    currency_symbol_ratio)."""
    if sample.empty or metadata_col.name not in sample.columns:
        return {}

    raw_series = sample[metadata_col.name].dropna().astype(str)
    if len(raw_series) == 0:
        return {}
    col_series = raw_series.str.strip()

    has_percent_mask = col_series.str.contains(r"%$", regex=True)

    ratios: dict[str, float] = {
        "currency_symbol_ratio": float(col_series.str.contains(CURRENCY_SYMBOL_RE).mean()),
        "percent_sign_ratio": float(has_percent_mask.mean()),
        "null_variant_ratio": float(col_series.str.lower().isin(NULL_VARIANTS).mean()),
        "needs_trim_ratio": float((raw_series != col_series).mean()),
    }

    lowered_nunique = col_series.str.lower().nunique()
    ratios["inconsistent_casing"] = (
        1.0
        if (
            metadata_col.distinct_count < _CASING_DISTINCT_LIMIT
            and col_series.nunique() > lowered_nunique
        )
        else 0.0
    )

    # `inconsistent_boolean` targets booleans stored as TEXT with mixed
    # encodings ('Y'/'yes'/'1'/'true'). A column already declared as a native
    # BOOLEAN (or any non-string type) is already clean — flagging it would
    # make the cleaner wrap it in LOWER(TRIM(...)), which DuckDB rejects with a
    # binder error (`trim(BOOLEAN)`) when run against the real boolean column.
    # So require a string declared type here.
    is_string_type = _type_matches(metadata_col.declared_type, STRING_TYPE_HINTS)
    lowered = col_series.str.lower()
    ratios["inconsistent_boolean"] = (
        1.0
        if (
            is_string_type
            and metadata_col.distinct_count <= 2
            and lowered.isin(_BOOLEAN_TOKENS).all()
            and lowered.isin(_BOOLEAN_TRUTHY).any()
            and lowered.isin(_BOOLEAN_FALSY).any()
        )
        else 0.0
    )

    # A column with SOME '%'-suffixed values and SOME bare numeric values is
    # ambiguous (e.g. '2%' next to '0.02' — is the bare value 2% or 0.02%?)
    # regardless of how rare either form is, so this is tracked separately
    # from percent_sign_ratio.
    if has_percent_mask.any() and not has_percent_mask.all():
        non_percent = col_series[~has_percent_mask]
        numeric_non_percent = pd.to_numeric(non_percent, errors="coerce")
        ratios["mixed_percent_format_ratio"] = (
            1.0 if numeric_non_percent.notna().any() else 0.0
        )
    else:
        ratios["mixed_percent_format_ratio"] = 0.0

    if _type_matches(metadata_col.declared_type, STRING_TYPE_HINTS):
        ratios["date_like_ratio"] = float(
            col_series.str.match(_DATE_LIKE_RE, na=False).mean()
        )
        _numeric_strip_re = re.compile(f"[,%{re.escape(_CURRENCY_SYMBOL_CHARS)}]")
        ratios["numeric_like_ratio"] = float(
            pd.to_numeric(
                col_series.str.replace(_numeric_strip_re, "", regex=True), errors="coerce"
            ).notna().mean()
        )

    return ratios


def detect_column_issues(
    sample: pd.DataFrame,
    metadata_col: ColumnMetadata,
    ratios: dict[str, float] | None = None,
) -> list[str]:
    """Deterministic, LLM-free heuristic that labels a column's cleaning issue(s).

    Presence-based ("any occurrence"), not threshold-based: a single dirty
    row in a million still needs cleaning, and a million-row pipeline must not
    crash on it. A column can genuinely have multiple issues (e.g. it needs a
    trim AND has currency strings), so this returns ALL matching issues rather
    than stopping at the first one.

    Each entry is one of: currency_string | percentage_string | null_variant |
    needs_trim | inconsistent_casing | inconsistent_boolean |
    mixed_date_format | numeric_as_string.

    ``ratios`` may be passed in (from a prior ``compute_issue_ratios`` call on
    the same sample/column) to avoid recomputing them.
    """
    if sample.empty or metadata_col.name not in sample.columns:
        return []

    raw_series = sample[metadata_col.name].dropna().astype(str)
    if len(raw_series) == 0:
        return []
    col_series = raw_series.str.strip()

    if ratios is None:
        ratios = compute_issue_ratios(sample, metadata_col)
    issues: list[str] = []

    if col_series.str.contains(CURRENCY_SYMBOL_RE).any():
        issues.append("currency_string")

    if (
        col_series.str.contains(r"%$", regex=True).any()
        or ratios.get("mixed_percent_format_ratio", 0.0) > 0.0
    ):
        issues.append("percentage_string")

    if col_series.str.lower().isin(NULL_VARIANTS).any():
        issues.append("null_variant")

    if (raw_series != col_series).any():
        issues.append("needs_trim")

    if ratios.get("inconsistent_casing", 0.0) > 0.0:
        issues.append("inconsistent_casing")

    if ratios.get("inconsistent_boolean", 0.0) > 0.0:
        issues.append("inconsistent_boolean")

    if "date_like_ratio" in ratios:
        if ratios["date_like_ratio"] > _DATE_LIKE_THRESHOLD:
            issues.append("mixed_date_format")
        elif ratios.get("numeric_like_ratio", 0.0) > _NUMERIC_LIKE_THRESHOLD:
            issues.append("numeric_as_string")

    return issues


# Day-first date patterns: DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY (2- or 4-digit year).
_SLASH_DATE_RE = re.compile(r"^(\d{1,2})([-/.])(\d{1,2})[-/.](\d{2,4})$")


def detect_date_format(sample_col: pd.Series) -> str | None:
    """Inspect an entire column's slash/dash-separated date values and decide,
    column-wide, whether they are day-first (DD/MM/YYYY) or month-first
    (MM/DD/YYYY).

    Per-row format guessing (e.g. COALESCE(TRY_STRPTIME(..., '%d/%m/%Y'),
    TRY_STRPTIME(..., '%m/%d/%Y'), ...)) is ambiguous and silently swaps day
    and month for rows where both interpretations are valid (e.g.
    '04/05/2023'). Instead, scan every value: if ANY value has a first
    component > 12, the column must be day-first; if ANY value has a second
    component > 12, it must be month-first. Returns a single strptime format
    string to apply to the whole column, or ``None`` if the column doesn't
    look like slash/dash dates at all.
    """
    col_series = sample_col.dropna().astype(str).str.strip()
    if len(col_series) == 0:
        return None

    matches = col_series.str.extract(_SLASH_DATE_RE)
    matches = matches.dropna()
    if len(matches) / len(col_series) < 0.5:
        return None

    first = pd.to_numeric(matches[0], errors="coerce")
    second = pd.to_numeric(matches[2], errors="coerce")
    # Use the separator from the actual data — TRY_STRPTIME format strings
    # are literal, so '%d/%m/%Y' will never match a '-'-separated value.
    sep = matches[1].mode().iat[0]

    day_first_evidence = (first > 12).any()
    month_first_evidence = (second > 12).any()

    if day_first_evidence and month_first_evidence:
        # Contradictory evidence within one column — can't be a single
        # consistent format; let the caller fall back to per-row COALESCE.
        return None
    if day_first_evidence:
        return f"%d{sep}%m{sep}%Y"
    if month_first_evidence:
        return f"%m{sep}%d{sep}%Y"
    # Ambiguous (every value <= 12 in both positions): default to day-first,
    # the more common convention outside the US.
    return f"%d{sep}%m{sep}%Y"
