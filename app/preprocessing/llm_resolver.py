"""Stage 0 v3.0 — focused LLM resolver for CLEAN_AMBIG columns only
(stage0_v3_spec.md §7).

Called at most once per table, only when CLEAN_AMBIG columns exist (typically
2-5 per table). The LLM sees only those columns' name, declared type, detected
issues and sample values — never the rest of the table, and never PII columns
(those are excluded long before sampling by ``column_classifier.pre_classify``).

On ANY failure (LLM unavailable, malformed response, unknown column, etc.)
:func:`build_expression` is the guaranteed deterministic fallback — this
module never raises and never leaves a column unresolved.
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.debug_logger import DebugLogger
from app.llm_engine import _generate_structured
from app.preprocessing.expression_builder import (
    _as_varchar,
    _q,
    build_expression,
    build_passthrough,
)
from app.preprocessing.fallback_guard import guard
from app.preprocessing.models import ClassifiedColumn, ColumnExpression, ColumnMetadata

logger = logging.getLogger(__name__)

# Used by apply_clarification_answer's currency-split branch.
ALL_CURRENCY_SYMBOLS = r"\$€£¥₹₩₽₪₫₴₦₱฿₲₡₵₸₮₭₼₾₺"


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------

class ResolutionItem(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    column: str
    reasoning: str = ""
    action: str = "resolve"  # "resolve" | "clarify"
    split: bool = False
    output_names: list[str] = Field(default_factory=list)
    sql_exprs: list[str] = Field(default_factory=list)
    clarification_question: Optional[str] = None
    clarification_options: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_singular_field_names(cls, data):
        """Observed (Groq openai/gpt-oss-120b, single-output non-split
        resolutions): the model writes ``sql_expr``/``output_name``
        (singular) instead of the schema's ``sql_exprs``/``output_names``
        arrays — the expression itself is correct, just shaped for "one
        value" instead of the (rarely used) multi-output currency-split
        case. Without this, the singular keys are silently dropped by
        ``extra=\"ignore\"``, the arrays default to empty, and a perfectly
        good resolution gets discarded as if the LLM had failed.
        """
        if not isinstance(data, dict):
            return data
        if not data.get("sql_exprs") and data.get("sql_expr"):
            data = {**data, "sql_exprs": [data["sql_expr"]]}
        if not data.get("output_names") and data.get("output_name"):
            data = {**data, "output_names": [data["output_name"]]}
        return data


class ResolverResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    resolutions: list[ResolutionItem]


SYSTEM_PROMPT = """You are a DuckDB SQL expert specializing in data quality.

You will receive a list of columns that have AMBIGUOUS data quality issues — cases
where the correct SQL expression cannot be determined without judgment. For each
column:

1. Analyze the samples and detected issue(s).
2. Either provide a DuckDB SQL expression (action="resolve"), or raise a
   clarification question for the user (action="clarify") if you genuinely
   cannot determine the correct interpretation from the samples alone.

Rules for SQL expressions:
- Must be valid DuckDB SQL, usable directly in a SELECT clause as the source of
  an alias (do not write the alias yourself).
- Always reference the original column, double-quoted: "col_name"
- Use TRY_CAST everywhere, never bare CAST.
- Use REGEXP_REPLACE with the 'g' flag for multi-occurrence replacements.
- Never reference other tables, files, or external functions.
- String literals use single quotes only (DuckDB treats double quotes as identifiers).

For a multi-currency split, set split=true and provide two output_names
(`<col>_amount`, `<col>_currency`) and two sql_exprs.

Percentage normalization rule (applies whenever a column mixes '%'-suffixed
values with bare numbers, e.g. '5.2%' and '0.02' in the same column): convert
every value to a consistent fraction (0-1 scale) using this magnitude-aware
logic, so '5.2%' -> 0.052 and a bare '0.02' (already a fraction) stays 0.02,
while a bare '45' (no '%', > 1) -> 0.45:
  CASE
    WHEN <sentinel-is-null> THEN NULL
    WHEN strpos(<val>, '%') > 0 THEN <numeric-part> / 100.0
    WHEN ABS(<numeric-part>) > 1 THEN <numeric-part> / 100.0
    ELSE <numeric-part>
  END
Do NOT divide every value by 100 unconditionally — that corrupts values that
are already fractions.

