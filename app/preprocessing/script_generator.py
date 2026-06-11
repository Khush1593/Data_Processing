"""Stage 0 — LLM prompt builder + cleaning SQL generation (process.md §6).

Deterministic-first contract: if the LLM call fails for any reason, we return a
safe pass-through ``SELECT`` that changes nothing, marked
``source="deterministic_fallback"``. The AI writes the cleaning SQL exactly
once; after user confirmation it is locked permanently (see cache_engine).
"""
from __future__ import annotations

import re

import pandas as pd
from pydantic import BaseModel, ConfigDict

from app.llm_engine import _generate_structured
from app.preprocessing.models import CleaningScript, TableMetadata


class CleaningScriptResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    duckdb_sql: str
    explanation: str
    columns_transformed: list[str]


def _build_sample_summary(sample: pd.DataFrame, metadata: TableMetadata) -> str:
    lines = []
    for col in metadata.columns:
        if col.name not in sample.columns:
            continue
        col_series = sample[col.name].dropna().astype(str).str.strip()
        unique_vals = col_series.unique()[:10].tolist()
        lines.append(
            f"  - {col.name} ({col.declared_type}): "
            f"null_pct={col.null_pct:.1%}, "
            f"distinct={col.distinct_count}, "
            f"issue={col.inferred_issue or 'none'}, "
            f"sample_values={unique_vals}"
        )
    return "\n".join(lines)


def _build_prompt(
    metadata: TableMetadata,
    sample: pd.DataFrame,
    column_overrides: dict[str, str] | None = None,
) -> str:
    sample_summary = _build_sample_summary(sample, metadata)

    overrides_section = ""
    if column_overrides:
        lines = "\n".join(
            f"- `{col}`: {instruction}" for col, instruction in column_overrides.items()
        )
        overrides_section = f"""

## Column-Specific Overrides — Apply These BEFORE the General Rules Below
The user has reviewed the detected issues and given explicit instructions for
the following columns. These instructions take priority over the general
transformation rules for these specific columns:
{lines}
"""

    return f"""You are a senior data engineer. Your task is to write a DuckDB-dialect SQL SELECT statement that cleans and normalises the table described below.{overrides_section}

## Table: {metadata.table_name}
## Row Count: {metadata.row_count:,}

## Column Metadata + Top Unique Sample Values
{sample_summary}

## Your Task
Write a single DuckDB SELECT statement that:
1. Selects ALL columns (do not drop any columns)
2. Applies cleaning transformations ONLY to columns where an issue is detected
3. For columns with no issue, passes them through unchanged: just `column_name`

## Transformation Rules — Follow These Exactly

### Currency strings (issue: currency_string)
CASE
    WHEN TRIM(col) IN ('N/A', '-', '', 'null', 'none', 'NA') THEN NULL
    WHEN UPPER(TRIM(col)) = 'FREE' THEN 0
    WHEN col SIMILAR TO '[$€£₹¥][0-9,]+\\.?[0-9]*'
        THEN TRY_CAST(REGEXP_REPLACE(col, '[^0-9.]', '', 'g') AS DOUBLE)
    ELSE TRY_CAST(REGEXP_REPLACE(col, '[^0-9.]', '', 'g') AS DOUBLE)
END AS col

### Percentage strings (issue: percentage_string)
Detect the percent sign with strpos (DuckDB LIKE does NOT treat backslash as an
escape by default, so never write `LIKE '%\\%'`).
CASE
    WHEN TRIM(col) IN ('N/A', '-', '', 'null', 'none', 'NA') THEN NULL
    WHEN strpos(col, '%') > 0
        THEN TRY_CAST(REGEXP_REPLACE(col, '[^0-9.]', '', 'g') AS DOUBLE) / 100.0
    ELSE TRY_CAST(col AS DOUBLE)
END AS col

### Null variants (issue: null_variant)
CASE
    WHEN LOWER(TRIM(col)) IN ('n/a', 'na', 'null', 'none', '-', '–', '—', '', 'nan', '#n/a')
        THEN NULL
    ELSE col
END AS col

### Mixed date formats (issue: mixed_date_format)
Values may appear in many different formats: ISO ('2023-01-15'), slash
('15/01/2023', day-first), or written-out ('Jan 15 2023', 'April 20 2023').
Some may also be Unix epoch seconds (e.g. '1673740800') or Excel serial date
numbers (e.g. '44927', roughly between 20000 and 60000). Try every format
with COALESCE so each row is parsed by whichever format actually matches it:
COALESCE(
    TRY_CAST(col AS TIMESTAMP),
    TRY_STRPTIME(col, '%d/%m/%Y'),
    TRY_STRPTIME(col, '%m/%d/%Y'),
    TRY_STRPTIME(col, '%b %d %Y'),
    TRY_STRPTIME(col, '%B %d %Y'),
    TRY_CAST(TO_TIMESTAMP(TRY_CAST(col AS BIGINT)) AS TIMESTAMP),
    DATE '1899-12-30' + TRY_CAST(TRY_CAST(col AS DOUBLE) AS INTEGER) * INTERVAL '1 day'
) AS col

### Numeric stored as string (issue: numeric_as_string)
If the column is an ID/code with meaningful leading zeros (e.g. supplier
codes, SKUs), DO NOT cast to a numeric type — leading zeros must be
preserved as text. In that case pass it through unchanged: `col`.
Otherwise (genuine numeric measures):
TRY_CAST(REGEXP_REPLACE(col, '[^0-9.-]', '', 'g') AS DOUBLE) AS col

## Critical Rules
* Use ONLY DuckDB-compatible SQL syntax
* Use TRY_CAST (not CAST) for all type conversions
* Never use DROP, DELETE, UPDATE, INSERT
* Never remove rows (no WHERE / no LIMIT)
* Include non-issue columns as-is
* Alias every transformed column back to its ORIGINAL name
* String literals (e.g. 'null', 'N/A', '-') MUST use single quotes. NEVER use
  double quotes for string literals — in DuckDB, double quotes denote a
  COLUMN/IDENTIFIER reference, not a string. Writing "null" instead of 'null'
  will cause a binder error.

Return ONLY a JSON object with keys: duckdb_sql, explanation, columns_transformed."""


