"""Stage 0 v3.0 — metadata profiler + cleaning-script builder
(stage0_v3_spec.md §10-11).

:func:`profile_table` connects, extracts structural metadata, pre-classifies
every column (Column Intelligence Gate), pulls a stratified sample of every
column (so ``dry_run`` can later execute the full assembled SELECT), and
enriches only candidate columns with their top sample values and inferred
cleaning issues. PII/IDENTIFIER/STRUCTURAL columns get empty
``sample_values``/``inferred_issues`` and so never reach the LLM, even though
their raw values are present in the in-memory sample DataFrame.

:func:`build_cleaning_script` takes that profiled metadata + sample and
produces a :class:`CleaningScript`: post-classifies every column, builds a
deterministic ``ColumnExpression`` for CLEAN_DET/OBSERVE/SKIP columns, sends
only the CLEAN_AMBIG columns (if any) to the focused LLM resolver, then
assembles the final SELECT. Per-column expression slots mean a column can
never be silently "skipped".
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Callable

import pandas as pd

from app.debug_logger import DebugLogger
from app.preprocessing.column_classifier import (
    _is_identifier,
    _name_tokens,
    needs_sample,
    post_classify,
    pre_classify,
    summary,
)
from app.preprocessing.connector import get_table_metadata
from app.preprocessing.expression_builder import build_expression, build_passthrough
from app.preprocessing.llm_resolver import (
    _deterministic_fallback,
    apply_clarification_answer,
    resolve_ambiguous,
)
from app.preprocessing.models import (
    ClassifiedColumn,
    CleaningScript,
    ColumnClass,
    ColumnExpression,
    ColumnMetadata,
    TableMetadata,
)
from app.preprocessing.sampler import (
    NULL_VARIANTS,
    compute_issue_ratios,
    detect_column_issues,
    detect_currency_symbols,
    detect_date_format,
    extract_stratified_sample,
    select_diverse_sample_values,
)
from app.preprocessing.sql_assembler import build_audit_log, build_select

SAMPLE_VALUES_PER_COLUMN = 10

# ---------------------------------------------------------------------------
# Stage 0.5 (stage0_v3_spec.md) — format signatures.
#
# A "format signature" is a short, PII-free pattern descriptor (never derived
# from / containing actual row values) computed for every column during
# column-wise profiling, so the later cross-table consistency pass can group
# and align columns across tables without re-touching raw data.
# ---------------------------------------------------------------------------

PHONE_NAME_TOKENS: frozenset[str] = frozenset({"phone", "mobile", "cell", "tel", "fax", "whatsapp"})
_DATE_NAME_TOKENS: frozenset[str] = frozenset({"date", "time", "at", "on", "timestamp", "datetime"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_NUMERIC_DECLARED_TOKENS = ("int", "numeric", "decimal", "serial")
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")


def _id_format_signature(col: ColumnMetadata, values: list[str]) -> str | None:
    """Pattern descriptor for ID/key columns: 'numeric' (sequential numeric
    ID, possibly stored as text) or 'alnum' (contains letters — UUID/hash/hex
    -like, must never have leading zeros stripped). Never includes the actual
    ID values."""
    dt = col.declared_type.lower()
    if any(t in dt for t in _NUMERIC_DECLARED_TOKENS):
        return "numeric"
    if not values:
        return None
    if any(_HAS_LETTER_RE.search(v) for v in values[:50]):
        return "alnum"
    return "numeric"


def _phone_format_signature(values: list[str]) -> str | None:
    """Pattern descriptor like 'local_10d' / 'intl_12d' — digit count and
    whether an international '+' prefix is present. Never includes the
    digits themselves."""
    if not values:
        return None
    digit_counts = []
    has_plus = 0
    for v in values[:50]:
        digits = re.sub(r"\D", "", v)
        if not digits:
            continue
        digit_counts.append(len(digits))
        if v.strip().startswith("+"):
            has_plus += 1
    if not digit_counts:
        return None
    common_len = Counter(digit_counts).most_common(1)[0][0]
    prefix = "intl" if has_plus / len(digit_counts) > 0.5 else "local"
    return f"{prefix}_{common_len}d"


def _date_format_signature(col: ColumnMetadata, sample_col: pd.Series) -> str | None:
    """Pattern descriptor for date/timestamp-like columns: 'native_timestamp'
    for already-typed columns, a strptime format string (e.g. '%d/%m/%Y') for
    detected string formats, or 'ambiguous' if no single format could be
    determined column-wide. Returns None for columns that don't look
    date-like at all."""
    dt = col.declared_type.lower()
    if any(t in dt for t in ("timestamp", "datetime", "date")) and not any(
        s in dt for s in ("varchar", "char", "text")
    ):
        return "native_timestamp"
    if not (_name_tokens(col.name) & _DATE_NAME_TOKENS):
        return None
    values = sample_col.dropna().astype(str).str.strip()
    if values.empty:
        return None
    # Check ISO (YYYY-MM-DD) before falling back to the mixed_date_format /
    # slash-date heuristics — detect_date_format only recognises
    # slash/dash DD-MM-YYYY-style values and would otherwise misclassify a
    # clean ISO column as "ambiguous".
    if (values.str.match(_ISO_DATE_RE)).mean() > 0.8:
        return "%Y-%m-%d"
    if "mixed_date_format" in col.inferred_issues:
        return col.date_format or "ambiguous"
    return detect_date_format(sample_col)


def profile_table(
    db_uri: str, table_name: str, schema: str | None = None,
    debug: DebugLogger | None = None,
) -> tuple[TableMetadata, pd.DataFrame]:
    """Profile a source table: structural metadata + targeted stratified
    sample + issue detection, restricted to non-SKIP (candidate) columns."""
    metadata = get_table_metadata(db_uri, table_name, schema=schema)
    if debug:
        debug.code("Structural metadata", metadata.model_dump(), lang="json")

    pre_classified = [pre_classify(col) for col in metadata.columns]
    candidate_cols = [c.column.name for c in pre_classified if needs_sample(c)]
    if debug:
        debug.code(
            "Pre-classification (Column Intelligence Gate)",
            {
                c.column.name: {"class": c.classification.value, "reasons": c.reasons}
                for c in pre_classified
            },
            lang="json",
        )

    # Always pull every column — dry_run needs the full row shape to execute
    # the assembled SELECT (which includes passthrough PII/IDENTIFIER
    # columns). Privacy isolation happens next, in enrich_metadata_with_sample.
    sample = extract_stratified_sample(db_uri, metadata)
    if debug:
        debug.section(
            "Stratified sample",
            f"{len(sample)} rows x {len(sample.columns)} columns "
            f"(profiled/LLM-eligible candidates: {candidate_cols or '(none)'}).",
        )

    enrich_metadata_with_sample(metadata, sample, candidate_cols)
    if debug:
        debug.code(
            "Issue detection per column",
            {
                col.name: {
                    "declared_type": col.declared_type,
                    "inferred_issues": col.inferred_issues,
                    "issue_ratios": col.issue_ratios,
                    "sample_values": col.sample_values,
                    "currency_symbols": col.currency_symbols,
                    "date_format": col.date_format,
                }
                for col in metadata.columns
            },
            lang="json",
        )
    return metadata, sample


def enrich_metadata_with_sample(
    metadata: TableMetadata, sample: pd.DataFrame, candidate_cols: list[str] | None = None,
) -> TableMetadata:
    """Populate ``sample_values`` and ``inferred_issues`` for each candidate
    column in place. Columns excluded from the sample (PII/IDENTIFIER/
    STRUCTURAL/declared-BOOLEAN) are left with empty issues — they were never
    pulled into ``sample`` and must never be profiled from it."""
    candidates = set(candidate_cols) if candidate_cols is not None else None
    for col in metadata.columns:
        is_candidate = candidates is None or col.name in candidates
        if not is_candidate:
            col.sample_values = []
            col.inferred_issues = []
        if col.name in sample.columns:
            col_values = sample[col.name].dropna().astype(str)
            if is_candidate:
                unique_values = col_values.unique().tolist()
                col.sample_values = select_diverse_sample_values(unique_values, SAMPLE_VALUES_PER_COLUMN)
                col.issue_ratios = compute_issue_ratios(sample, col)
                col.inferred_issues = detect_column_issues(sample, col, ratios=col.issue_ratios)
                if "currency_string" in col.inferred_issues:
                    symbols = detect_currency_symbols(col_values)
                    if len(symbols) > 1:
                        col.currency_symbols = symbols
                if "mixed_date_format" in col.inferred_issues:
                    col.date_format = detect_date_format(sample[col.name])
                if len(col_values) > 0:
                    sentinel_mask = col_values.str.strip().str.lower().isin(NULL_VARIANTS)
                    col.null_sentinel_pct = float(sentinel_mask.mean())

            # Stage 0.5: format_signature is computed for EVERY column present
            # in the sample (including PII/phone columns) — it is a pattern
            # descriptor only, never raw values, so it doesn't violate the
            # PII boundary above.
            if _name_tokens(col.name) & PHONE_NAME_TOKENS:
                col.format_signature = _phone_format_signature(col_values.tolist())
            elif _is_identifier(col):
                col.format_signature = _id_format_signature(col, col_values.tolist())
            else:
                col.format_signature = _date_format_signature(col, sample[col.name])
        elif not is_candidate:
            col.sample_values = []
            col.inferred_issues = []
    return metadata


# Slug-ify a free-form option label into a stable snake_case id for the
# review UI's clarification-question options.
def _option_id(label: str) -> str:
    return "_".join(label.lower().split()) or "option"


def _infer_script_source(expressions: list[ColumnExpression]) -> str:
    sources = {e.source for e in expressions}
    if "llm" in sources:
        return "llm"
    if "llm_fallback_det" in sources:
        return "deterministic_fallback"
    return "deterministic"


def _extract_clarifications(expressions: list[ColumnExpression]) -> list[dict]:
    clarifications: list[dict] = []
    for expr in expressions:
        if not expr.clarification_needed:
            continue
        options = [{"id": _option_id(o), "label": o} for o in expr.clarification_options]
        if not options:
            options = [{"id": "leave_as_text", "label": "Leave as text"}]
        clarifications.append({
            "column": expr.col_name,
            "question": expr.clarification_question or f"How should '{expr.col_name}' be cleaned?",
            "options": options,
            "default": options[0]["id"],
        })
    return clarifications


# Overrides whose instruction text is one of the simple, deterministically
# parseable forms `apply_clarification_answer` already understands (currency
# split / keep-as-text) — handled without an LLM call. Anything else (custom
# free-form instructions) is forwarded to `llm_resolver` as context.
def _is_simple_override(instruction: str) -> bool:
    i = instruction.lower()
    return (
        "split this column into two output columns" in i
        or "leave this column completely unchanged" in i
    )


def build_cleaning_script(
    metadata: TableMetadata,
    sample: pd.DataFrame,
    column_overrides: dict[str, str] | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    api_key: str | None = None,
    disable_llm: bool = False,
    debug: DebugLogger | None = None,
    expression_patch: Callable[[ColumnExpression, ColumnMetadata], ColumnExpression] | None = None,
) -> CleaningScript:
    """Build the per-table cleaning ``CleaningScript`` (v3.0 pipeline).

    Every column gets exactly one ``ColumnExpression`` slot — built
    deterministically for CLEAN_DET/OBSERVE/SKIP/FREE_TEXT columns, and via a
    single focused LLM call (with guaranteed deterministic fallback) for any
    CLEAN_AMBIG columns. Columns cannot be silently skipped.

    ``disable_llm``: skip the LLM resolver entirely and use the deterministic
    fallback for every CLEAN_AMBIG column. Used to retry after the LLM-influenced
    SQL fails AST validation, without a second LLM round-trip.
    """
    column_overrides = column_overrides or {}
    classified = [post_classify(col) for col in metadata.columns]
    if debug:
        debug.code(
            "Post-classification (Column Intelligence Gate)",
            {c.column.name: {"class": c.classification.value, "reasons": c.reasons,
                              "active_issues": c.active_issues} for c in classified},
            lang="json",
        )

    expressions: list[ColumnExpression] = []
    ambiguous: list[ClassifiedColumn] = []

    for c in classified:
        col = c.column
        override = column_overrides.get(col.name)

        if override:
            if "leave this column completely unchanged" in override.lower():
                expressions.append(build_passthrough(col))
            elif _is_simple_override(override):
                expressions.append(apply_clarification_answer(col, override))
            else:
                # Custom free-form instruction — forward to the LLM resolver
                # as context, regardless of this column's own classification.
                ambiguous.append(
                    ClassifiedColumn(col, ColumnClass.CLEAN_AMBIG, ["user override"], c.active_issues)
                )
            continue

        if c.classification == ColumnClass.CLEAN_DET:
            expressions.append(build_expression(col, c.active_issues))
        elif c.classification == ColumnClass.CLEAN_AMBIG:
            ambiguous.append(c)
        else:
            # PII, IDENTIFIER, FREE_TEXT, STRUCTURAL, OBSERVE -> passthrough.
            expressions.append(build_passthrough(col))

    if ambiguous:
        if disable_llm:
            expressions.extend(_deterministic_fallback(c) for c in ambiguous)
        else:
            expressions.extend(resolve_ambiguous(
                ambiguous, column_overrides=column_overrides,
                llm_provider=llm_provider, llm_model=llm_model, api_key=api_key, debug=debug,
            ))

    if expression_patch is not None:
        cols_by_name = {col.name: col for col in metadata.columns}
        expressions = [
            expression_patch(e, cols_by_name[e.col_name]) if e.col_name in cols_by_name else e
            for e in expressions
        ]

    # Restore original column order for the SELECT.
    col_order = {col.name: i for i, col in enumerate(metadata.columns)}
    expressions.sort(key=lambda e: col_order.get(e.col_name, len(col_order)))

    cleaning_sql = build_select(metadata.table_name, expressions)
    cls_summary = summary(classified)
    explanation = build_audit_log(expressions, cls_summary)

    columns_transformed = [e.col_name for e in expressions if e.source != "passthrough"]

    script = CleaningScript(
        table_name=metadata.table_name,
        duckdb_sql=cleaning_sql,
        explanation=explanation,
        columns_transformed=columns_transformed,
        source=_infer_script_source(expressions),
        clarification_questions=_extract_clarifications(expressions),
    )
    if debug:
        debug.code("Assembled cleaning script", script.model_dump(), lang="json")
        debug.section("Audit log", explanation)
    return script
