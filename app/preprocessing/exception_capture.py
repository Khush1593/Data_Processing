"""Stage 0 v3.1 — Step 4: Self-Healing Exception Capture.

For each OBSERVE column in the DuckDB in-memory sample:

  4.1  Determine the target type (DOUBLE / TIMESTAMP / BOOLEAN).
       Columns already natively typed → passthrough immediately.
       If no target type can be determined → passthrough + review note.

  4.2  Run the exception capture query against the in-memory sample.

  4.3  Evaluate the result:
         0 rows  → clean; deterministic TRY_CAST expression.
         1-20    → proceed to AI patching.
         < 10 non-null values total → skip; flag for manual review.

  4.4  Safety-check exception values: strip values that look like PII
       (email, phone) before sending to the AI.

  4.5  Build and send the AI patch prompt (temperature=0).

  4.6  Verify the AI expression against the exception values in DuckDB.

  4.7  Lock in (source="llm_patch") or fall back to deterministic
       TRY_CAST (source="llm_patch_fallback_det").
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import duckdb
import pandas as pd
from pydantic import BaseModel, ConfigDict

from app.debug_logger import DebugLogger
from app.llm_engine import _generate_structured
from app.preprocessing.expression_builder import (
    _as_varchar,
    _currency_expr,
    _percentage_expr,
    build_passthrough,
)
from app.preprocessing.fallback_guard import guard
from app.preprocessing.models import ClassifiedColumn, ColumnClass, ColumnExpression, ColumnMetadata
from app.preprocessing.sampler import CURRENCY_SYMBOL_RE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sentinel values that should always become NULL — checked lowercase-trimmed.
_SENTINEL_SET: frozenset[str] = frozenset({
    "tbd", "na", "n/a", "n.a.", "null", "none", "-", "–", "—",
    "nan", "#n/a", "nil", "", "not available", "missing",
})

# Characters stripped before the "has digits" check (punctuation / symbols).
_STRIP_PUNCT_RE = re.compile(r"[\s$%\-_./\\()+*=#@!?]")

# PII safety-check patterns for exception values (Section 3.5).
_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")
_PHONE_RE = re.compile(r"[\+\d][\d\s\-\(\)]{7,}")
# Plain numeric-with-separators dates (e.g. "26-12-2023", "2024/01/05") match
# _PHONE_RE too — every digit-and-dash exception value for a TIMESTAMP-target
# column was being misclassified as a phone number and skipped before ever
# reaching the AI, silently disabling exactly the date-format healing this
# step exists for. A value only needs the phone check when we're not already
# trying to parse it as a date.
_DATE_LIKE_RE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")

# Target type heuristics for VARCHAR column names (Section 3.2).
_DATE_NAME_TOKENS = frozenset({
    "date", "at", "on", "time", "timestamp", "datetime",
})
# Checked BEFORE _DATE_NAME_TOKENS: a column like "resolution_time_hrs"
# tokenizes to {"resolution", "time", "hrs"} and "time" alone would match
# _DATE_NAME_TOKENS, forcing a numeric duration (e.g. "72 hours", "0.5")
# into a TIMESTAMP cast. DuckDB's TRY_CAST(<double> AS TIMESTAMP) doesn't
# treat the number as epoch seconds — it just returns NULL — so this
# silently destroyed every value in the column. Duration tokens take
# priority over the date tokens so "_time" doesn't shadow them.
_DURATION_NAME_TOKENS = frozenset({
    "hrs", "hours", "hr", "mins", "minutes", "min",
    "secs", "seconds", "sec", "days", "duration",
})
_AMOUNT_NAME_TOKENS = frozenset({
    "amount", "price", "cost", "revenue", "balance",
    "fee", "total", "salary", "rate",
})
_BOOL_NAME_TOKENS = frozenset({
    "is_", "has_", "flag", "active", "enabled", "verified",
})

# Declared SQL types that mean the column is already natively typed.
_NATIVE_NUMERIC = ("int", "numeric", "decimal", "float", "real", "double", "bigint", "smallint")
_NATIVE_TIMESTAMP = ("timestamp", "datetime", "date")
_NATIVE_BOOLEAN = ("bool",)

# Boolean string values that standard processing already handles.
_BOOLEAN_KNOWN = frozenset({
    "true", "false", "t", "f", "yes", "no", "y", "n", "1", "0",
})

# ---------------------------------------------------------------------------
# AI patch response schema
# ---------------------------------------------------------------------------

class _PatchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    expression: str
    handles_nulls: bool = True
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Step 4.1 — Determine target type
# ---------------------------------------------------------------------------

def _q(col_name: str) -> str:
    return '"' + col_name.replace('"', '""') + '"'


def _name_tokens(col_name: str) -> set[str]:
    return set(col_name.lower().replace("-", "_").split("_"))


def _is_native_type(declared_type: str, hints: tuple[str, ...]) -> bool:
    dt = declared_type.lower()
    return any(h in dt for h in hints)


def determine_target_type(col: ColumnMetadata, sample_col: pd.Series) -> str | None:
    """Return 'DOUBLE', 'TIMESTAMP', 'BOOLEAN', or None.

    None means: already correctly typed (native), or cannot be determined
    with reasonable confidence → caller assigns passthrough.
    """
    dt = col.declared_type.lower()

    # Already natively typed → no casting needed.
    if _is_native_type(dt, _NATIVE_NUMERIC):
        return None
    if _is_native_type(dt, _NATIVE_TIMESTAMP):
        return None
    if _is_native_type(dt, _NATIVE_BOOLEAN):
        return None

    tokens = _name_tokens(col.name)

    # Name heuristics (highest priority). Duration tokens are checked first
    # so a name like "resolution_time_hrs" resolves to DOUBLE (it's a count
    # of hours/minutes/etc.), not TIMESTAMP just because it also contains
    # "time".
    if tokens & _DURATION_NAME_TOKENS:
        return "DOUBLE"
    if tokens & _DATE_NAME_TOKENS:
        return "TIMESTAMP"
    if tokens & _AMOUNT_NAME_TOKENS:
        return "DOUBLE"
    # Boolean prefix check (is_active, has_flag, ...).
    name_lower = col.name.lower()
    for tok in _BOOL_NAME_TOKENS:
        if name_lower.startswith(tok) or tok in name_lower:
            return "BOOLEAN"

    # Quick value scan tiebreaker (5 non-null values).
    values = sample_col.dropna().astype(str).str.strip()
    probe = values[values != ""].head(5)
    if probe.empty:
        return None

    # Boolean probe.
    if probe.str.lower().isin(_BOOLEAN_KNOWN).all():
        return "BOOLEAN"

    # Numeric probe. Strips ALL known currency symbols (not just "$") before
    # the parse check — otherwise this is order-dependent on the sample: a
    # column whose first 5 probed values happen to include a "£"/"€"/other
    # non-$ symbol would fail this check and fall through to a bare
    # passthrough (no cleaning, no cast, no AI — just the raw string),
    # purely because of which 5 rows landed in the sample.
    numeric_probe = pd.to_numeric(
        probe.str.replace(CURRENCY_SYMBOL_RE, "", regex=True).str.replace(r"[,%]", "", regex=True),
        errors="coerce",
    )
    if numeric_probe.notna().all():
        return "DOUBLE"

    # Date probe — ISO or common slash/dash patterns.
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}|\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}$")
    if probe.str.match(date_re, na=False).all():
        return "TIMESTAMP"

    return None


# ---------------------------------------------------------------------------
# Step 4.2 — Exception capture queries
# ---------------------------------------------------------------------------

_BOOLEAN_EXCEPTION_QUERY = (
    "SELECT DISTINCT CAST({col} AS VARCHAR) AS raw_value "
    "FROM {table} "
    "WHERE LOWER(TRIM(CAST({col} AS VARCHAR))) "
    "      NOT IN ('true','false','t','f','yes','no','y','n','1','0') "
    "  AND {col} IS NOT NULL "
    "LIMIT 20"
)


def _select_base_expression(col: ColumnMetadata, target_type: str, sample_col: pd.Series) -> str:
    """Step 4.2 — pick a deterministic base expression for ``target_type``.

    For DOUBLE columns this does a fast, presence-based ("any value
    contains...") check against the sample — NOT a ratio/threshold guess —
    and assigns the matching robust template (currency-symbol stripping,
    percentage normalization) instead of a plain ``TRY_CAST(... AS DOUBLE)``.

    Why this matters: a naive TRY_CAST fails on perfectly standard formatting
    like "$1,200.00" or "20%", so every clean currency/percentage value was
    being misclassified as an "exception" and sent to the AI — flooding the
    LLM with calls for data that was never actually broken, and (on AI
    failure) falling back to the same naive cast, turning that clean data
    into NULL. Routing standard formats through the existing deterministic
    currency/percentage templates up front means only genuine anomalies
    (e.g. "TBD", "100 USD (Approx)") ever become exceptions or reach the AI.
    """
    if target_type != "DOUBLE":
        return _base_expression(col.name, target_type)

    values = sample_col.dropna().astype(str)
    has_currency = bool(values.str.contains(CURRENCY_SYMBOL_RE).any())
    has_percent = (not has_currency) and bool(values.str.contains("%", regex=False).any())

    base = _as_varchar(col.name)
    if has_currency:
        return _currency_expr(col, base)
    if has_percent:
        return _percentage_expr(base)
    return _base_expression(col.name, target_type)


def _run_exception_query(
    conn: duckdb.DuckDBPyConnection,
    sample_table: str,
    col_name: str,
    target_type: str,
    base_expr: str,
) -> list[str]:
    """Find values the chosen ``base_expr`` (smart or naive) cannot parse.

    Exception detection always runs against the SAME expression that will
    become the column's final/fallback expression — never a separate
    hardcoded naive cast — so a value only counts as an "exception" if the
    deterministic template genuinely can't handle it.
    """
    col = _q(col_name)
    table = _q(sample_table)
    if target_type == "BOOLEAN":
        sql = _BOOLEAN_EXCEPTION_QUERY.format(col=col, table=table)
    else:
        sql = (
            f"SELECT DISTINCT CAST({col} AS VARCHAR) AS raw_value "
            f"FROM {table} "
            f"WHERE {base_expr} IS NULL "
            f"  AND {col} IS NOT NULL "
            f"LIMIT 20"
        )
    try:
        rows = conn.execute(sql).fetchdf()
        return rows["raw_value"].dropna().astype(str).tolist()
    except Exception as exc:
        logger.warning("Exception query failed for %s (%s): %s", col_name, target_type, exc)
        return []


# ---------------------------------------------------------------------------
# Step 4.4 — Safety check
# ---------------------------------------------------------------------------

def _is_pii_value(value: str, target_type: str) -> bool:
    if len(value) > 100:
        return True
    if _EMAIL_RE.search(value):
        return True
    # Skip the phone check for values we're trying to parse as dates —
    # a dash/slash-separated date (e.g. "26-12-2023") satisfies _PHONE_RE's
    # "digit followed by 7+ digits/separators" shape too, which would
    # otherwise flag every malformed date as PII and block AI patching.
    if target_type == "TIMESTAMP" and _DATE_LIKE_RE.match(value.strip()):
        return False
    if _PHONE_RE.search(value):
        return True
    return False


# ---------------------------------------------------------------------------
# Step 4.6 — Verification helpers
# ---------------------------------------------------------------------------

def _has_digits(value: str) -> bool:
    """True if the value contains at least one digit after stripping punctuation."""
    stripped = _STRIP_PUNCT_RE.sub("", value)
    return any(c.isdigit() for c in stripped)


def _is_expected_null(value: str) -> bool:
    """True if a NULL result from the AI expression is expected / correct.

    A NULL is expected when EITHER:
      - The value is in the explicit sentinel list.
      - The value contains zero numeric digits after stripping punctuation
        (e.g. "UNKNOWN", "PENDING", "---", "$", "%").
    """
    key = value.strip().lower()
    if key in _SENTINEL_SET:
        return True
    return not _has_digits(value)


def _count_unexpected_nulls(
    patched: pd.Series,
    raw_values: list[str],
) -> int:
    """Count NULLs in patched that are NOT expected (Section 3.7)."""
    count = 0
    for i, raw in enumerate(raw_values):
        if i >= len(patched):
            break
        val = patched.iloc[i]
        # pd.isna covers None, float NaN, and pd.NA (which DuckDB emits for NULLs).
        try:
            is_null = bool(pd.isna(val))
        except (TypeError, ValueError):
            is_null = False
        if is_null and not _is_expected_null(raw):
            count += 1
    return count


def _verify_patch(
    ai_expression: str,
    exception_values: list[str],
    col_name: str,
) -> bool:
    """Run the AI expression against exception values in DuckDB; return True if patch is good."""
    verify_df = pd.DataFrame({col_name: exception_values})
    conn = duckdb.connect()
    try:
        conn.register("verify_tbl", verify_df)
        result = conn.execute(
            f"SELECT {ai_expression} AS patched FROM verify_tbl"
        ).fetchdf()
        unexpected = _count_unexpected_nulls(result["patched"], exception_values)
        return unexpected == 0
    except Exception as exc:
        logger.warning("Patch verification failed for %s: %s", col_name, exc)
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step 4.5 — Prompt builder
# ---------------------------------------------------------------------------

_PATCH_SYSTEM = """\
You are a DuckDB SQL expert. Write a single DuckDB SQL column expression \
(NOT a full SELECT statement — only the expression for one column).

Respond ONLY with a JSON object. No explanation. No markdown fences.

Output schema:
{
  "expression": "DuckDB SQL expression string",
  "handles_nulls": true | false,
  "reasoning": "one sentence"
}

Rules:
- The expression must reference the source column as the double-quoted \
identifier provided.
- Use TRY_CAST everywhere. Never use hard CAST.
- The output type must match the requested target type exactly.
- Values that cannot be meaningfully parsed must become SQL NULL — \
not 0, not an empty string, not a placeholder.
- The expression must handle both the standard values (which already \
work with a basic TRY_CAST) AND the exception values listed.
- Use only DuckDB-compatible functions: TRY_CAST, TRY_STRPTIME, \
REGEXP_REPLACE, REGEXP_MATCHES, CASE WHEN, COALESCE, TRIM, LOWER, \
NULLIF, SUBSTR, LENGTH."""


def _base_expression(col_name: str, target_type: str) -> str:
    q = _q(col_name)
    varchar_expr = f"TRIM(CAST({q} AS VARCHAR))"
    if target_type == "TIMESTAMP":
        return f"TRY_CAST({varchar_expr} AS TIMESTAMP)"
    if target_type == "BOOLEAN":
        return f"TRY_CAST({varchar_expr} AS BOOLEAN)"
    return f"TRY_CAST({varchar_expr} AS DOUBLE)"


def _build_patch_user_message(
    table_name: str,
    col: ColumnMetadata,
    target_type: str,
    exception_values: list[str],
    base_expr: str,
) -> str:
    q = _q(col.name)
    values_block = "\n".join(f'  "{v}"' for v in exception_values)

    # Identify which exception values should clearly become NULL (for the hint).
    null_hints = [v for v in exception_values if _is_expected_null(v)]
    null_hint_line = ""
    if null_hints:
        null_hint_line = (
            f'\n"{", ".join(null_hints)}" and similar non-numeric/non-parseable '
            f"strings must become SQL NULL."
        )

    return (
        f"Table: {table_name}\n"
        f"Column: {q} ({col.declared_type})\n"
        f"Target type: {target_type}\n\n"
        f"Current working expression (handles the majority of rows):\n"
        f"  {base_expr}\n\n"
        f"This expression returns NULL for the following specific exception values\n"
        f"(captured from the sample — these are the only values you need to fix):\n"
        f"{values_block}\n"
        f"{null_hint_line}\n"
        f"Write an updated single-column expression that handles both the standard "
        f"values AND these exceptions. Return only the JSON object."
    )


# ---------------------------------------------------------------------------
# Per-column capture and patch
# ---------------------------------------------------------------------------

def _capture_and_patch_column(
    c: ClassifiedColumn,
    conn: duckdb.DuckDBPyConnection,
    sample_table: str,
    sample: pd.DataFrame,
    target_type: str,
    llm_provider: str | None,
    llm_model: str | None,
    api_key: str | None,
    disable_llm: bool,
    review_notes: list[str],
    debug: DebugLogger | None,
) -> ColumnExpression:
    col = c.column
    col_name = col.name

    # Check we have enough data.
    if col_name in sample.columns:
        non_null_count = sample[col_name].notna().sum()
    else:
        non_null_count = 0

    if non_null_count < 10:
        review_notes.append(
            f"Column '{col_name}': insufficient sample data ({non_null_count} non-null rows) "
            f"to determine format — verify manually."
        )
        return build_passthrough(col)

    # Step 4.2 — pick the smart deterministic base expression (currency/
    # percentage template for DOUBLE columns when the sample shows the
    # corresponding symbol present; naive TRY_CAST otherwise), then run the
    # exception query against THAT expression — not a separate naive one.
    sample_col = sample[col_name] if col_name in sample.columns else pd.Series(dtype=object)
    base_expr = _select_base_expression(col, target_type, sample_col)
    exceptions = _run_exception_query(conn, sample_table, col_name, target_type, base_expr)

    # A column with >1 distinct currency symbol (e.g. "$", "£", "€" all in
    # the same column) is genuinely ambiguous, not a formatting nuisance —
    # column_classifier.py already flags this case for LLM/human judgment.
    # The currency template above strips whichever symbol is present and
    # treats the number as-is regardless of which one, so it WILL report
    # 0 exceptions (every value parses) while silently treating $1, £1 and
    # €1 as numerically equal. That's a real correctness risk (no FX
    # conversion applied) with no AI escalation and no error to catch it —
    # flag it explicitly so it isn't mistaken for "fully resolved, no
    # action needed." The clarification this column already gets from
    # ``_build_clarifications`` (its "Other" free-form option) is exactly
    # where a fixed conversion rate per symbol should be supplied.
    if target_type == "DOUBLE" and len(col.currency_symbols) > 1:
        review_notes.append(
            f"Column '{col_name}': contains multiple currency symbols "
            f"({', '.join(col.currency_symbols)}) — values were stripped of "
            f"their symbol and treated as the same numeric unit with NO "
            f"currency conversion applied (e.g. \"€890.00\" became 890.0, "
            f"not its USD equivalent). If these are genuinely different "
            f"currencies, answer this column's clarification question with "
            f"the conversion rates to apply before locking."
        )

    # 0 exceptions → the smart base expression already handles every value.
    if not exceptions:
        return ColumnExpression(
            col_name=col_name,
            output_names=[col_name],
            sql_exprs=[base_expr],
            source="deterministic",
            issues_handled=[f"{target_type.lower()}_cast"],
        )

    # Apply PII safety check before sending exceptions to AI.
    safe_exceptions = [v for v in exceptions if not _is_pii_value(v, target_type)]
    pii_removed = len(exceptions) - len(safe_exceptions)
    if pii_removed > 0:
        logger.warning(
            "Column '%s': removed %d PII-looking exception values before AI prompt.",
            col_name, pii_removed,
        )

    if not safe_exceptions:
        guard(
            f"Column '{col_name}': all {len(exceptions)} exception value(s) "
            f"looked like PII — AI patching skipped, would fall back to "
            f"the smart base expression (already known to fail on these "
            f"specific values)."
        )
        review_notes.append(
            f"Column '{col_name}': all exception values looked like PII — "
            f"AI patching skipped. Standard processing applied; some values may remain NULL."
        )
        return ColumnExpression(
            col_name=col_name,
            output_names=[col_name],
            sql_exprs=[base_expr],
            source="deterministic",
            issues_handled=[f"{target_type.lower()}_cast"],
        )

    # Skip AI if disabled — use the smart base expression (currency/
    # percentage template when applicable, not a naive TRY_CAST).
    if disable_llm:
        unresolved = safe_exceptions
        review_notes.append(
            f"Column '{col_name}': AI patch skipped (disable_llm=True). "
            f"Standard processing applied. Unresolved exceptions: {unresolved}."
        )
        return ColumnExpression(
            col_name=col_name,
            output_names=[col_name],
            sql_exprs=[base_expr],
            source="llm_patch_fallback_det",
            issues_handled=[f"{target_type.lower()}_cast"],
        )

    # Step 4.5 — Build and send AI patch prompt.
    user_msg = _build_patch_user_message(
        sample_table, col, target_type, safe_exceptions, base_expr,
    )
    full_prompt = f"{_PATCH_SYSTEM}\n\n{user_msg}"

    if debug:
        debug.code(f"Exception capture prompt for '{col_name}'", full_prompt)

    try:
        patch: _PatchResponse = _generate_structured(
            prompt=full_prompt,
            response_schema=_PatchResponse,
            provider=llm_provider,
            model=llm_model,
            api_key=api_key,
            temperature=0.0,
        )
        ai_expression = patch.expression.strip()
    except Exception as exc:
        logger.error("AI patch call failed for column '%s': %s", col_name, exc)
        guard(
            f"Column '{col_name}': AI patch call failed ({exc}) — would fall "
            f"back to the smart base expression. Unresolved exceptions: {safe_exceptions}."
        )
        review_notes.append(
            f"Column '{col_name}': AI patch call failed ({exc}). "
            f"Standard processing applied. Unresolved exceptions: {safe_exceptions}."
        )
        return ColumnExpression(
            col_name=col_name,
            output_names=[col_name],
            sql_exprs=[base_expr],
            source="llm_patch_fallback_det",
            issues_handled=[f"{target_type.lower()}_cast"],
        )

    if debug:
        debug.code(f"AI patch expression for '{col_name}'", ai_expression)

    # Step 4.6 — Verify the patch.
    passed = _verify_patch(ai_expression, safe_exceptions, col_name)

    if passed:
        return ColumnExpression(
            col_name=col_name,
            output_names=[col_name],
            sql_exprs=[ai_expression],
            source="llm_patch",
            issues_handled=[f"{target_type.lower()}_cast", "exception_patch"],
        )

    # Patch failed — fall back to deterministic TRY_CAST.
    failed_values = [
        v for v in safe_exceptions
        if _has_digits(v) and v.strip().lower() not in _SENTINEL_SET
    ]
    guard(
        f"Column '{col_name}': AI patch could not be verified "
        f"(expression: {ai_expression!r}) — would fall back to the smart "
        f"base expression. Unresolved exceptions: {failed_values}."
    )
    review_notes.append(
        f"Column '{col_name}': AI patch could not be verified — standard processing applied. "
        f"Some values may remain NULL after processing. "
        f"Unresolved exceptions: {failed_values}."
    )
    return ColumnExpression(
        col_name=col_name,
        output_names=[col_name],
        sql_exprs=[base_expr],
        source="llm_patch_fallback_det",
        issues_handled=[f"{target_type.lower()}_cast"],
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_exception_capture(
    observe_columns: list[ClassifiedColumn],
    sample: pd.DataFrame,
    table_name: str,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    api_key: str | None = None,
    disable_llm: bool = False,
    debug: DebugLogger | None = None,
) -> tuple[list[ColumnExpression], list[str]]:
    """Run Self-Healing Exception Capture for all OBSERVE columns.

    Returns ``(expressions, review_notes)`` where ``review_notes`` is a list
    of plain-English notes for columns that need manual review.

    Guaranteed to return exactly one ``ColumnExpression`` per input column.
    """
    if not observe_columns:
        return [], []

    review_notes: list[str] = []
    expressions: list[ColumnExpression] = []

    # Register the sample in a shared in-memory DuckDB connection so all
    # exception queries share one connection (avoid repeated DataFrame copies).
    conn = duckdb.connect()
    try:
        # DuckDB table name must be a simple identifier.
        duckdb_table = re.sub(r"[^a-zA-Z0-9_]", "_", table_name)
        conn.register(duckdb_table, sample)

        for c in observe_columns:
            col = c.column
            # Step 4.1 — Determine target type.
            sample_col = sample[col.name] if col.name in sample.columns else pd.Series(dtype=object)
            target_type = determine_target_type(col, sample_col)

            if target_type is None:
                # Already correctly typed or type cannot be determined → passthrough.
                expressions.append(build_passthrough(col))
                continue

            expr = _capture_and_patch_column(
                c, conn, duckdb_table, sample, target_type,
                llm_provider, llm_model, api_key, disable_llm, review_notes, debug,
            )
            expressions.append(expr)

    finally:
        conn.close()

    return expressions, review_notes