def generate_cleaning_script(
    metadata: TableMetadata,
    sample: pd.DataFrame,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    api_key: str | None = None,
    column_overrides: dict[str, str] | None = None,
) -> CleaningScript:
    """Generate the cleaning SQL via the configured LLM, with safe fallback."""
    prompt = _build_prompt(metadata, sample, column_overrides=column_overrides)
    try:
        result: CleaningScriptResponse = _generate_structured(
            prompt=prompt,
            response_schema=CleaningScriptResponse,
            provider=llm_provider,
            model=llm_model,
            api_key=api_key,
            temperature=0.1,
        )
        return CleaningScript(
            table_name=metadata.table_name,
            duckdb_sql=_sanitize_sql(result.duckdb_sql),
            explanation=result.explanation,
            columns_transformed=result.columns_transformed,
            source="llm",
        )
    except Exception as e:
        col_names = ", ".join(_quote_ident(col.name) for col in metadata.columns)
        return CleaningScript(
            table_name=metadata.table_name,
            duckdb_sql=f"SELECT {col_names} FROM {_quote_ident(metadata.table_name)}",
            explanation=(
                f"LLM generation failed ({e}). Pass-through SELECT with no "
                f"transformations applied."
            ),
            columns_transformed=[],
            source="deterministic_fallback",
        )


# Common null-sentinel tokens that LLMs occasionally emit as DuckDB
# *identifiers* (double quotes) when they meant a *string literal* (single
# quotes). None of these are realistic column names, so it is safe to
# rewrite them unconditionally.
_NULL_TOKEN_RE = re.compile(
    r'"(null|none|na|n/a|nan|#n/a|-|–|—)"', flags=re.IGNORECASE
)


def _sanitize_sql(sql: str) -> str:
    """Fix LLM mistakes that swap string-literal quotes for identifier quotes."""
    return _NULL_TOKEN_RE.sub(lambda m: "'" + m.group(1) + "'", sql)


def _quote_ident(name: str) -> str:
    """Double-quote an identifier for DuckDB, escaping embedded quotes."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'
