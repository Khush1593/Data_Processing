"""Stage 0 v3.0 — assembles per-column ``ColumnExpression`` objects into the
final DuckDB cleaning ``SELECT`` (stage0_v3_spec.md §8)."""
from __future__ import annotations

from app.preprocessing.models import ColumnExpression


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build_select(
    table_name: str,
    expressions: list[ColumnExpression],
) -> str:
    """Build the final DuckDB cleaning SELECT from per-column expressions.

    ``expressions`` must already be ordered to match the source table's
    column order. Each expression contributes one output column normally, or
    two for a currency split (amount + currency code).

    The resulting SQL is always executed against a table/dataframe
    *locally registered* under ``table_name`` in an in-memory DuckDB
    connection (dry-run sample, cold-start chunk) — never against the live
    source database — so it is intentionally never schema-qualified, even
    when the source table lives in a non-default source schema (e.g.
    Postgres ``public``, which isn't a valid DuckDB schema name out of the
    box).
    """
    if not expressions:
        raise ValueError(f"No expressions provided for table '{table_name}'")

    table_ref = _quote_ident(table_name)

    parts: list[str] = []
    for expr in expressions:
        # A mismatch between expression count and output-name count means a
        # malformed ColumnExpression (e.g. the LLM declared a 2-way currency
        # split but returned only one sql_expr, or vice versa). zip() would
        # silently truncate to the shorter list and drop a declared output
        # column from the cleaning SELECT with no error — a silent data loss
        # that passes AST validation (the SQL is valid) and slips past the
        # dry-run (the dropped column is a *new* output, never in the source,
        # so the missing-column check never sees it). Fail loudly instead.
        if len(expr.sql_exprs) != len(expr.output_names):
            raise ValueError(
                f"Column '{expr.col_name}' (source={expr.source}) has "
                f"{len(expr.sql_exprs)} SQL expression(s) but "
                f"{len(expr.output_names)} output name(s): "
                f"exprs={expr.sql_exprs!r} names={expr.output_names!r}. "
                f"These must be 1:1 — refusing to assemble a SELECT that "
                f"would silently drop a declared output column."
            )
        for sql_expr, output_name in zip(expr.sql_exprs, expr.output_names):
            alias = _quote_ident(output_name)
            if sql_expr.strip() == alias:
                parts.append(f"    {sql_expr}")
            else:
                parts.append(f"    {sql_expr} AS {alias}")

    cols_sql = ",\n".join(parts)
    return f"SELECT\n{cols_sql}\nFROM {table_ref}"


def build_audit_log(
    expressions: list[ColumnExpression],
    classification_summary: dict[str, int],
    review_notes: list[str] | None = None,
) -> str:
    """Human-readable audit string for debug logs and the review UI's
    ``CleaningScript.explanation`` field.

    ``review_notes`` (Step 4 manual-review flags: insufficient sample data,
    PII-blocked exceptions, AI patch failed/unverified, multi-currency
    columns left unconverted, etc.) were previously only written to the
    debug log — never to this explanation — so a column the pipeline itself
    flagged as needing a human look was invisible to anyone not reading raw
    debug markdown. They're included here so the actual review UI surfaces
    them.
    """
    lines = [
        f"Column classification: {classification_summary}",
        "",
        f"{'Column':<30} {'Source':<18} {'Issues handled':<40}",
        "-" * 88,
    ]
    for expr in expressions:
        issues = ", ".join(expr.issues_handled) or "-"
        flag = " [CLARIFICATION NEEDED]" if expr.clarification_needed else ""
        lines.append(f"{expr.col_name:<30} {expr.source:<18} {issues:<40}{flag}")
    if review_notes:
        lines.append("")
        lines.append("Manual review needed:")
        lines.extend(f"  - {note}" for note in review_notes)
    return "\n".join(lines)
