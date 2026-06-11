"""Stage 0 — dry-run preview + Data Quality Diff (process.md §8).

Executes the cleaning SQL against the in-memory sample (never the live DB) and
compares before/after on row count, per-column null counts, and dtypes. The
result drives the user-facing "Data Quality Diff" and the ``safe_to_lock`` gate:
the SQL cannot be locked if it drops rows, drops columns, or spikes nulls.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from app.config import get_settings
from app.preprocessing.models import CleaningScript, DataQualityDiff

NULL_SPIKE_THRESHOLD = get_settings().PREPROCESSING_NULL_SPIKE_THRESHOLD


def run_dry_run(script: CleaningScript, sample: pd.DataFrame) -> DataQualityDiff:
    table_name = script.table_name
    warnings: list[str] = []

    if sample.empty:
        return DataQualityDiff(
            table_name=table_name, row_count_before=0, row_count_after=0,
            column_diffs=[], warnings=["Sample is empty; cannot dry-run."],
            safe_to_lock=False,
        )

    before_row_count = len(sample)
    before_nulls = {col: int(sample[col].isna().sum()) for col in sample.columns}
    before_types = {col: str(sample[col].dtype) for col in sample.columns}

    con = duckdb.connect()
    try:
        con.register(table_name, sample)
        result_df = con.execute(script.duckdb_sql).df()
    except Exception as e:
        return DataQualityDiff(
            table_name=table_name, row_count_before=before_row_count, row_count_after=0,
            column_diffs=[], warnings=[f"Dry-run failed: {e}"], safe_to_lock=False,
        )
    finally:
        con.close()

    after_row_count = len(result_df)
    after_nulls = {col: int(result_df[col].isna().sum()) for col in result_df.columns}
    after_types = {col: str(result_df[col].dtype) for col in result_df.columns}

    safe_to_lock = True
    if after_row_count != before_row_count:
        warnings.append(
            f"Row count changed ({before_row_count} -> {after_row_count}). "
            f"Cleaning SQL must not drop rows."
        )
        safe_to_lock = False

    column_diffs: list[dict] = []
    for col in sample.columns:
        if col not in result_df.columns:
            warnings.append(f"Column '{col}' missing from output.")
            safe_to_lock = False
            continue

        null_before = before_nulls.get(col, 0)
        null_after = after_nulls.get(col, 0)
        null_increase = (null_after - null_before) / max(before_row_count, 1)

        if null_increase > NULL_SPIKE_THRESHOLD:
            warnings.append(f"Column '{col}': nulls increased by {null_increase:.1%}.")
            safe_to_lock = False

        column_diffs.append(
            {
                "column": col,
                "null_before": null_before,
                "null_after": null_after,
                "type_before": before_types.get(col, "unknown"),
                "type_after": after_types.get(col, "unknown"),
                "transformed": col in script.columns_transformed,
            }
        )

    return DataQualityDiff(
        table_name=table_name,
        row_count_before=before_row_count,
        row_count_after=after_row_count,
        column_diffs=column_diffs,
        warnings=warnings,
        safe_to_lock=safe_to_lock,
    )
