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
) -> str:
    """Human-readable audit string for debug logs and the review UI's
    ``CleaningScript.explanation`` field."""
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
    return "\n".join(lines)
