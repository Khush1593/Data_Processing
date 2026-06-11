"""Stage 0 — metadata profiler (process.md §1 file list).

Single orchestration point that the API layer calls: it connects, extracts
structural metadata, pulls a stratified in-memory sample, and enriches each
column with its top sample values and inferred cleaning issue. Returns both the
enriched metadata and the sample DataFrame (the sample is the caller's to hold
in memory and discard once the cleaning SQL is locked).
"""
from __future__ import annotations

import pandas as pd

from app.preprocessing.connector import get_table_metadata
from app.preprocessing.models import TableMetadata
from app.preprocessing.sampler import (
    detect_column_issues,
    detect_currency_symbols,
    extract_stratified_sample,
)

SAMPLE_VALUES_PER_COLUMN = 10


def profile_table(
    db_uri: str, table_name: str, schema: str | None = None
) -> tuple[TableMetadata, pd.DataFrame]:
    """Profile a source table: structural metadata + stratified sample + issues."""
    metadata = get_table_metadata(db_uri, table_name, schema=schema)
    sample = extract_stratified_sample(db_uri, metadata)
    enrich_metadata_with_sample(metadata, sample)
    return metadata, sample


def enrich_metadata_with_sample(metadata: TableMetadata, sample: pd.DataFrame) -> TableMetadata:
    """Populate ``sample_values`` and ``inferred_issue`` for each column in place."""
    for col in metadata.columns:
        if col.name in sample.columns:
            col.sample_values = (
                sample[col.name]
                .dropna()
                .astype(str)
                .unique()[:SAMPLE_VALUES_PER_COLUMN]
                .tolist()
            )
            col.inferred_issue = detect_column_issues(sample, col)
            if col.inferred_issue == "currency_string":
                symbols = detect_currency_symbols(
                    sample[col.name].dropna().astype(str)
                )
                if len(symbols) > 1:
                    col.currency_symbols = symbols
        else:
            col.sample_values = []
            col.inferred_issue = None
    return metadata
