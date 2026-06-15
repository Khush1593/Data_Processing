"""Stage 0 v3.0 — per-column deterministic DuckDB expression builder
(stage0_v3_spec.md §6).

No LLM, no network calls — always produces valid DuckDB SQL. Used directly
for CLEAN_DET columns, and as the guaranteed fallback when ``llm_resolver``
fails (or is never called) for CLEAN_AMBIG columns.

The per-issue SQL mirrors the magnitude-aware currency/percentage rules and
the column-wide date-format handling from the v2.0 LLM prompt and
deterministic cleaner (process.md §6), now emitted per-column instead of as
one monolithic SELECT.
"""
from __future__ import annotations

import re

from app.preprocessing.models import ColumnExpression, ColumnMetadata

# Issues are applied in this priority order: the first matching issue becomes
# the "primary" type-conversion expression; null_variant/needs_trim/
# inconsistent_casing are layered on top if also present.
ISSUE_PRIORITY = [
    "currency_string",
    "percentage_string",
    "mixed_date_format",
    "numeric_as_string",
    "inconsistent_boolean",
    "needs_trim",
    "null_variant",
    "inconsistent_casing",
]

_SENTINELS = ("n/a", "na", "null", "none", "-", "–", "—", "", "nan", "#n/a")

_NON_STRING_PRIMARIES = {
    "currency_string", "percentage_string", "mixed_date_format",
    "numeric_as_string", "inconsistent_boolean",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _q(col_name: str) -> str:
    """Double-quote a DuckDB identifier, escaping embedded quotes."""
    return '"' + col_name.replace('"', '""') + '"'


def _as_varchar(col_name: str) -> str:
    return f"CAST({_q(col_name)} AS VARCHAR)"


def _sql_str_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def _sentinel_is_null(expr: str) -> str:
    return f"LOWER(TRIM({expr})) IN ({_sql_str_list(_SENTINELS)})"


def _has_magnitude_suffix(values: list[str]) -> bool:
    """True if any sample value is a number with a trailing K/M/B (e.g. '£500K')."""
    return any(re.search(r"[0-9.][kKmMbB]\s*$", v) for v in values)


def _is_european_decimal(values: list[str]) -> bool:
    """True if values look like European format (comma = decimal separator,
    e.g. '1.234,56') more often than US/UK ('1,234.56')."""
    eu = sum(1 for v in values if re.search(r",\d{1,2}\s*$", v))
    us = sum(1 for v in values if re.search(r"\.\d{1,2}\s*$", v))
    return eu > us


def _has_parentheses_negative(values: list[str]) -> bool:
    """True if any value is an accounting-style negative, e.g. '($1,234.56)'."""
    return any("(" in v and ")" in v for v in values)


def _has_leading_zero_code(values: list[str]) -> bool:
    """True if any value is an all-digit string with a meaningful leading
    zero (e.g. a zero-padded SKU '0042') — must not be cast to a number."""
    return any(re.fullmatch(r"0\d+", v.strip()) for v in values)


# ---------------------------------------------------------------------------
# Per-issue expression builders
# ---------------------------------------------------------------------------

def _extract_number(expr: str, european: bool, magnitude: bool) -> str:
    """SQL expression that pulls a DOUBLE out of a messy money/number string."""
    if european:
        base = (
            f"TRY_CAST(REPLACE(REPLACE(REGEXP_REPLACE({expr}, '[^0-9.,\\-]', '', 'g'),"
            f" '.', ''), ',', '.') AS DOUBLE)"
        )
    else:
        base = f"TRY_CAST(REGEXP_REPLACE({expr}, '[^0-9.\\-]', '', 'g') AS DOUBLE)"
    if not magnitude:
        return base
    return (
        "CASE "
        f"WHEN UPPER(TRIM({expr})) SIMILAR TO '.*[0-9.]B' THEN {base} * 1e9 "
        f"WHEN UPPER(TRIM({expr})) SIMILAR TO '.*[0-9.]M' THEN {base} * 1e6 "
        f"WHEN UPPER(TRIM({expr})) SIMILAR TO '.*[0-9.]K' THEN {base} * 1e3 "
        f"ELSE {base} END"
    )


def _currency_expr(col: ColumnMetadata, expr: str) -> str:
    european = _is_european_decimal(col.sample_values)
    magnitude = _has_magnitude_suffix(col.sample_values)
    num = _extract_number(expr, european, magnitude)
    branches = [
        f"WHEN {_sentinel_is_null(expr)} THEN NULL",
        f"WHEN UPPER(TRIM({expr})) = 'FREE' THEN 0",
    ]
    if _has_parentheses_negative(col.sample_values):
        branches.append(f"WHEN {expr} SIMILAR TO '\\([^()]*\\)' THEN -1 * {num}")
    branches.append(f"ELSE {num}")
    return "CASE " + " ".join(branches) + " END"


def _percentage_expr(expr: str) -> str:
    """Magnitude-aware percentage normalisation to a consistent fraction.

    '2%' -> 0.02, '45' (no '%', > 1) -> 0.45, '0.01' (no '%', <= 1) -> 0.01.
    """
    num = f"TRY_CAST(REGEXP_REPLACE({expr}, '[^0-9.\\-]', '', 'g') AS DOUBLE)"
    return (
        "CASE "
        f"WHEN {_sentinel_is_null(expr)} THEN NULL "
        f"WHEN strpos({expr}, '%') > 0 THEN {num} / 100.0 "
        f"WHEN ABS({num}) > 1 THEN {num} / 100.0 "
        f"ELSE {num} "
        "END"
    )


def _date_expr(expr: str, date_format: str | None) -> str:
    branches = [f"TRY_CAST({expr} AS TIMESTAMP)"]
    if date_format:
        branches.append(f"TRY_STRPTIME({expr}, '{date_format}')")
    branches.append(f"TRY_STRPTIME({expr}, '%b %d %Y')")
    branches.append(f"TRY_STRPTIME({expr}, '%B %d %Y')")
    # Epoch seconds (1970..~2100) and Excel serial dates (~1954..2064), both
    # range-guarded so a stray id/year isn't silently turned into a fake date.
    branches.append(
        f"CASE WHEN TRY_CAST({expr} AS BIGINT) BETWEEN 0 AND 4102444800 "
        f"THEN TRY_CAST(TO_TIMESTAMP(TRY_CAST({expr} AS BIGINT)) AS TIMESTAMP) END"
    )
    branches.append(
        f"CASE WHEN TRY_CAST({expr} AS DOUBLE) BETWEEN 20000 AND 60000 "
        f"THEN DATE '1899-12-30' + TRY_CAST(TRY_CAST({expr} AS DOUBLE) AS INTEGER) "
        f"* INTERVAL '1 day' END"
    )
    return "COALESCE(\n        " + ",\n        ".join(branches) + "\n    )"


def _boolean_expr(expr: str) -> str:
    truthy = _sql_str_list(("y", "yes", "true", "1", "t"))
    falsy = _sql_str_list(("n", "no", "false", "0", "f"))
    return (
        "CASE "
        f"WHEN LOWER(TRIM({expr})) IN ({truthy}) THEN TRUE "
        f"WHEN LOWER(TRIM({expr})) IN ({falsy}) THEN FALSE "
        "ELSE NULL END"
    )


def _numeric_as_string_expr(col: ColumnMetadata, expr: str) -> str:
    if _has_leading_zero_code(col.sample_values):
        # Leading zeros are semantically meaningful (e.g. SKUs) — preserve as text.
        return expr
    return f"TRY_CAST(REGEXP_REPLACE({expr}, '[^0-9.\\-]', '', 'g') AS DOUBLE)"


def _null_variant_expr(expr: str) -> str:
    return f"CASE WHEN {_sentinel_is_null(expr)} THEN NULL ELSE {expr} END"


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def build_expression(col: ColumnMetadata, active_issues: list[str]) -> ColumnExpression:
    """Build a ``ColumnExpression`` for a CLEAN_DET column (or as a guaranteed
    fallback for a CLEAN_AMBIG column when the LLM is unavailable).

    One primary type-conversion expression is chosen by ``ISSUE_PRIORITY``,
    with ``null_variant`` / ``needs_trim`` / ``inconsistent_casing`` layered on
    top when also present and not already the primary.
    """
    issue_set = set(active_issues)
    primary = next((i for i in ISSUE_PRIORITY if i in issue_set), None)

    # Every transform operates on the value's textual form — TRIM / LOWER /
    # REGEXP_REPLACE / strpos all require VARCHAR in DuckDB. Cast up front so
    # the expression is type-safe even for a native BOOLEAN/numeric column
    # that was flagged with an issue (avoids `trim(BOOLEAN)` binder errors).
    base = _as_varchar(col.name)

    if primary == "currency_string":
        expr = _currency_expr(col, base)
    elif primary == "percentage_string":
        expr = _percentage_expr(base)
    elif primary == "mixed_date_format":
        expr = _date_expr(base, col.date_format)
    elif primary == "numeric_as_string":
        expr = _numeric_as_string_expr(col, base)
    elif primary == "inconsistent_boolean":
        expr = _boolean_expr(base)
    elif primary == "needs_trim":
        expr = f"TRIM({base})"
    elif primary == "inconsistent_casing":
        expr = f"LOWER(TRIM({base}))"
    elif primary == "null_variant":
        expr = base  # wrapped below
    else:
        expr = _q(col.name)  # no primary issue — passthrough

    if (
        "null_variant" in issue_set
        and primary != "null_variant"
        and primary not in _NON_STRING_PRIMARIES
    ):
        expr = _null_variant_expr(expr)

    if (
        "needs_trim" in issue_set
        and primary not in _NON_STRING_PRIMARIES | {"needs_trim", "inconsistent_casing"}
    ):
        expr = f"TRIM({expr})"

    return ColumnExpression(
        col_name=col.name,
        output_names=[col.name],
        sql_exprs=[expr],
        source="deterministic",
        issues_handled=sorted(issue_set),
    )


def build_passthrough(col: ColumnMetadata) -> ColumnExpression:
    """No cleaning needed — select the column as-is (SKIP and OBSERVE classes)."""
    return ColumnExpression(
        col_name=col.name,
        output_names=[col.name],
        sql_exprs=[_q(col.name)],
        source="passthrough",
        issues_handled=[],
    )