Clarification rules:
- action="clarify" only when you truly cannot determine the correct interpretation.
- clarification_options must have 2-4 short (2-6 word) entries, and always include
  "Leave as text" as a final option.
- Still provide a best-effort sql_exprs/output_names even when action="clarify", so
  the script remains valid if the user never answers.
- If the issue is mixed_date_format and you cannot confidently determine whether
  the format is US (MM/DD/YYYY) or International (DD/MM/YYYY) from the samples
  (e.g. every day and month component is <= 12), you MUST set action="clarify"
  and provide exactly these clarification_options, in this order:
  ["MM/DD/YYYY (US)", "DD/MM/YYYY (International)", "Leave as text"].

Respond ONLY with a JSON object: {"resolutions": [...]}, one entry per input column."""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(
    ambiguous: list[ClassifiedColumn], column_overrides: dict[str, str] | None = None,
) -> str:
    blocks = []
    for c in ambiguous:
        col = c.column
        issues_str = ", ".join(c.active_issues) or "(user-specified override)"
        samples_str = "\n  ".join(col.sample_values[:10]) or "(no samples)"

        context_lines = []
        if col.currency_symbols:
            context_lines.append(f"Currency symbols found: {', '.join(col.currency_symbols)}")
        if col.date_format:
            context_lines.append(f"Detected column-wide date format: {col.date_format}")
        mixed_pct = col.issue_ratios.get("mixed_percent_format_ratio", 0.0)
        if mixed_pct > 0.0:
            context_lines.append(
                f"Mixed percentage format: {mixed_pct:.0%} of rows mix '%' and bare values"
            )
        override = (column_overrides or {}).get(col.name)
        if override:
            context_lines.append(f"USER INSTRUCTION (must follow): {override}")

        context_str = (f"Context: {'; '.join(context_lines)}\n") if context_lines else ""

        blocks.append(
            f'Column: "{col.name}"\n'
            f"Declared type: {col.declared_type}\n"
            f"Null %: {col.null_pct:.1%}\n"
            f"Active issues: {issues_str}\n"
            f"Samples:\n  {samples_str}\n"
            f"{context_str}"
        )

    return "Resolve the following ambiguous columns:\n\n" + "\n---\n".join(blocks)


# ---------------------------------------------------------------------------
# Response parsing + fallback
# ---------------------------------------------------------------------------

def _parse_resolution(res: ResolutionItem, col: ColumnMetadata) -> ColumnExpression:
    output_names = res.output_names or [col.name]
    if not res.sql_exprs:
        guard(
            f"Column '{col.name}': LLM resolver response had no usable "
            f"sql_exprs (action={res.action!r}) — would fall back to a bare "
            f"passthrough, silently dropping any transformation/conversion."
        )
    sql_exprs = res.sql_exprs or [_q(col.name)]

    return ColumnExpression(
        col_name=col.name,
        output_names=output_names,
        sql_exprs=sql_exprs,
        source="llm",
        clarification_needed=(res.action == "clarify"),
        clarification_question=res.clarification_question if res.action == "clarify" else None,
        clarification_options=res.clarification_options if res.action == "clarify" else [],
    )


def _deterministic_fallback(c: ClassifiedColumn) -> ColumnExpression:
    expr = build_expression(c.column, c.active_issues)
    return ColumnExpression(
        col_name=expr.col_name,
        output_names=expr.output_names,
        sql_exprs=expr.sql_exprs,
        source="llm_fallback_det",
        issues_handled=expr.issues_handled,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def resolve_ambiguous(
    ambiguous: list[ClassifiedColumn],
    column_overrides: dict[str, str] | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    api_key: str | None = None,
    debug: DebugLogger | None = None,
) -> list[ColumnExpression]:
    """Resolve CLEAN_AMBIG columns via a single focused LLM call.

    Guaranteed to return exactly one ``ColumnExpression`` per input column —
    on any LLM failure (or for any column the LLM omits/mis-names), falls
    back to :func:`build_expression`/:func:`_deterministic_fallback`.
    """
    if not ambiguous:
        return []

    prompt = _build_prompt(ambiguous, column_overrides)
    if debug:
        debug.code("LLM resolver prompt (CLEAN_AMBIG columns)", prompt)

    col_lookup = {c.column.name: c for c in ambiguous}

    try:
        result: ResolverResponse = _generate_structured(
            prompt=f"{SYSTEM_PROMPT}\n\n{prompt}",
            response_schema=ResolverResponse,
            provider=llm_provider,
            model=llm_model,
            api_key=api_key,
            temperature=0.0,
            debug=debug,
        )

        expressions: list[ColumnExpression] = []
        resolved_names: set[str] = set()

        for res in result.resolutions:
            classified = col_lookup.get(res.column)
            if classified is None:
                logger.warning("LLM resolver returned unknown column: %s", res.column)
                continue
            expressions.append(_parse_resolution(res, classified.column))
            resolved_names.add(res.column)

        for c in ambiguous:
            if c.column.name not in resolved_names:
                logger.warning(
                    "LLM resolver omitted column '%s' — using deterministic fallback",
                    c.column.name,
                )
                guard(
                    f"Column '{c.column.name}': LLM resolver omitted this "
                    f"column from its response — would fall back to "
                    f"deterministic expression."
                )
                expressions.append(_deterministic_fallback(c))

        if debug:
            debug.code("LLM resolver result", [e.__dict__ for e in expressions], lang="json")
        return expressions

    except Exception as exc:
        logger.error(
            "LLM resolver failed (%s). Using deterministic fallback for all %d ambiguous column(s).",
            exc, len(ambiguous),
        )
        if debug:
            debug.section(
                "LLM resolver FAILED — deterministic fallback applied for all ambiguous columns",
                str(exc),
            )
        guard(
            f"LLM resolver failed ({exc}) for {len(ambiguous)} ambiguous "
            f"column(s) ({[c.column.name for c in ambiguous]}) — would fall "
            f"back to deterministic expressions for all of them."
        )
        return [_deterministic_fallback(c) for c in ambiguous]


# ---------------------------------------------------------------------------
# Clarification answer -> final expression (no second LLM call)
# ---------------------------------------------------------------------------

def apply_clarification_answer(col: ColumnMetadata, answer: str) -> ColumnExpression:
    """Resolve a column's expression deterministically from the user's
    clarification answer text — no second LLM call."""
    a = answer.lower().strip()
    s = ALL_CURRENCY_SYMBOLS

    if "split" in a or ("amount" in a and "currency" in a):
        return ColumnExpression(
            col_name=col.name,
            output_names=[f"{col.name}_amount", f"{col.name}_currency"],
            sql_exprs=[
                f"TRY_CAST(REGEXP_REPLACE(REGEXP_REPLACE({_as_varchar(col.name)}, '[{s}\\s,]', '', 'g'), ',', '.') AS DOUBLE)",
                f"REGEXP_EXTRACT({_as_varchar(col.name)}, '[{s}]')",
            ],
            source="llm",
        )
    if "leave" in a or "unchanged" in a or ("text" in a and "as-is" in a) or "as text" in a:
        return build_passthrough(col)

    if "mm/dd" in a or "mdy" in a or "american" in a:
        return ColumnExpression(
            col_name=col.name, output_names=[col.name],
            sql_exprs=[f"TRY_STRPTIME(TRIM({_as_varchar(col.name)}), '%m/%d/%Y')"],
            source="llm",
        )
    if "dd/mm" in a or "dmy" in a or "european" in a or "international" in a:
        return ColumnExpression(
            col_name=col.name, output_names=[col.name],
            sql_exprs=[f"TRY_STRPTIME(TRIM({_as_varchar(col.name)}), '%d/%m/%Y')"],
            source="llm",
        )

    if "fraction" in a or "0 to 1" in a or "decimal" in a:
        return ColumnExpression(
            col_name=col.name, output_names=[col.name],
            sql_exprs=[f"TRY_CAST(TRIM({_as_varchar(col.name)}) AS DOUBLE)"],
            source="llm",
        )
    if "percent" in a or "0 to 100" in a:
        return ColumnExpression(
            col_name=col.name, output_names=[col.name],
            sql_exprs=[f"TRY_CAST(TRIM({_as_varchar(col.name)}) AS DOUBLE) / 100.0"],
            source="llm",
        )

    logger.warning(
        "Could not interpret clarification answer %r for column '%s' — "
        "deterministic fallback applied.", answer, col.name,
    )
    guard(
        f"Column '{col.name}': clarification answer {answer!r} did not "
        f"match any known pattern (split/leave-as-text/date-format/"
        f"fraction/percent) — would fall back to deterministic expression, "
        f"silently dropping the user's instruction."
    )
    return build_expression(col, [i for i in (col.inferred_issues or [])])
