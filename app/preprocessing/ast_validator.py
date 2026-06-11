"""Stage 0 — AST safety check for generated cleaning SQL (process.md §7).

The cleaning SQL is LLM-authored, so it must never be trusted blindly. This
validator parses it with sqlglot (DuckDB dialect) and rejects anything that is
not a single, side-effect-free SELECT over the expected source table.

Node names are pinned to the installed sqlglot (v30): there is no ``Truncate``
or ``AlterTable`` — TRUNCATE parses as ``Command`` and ALTER is ``Alter``.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

# Any of these appearing anywhere in the tree is an immediate rejection.
DESTRUCTIVE_NODE_TYPES = (
    exp.Drop,
    exp.Delete,
    exp.Insert,
    exp.Update,
    exp.Create,
    exp.Alter,
    exp.Command,  # catches TRUNCATE, COPY, VACUUM, CALL, PRAGMA, etc.
)


class SQLValidationError(Exception):
    pass


def validate_cleaning_sql(sql: str, table_name: str) -> str:
    if not sql or not sql.strip():
        raise SQLValidationError("Generated SQL is empty.")

    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except Exception as e:
        raise SQLValidationError(f"SQL could not be parsed: {e}")

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SQLValidationError(
            f"Expected exactly 1 SQL statement, got {len(statements)}."
        )

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise SQLValidationError("Cleaning SQL must be a SELECT statement.")

    for node in statement.walk():
        if isinstance(node, DESTRUCTIVE_NODE_TYPES):
            raise SQLValidationError(
                f"Destructive operation detected: {type(node).__name__}"
            )

    from_tables = [t.name.lower() for t in statement.find_all(exp.Table)]
    if from_tables and table_name.lower() not in from_tables:
        raise SQLValidationError(f"SQL references unexpected tables: {from_tables}")

    return sql.strip()
