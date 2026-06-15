"""Stage 0 acceptance scenarios 1-8 (process.md §13, step 15).

Each scenario exercises one layer end-to-end. Tests parametrised on
``source_uri`` run against BOTH SQLite and (when available) PostgreSQL sources.
"""
from __future__ import annotations

import uuid

import duckdb
import pytest

from app.db import session_scope
from app.llm_engine import register_provider
from app.memory_engine import read_memory, write_memory
from app.models import ColdStartProgress, SyncState
from app.preprocessing import cache_engine
from app.preprocessing.ast_validator import SQLValidationError, validate_cleaning_sql
from app.preprocessing.connector import get_table_metadata
from app.preprocessing.dry_run import run_dry_run
from app.preprocessing.column_classifier import needs_sample, pre_classify, post_classify
from app.preprocessing.expression_builder import build_expression, build_passthrough
from app.preprocessing.llm_resolver import resolve_ambiguous
from app.preprocessing.models import CleaningScript, ClassifiedColumn, ColumnClass, ColumnMetadata
from app.preprocessing.profiler import build_cleaning_script, profile_table
from app.preprocessing.sampler import detect_column_issues, extract_stratified_sample
from app.preprocessing.sql_assembler import build_select
from tests.fixtures import build_source


def _tbl() -> str:
    return f"sales_{uuid.uuid4().hex[:8]}"


def _clean_sql(table: str) -> str:
    return f"""
    SELECT id,
      CASE WHEN TRIM(amount) IN ('N/A','-','') THEN NULL
           ELSE TRY_CAST(REGEXP_REPLACE(amount,'[^0-9.]','','g') AS DOUBLE) END AS amount,
      CASE WHEN LOWER(TRIM(region)) IN ('n/a','na','null','none','-','') THEN NULL
           ELSE region END AS region,
      discount, price_str, order_date, created_at, updated_at
    FROM {table}"""


# --------------------------------------------------------------------------
# Scenario 1 — Connector: metadata + sync-mode detection (multi-dialect)
# --------------------------------------------------------------------------
def test_scenario_1_connector_metadata(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=300, with_updated=True)
    md = get_table_metadata(source_uri, table)
    assert md.row_count == 300
    assert md.primary_key_column == "id"
    assert md.detected_sync_mode == "upsert"  # pk + updated_at
    assert md.change_tracking_column == "updated_at"
    names = {c.name for c in md.columns}
    assert {"amount", "discount", "region", "updated_at"} <= names


def test_scenario_1b_delete_aware(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=200, with_updated=True, with_deleted=True)
    md = get_table_metadata(source_uri, table)
    assert md.detected_sync_mode == "delete_aware"
    assert any(c.has_deleted_at for c in md.columns)


# --------------------------------------------------------------------------
# Scenario 2 — Sampler: stratification + issue detection (all issue types)
# --------------------------------------------------------------------------
def test_scenario_2_sampler_issues(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=500)
    md = get_table_metadata(source_uri, table)
    sample = extract_stratified_sample(source_uri, md)
    assert not sample.empty and len(sample) <= 1000
    issues = {c.name: detect_column_issues(sample, c) for c in md.columns}
    assert "currency_string" in issues["amount"]
    assert "percentage_string" in issues["discount"]
    assert "mixed_date_format" in issues["order_date"]
    assert "null_variant" in issues["region"]
    assert issues["id"] == []


# --------------------------------------------------------------------------
# Scenario 3 — Profiler: metadata enriched with sample values + issues
# --------------------------------------------------------------------------
def test_scenario_3_profiler(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=300)
    md, sample = profile_table(source_uri, table)
    amount = next(c for c in md.columns if c.name == "amount")
    assert "currency_string" in amount.inferred_issues
    assert len(amount.sample_values) > 0
    assert all(isinstance(v, str) for v in amount.sample_values)


# --------------------------------------------------------------------------
# Scenario 4 — v3.0 cleaning-script builder: focused LLM resolver + fallback
# --------------------------------------------------------------------------
def test_scenario_4_cleaning_script_llm(sqlite_uri):
    table = _tbl()
    build_source(sqlite_uri, table, n_rows=200)
    md, sample = profile_table(sqlite_uri, table)

    def fake(prompt, model, api_key, temperature, timeout):
        import json
        # order_date / created_at / updated_at are CLEAN_AMBIG (no
        # column-wide slash/dash date format detected) and go to the focused
        # resolver. Resolve each with a simple TRY_CAST passthrough-to-timestamp.
        resolutions = [
            {
                "column": col,
                "action": "resolve",
                "output_names": [col],
                "sql_exprs": [f'TRY_CAST("{col}" AS TIMESTAMP)'],
            }
            for col in ("order_date", "created_at", "updated_at")
        ]
        return json.dumps({"resolutions": resolutions})

    register_provider("groq", fake)
    script = build_cleaning_script(md, sample, llm_provider="groq", api_key="dummy")
    assert script.source == "llm"
    # amount (currency_string, CLEAN_DET) is always cleaned regardless of the LLM.
    assert "amount" in script.columns_transformed
    assert "order_date" in script.columns_transformed
    validate_cleaning_sql(script.duckdb_sql, table)  # must pass safety check


def test_scenario_4b_cleaning_script_fallback(sqlite_uri):
    table = _tbl()
    build_source(sqlite_uri, table, n_rows=100)
    md, sample = profile_table(sqlite_uri, table)

    # Force the LLM to be unavailable (independent of any real API key/quota in
    # the environment) by registering a provider that always raises.
    def _always_fail(prompt, model, api_key, temperature, timeout):
        raise RuntimeError("simulated LLM outage")

    register_provider("failtest", _always_fail)

    # When the LLM resolver is down, CLEAN_AMBIG columns (order_date etc.) get
    # their deterministic fallback expression (source="llm_fallback_det"),
    # which marks the whole script "deterministic_fallback". The CLEAN_DET
    # `amount` column (currency_string) is still cleaned regardless — it
    # never depended on the LLM in the first place.
    script = build_cleaning_script(md, sample, llm_provider="failtest", api_key="x")
    assert script.source == "deterministic_fallback"
    assert "amount" in script.columns_transformed
    validate_cleaning_sql(script.duckdb_sql, table)


# --------------------------------------------------------------------------
# Scenario 5 — AST validator: accept SELECT, block everything destructive
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad_sql", [
    "DELETE FROM sales",
    "DROP TABLE sales",
    "UPDATE sales SET a=1",
    "INSERT INTO sales VALUES (1)",
    "TRUNCATE sales",
    "SELECT * FROM other_table",
    "SELECT * FROM sales; DROP TABLE sales",
    "",
    "SELECT * FRM sales",
])
def test_scenario_5_ast_blocks(bad_sql):
    with pytest.raises(SQLValidationError):
        validate_cleaning_sql(bad_sql, "sales")


def test_scenario_5b_ast_accepts():
    assert validate_cleaning_sql("SELECT a, TRY_CAST(b AS DOUBLE) AS b FROM sales", "sales")


# --------------------------------------------------------------------------
# Scenario 6 — Dry run: safe passthrough vs. row-drop / column-drop / null-spike
# --------------------------------------------------------------------------
def test_scenario_6_dry_run(sqlite_uri):
    table = _tbl()
    build_source(sqlite_uri, table, n_rows=300)
    md, sample = profile_table(sqlite_uri, table)

    def script(sql, cols=None):
        return CleaningScript(table_name=table, duckdb_sql=sql, explanation="",
                              columns_transformed=cols or [])

    safe = run_dry_run(script(f"SELECT * FROM {table}"), sample)
    assert safe.safe_to_lock and safe.row_count_before == safe.row_count_after

    rowdrop = run_dry_run(script(f"SELECT * FROM {table} WHERE region='North'"), sample)
    assert not rowdrop.safe_to_lock
    assert any("Row count changed" in w for w in rowdrop.warnings)

    coldrop = run_dry_run(script(f"SELECT id, amount FROM {table}"), sample)
    assert not coldrop.safe_to_lock
    assert any("missing from output" in w for w in coldrop.warnings)


# --------------------------------------------------------------------------
# Scenario 7 — Cold start (single chunk, integer PK): cache + reconciliation
# --------------------------------------------------------------------------
def test_scenario_7_cold_start(source_uri):
    table = _tbl()
    build_source(source_uri, table, n_rows=800)
    md = get_table_metadata(source_uri, table)
    project = f"proj_{uuid.uuid4().hex[:8]}"
    script = CleaningScript(table_name=table, duckdb_sql=_clean_sql(table),
                            explanation="locked", columns_transformed=["amount", "region"],
                            source="llm_locked")
    with session_scope() as s:
        result = cache_engine.run_cold_start(project, source_uri, md, script, s)
    assert result["status"] == "completed"
    assert result["rows_cached"] == 800
    assert result["reconciliation_warning"] is None

    con = duckdb.connect(cache_engine.get_project_duckdb_path(project))
    n = con.execute(f'SELECT COUNT(*) FROM "clean_cache_{table}"').fetchone()[0]
    typ = con.execute(f'SELECT typeof(amount) FROM "clean_cache_{table}" WHERE amount IS NOT NULL LIMIT 1').fetchone()[0]
    con.close()
    assert n == 800 and typ == "DOUBLE"

    with session_scope() as s:
        ss = s.query(SyncState).filter_by(project_id=project, table_name=table).first()
        assert ss.status == "completed" and ss.last_row_count == 800


# --------------------------------------------------------------------------
# Scenario 8 — Multi-chunk PK + offset(no-PK) path + memory lock + idempotency
# --------------------------------------------------------------------------
def test_scenario_8_multichunk_offset_memory(sqlite_uri, monkeypatch):
    table = _tbl()
    build_source(sqlite_uri, table, n_rows=500)
    md = get_table_metadata(sqlite_uri, table)
    script = CleaningScript(table_name=table, duckdb_sql=_clean_sql(table),
                            explanation="locked", columns_transformed=["amount", "region"],
                            source="llm_locked")

    # Force tiny chunks -> exercises INSERT-into-staging across many chunks.
    monkeypatch.setattr(cache_engine, "CHUNK_SIZE", 100)

    project = f"proj_{uuid.uuid4().hex[:8]}"
    with session_scope() as s:
        r1 = cache_engine.run_cold_start(project, sqlite_uri, md, script, s)
    assert r1["rows_cached"] == 500
    with session_scope() as s:
        cp = s.query(ColdStartProgress).filter_by(project_id=project, table_name=table).first()
        assert cp.total_chunks >= 5  # 500/100

    # Re-run is idempotent (rebuild + atomic swap), not a duplicate.
    with session_scope() as s:
        r2 = cache_engine.run_cold_start(project, sqlite_uri, md, script, s)
    assert r2["rows_cached"] == 500

    # Offset path: force PK=None so chunking falls back to LIMIT/OFFSET.
    md_no_pk = md.model_copy(deep=True)
    md_no_pk.primary_key_column = None
    project2 = f"proj_{uuid.uuid4().hex[:8]}"
    with session_scope() as s:
        r3 = cache_engine.run_cold_start(project2, sqlite_uri, md_no_pk, script, s)
    assert r3["rows_cached"] == 500

    # memory_engine: lock the script, confirm idempotent upsert.
    with session_scope() as s:
        write_memory(s, project_id=project, domain="Business_Logic",
                     topic=f"cleaning_script_{table}", content=script.duckdb_sql)
        write_memory(s, project_id=project, domain="Business_Logic",
                     topic=f"cleaning_script_{table}", content=script.duckdb_sql)
        locked = read_memory(s, project, "Business_Logic", f"cleaning_script_{table}")
        assert locked == script.duckdb_sql
        from app.models import AgentMemory
        cnt = s.query(AgentMemory).filter_by(project_id=project, topic=f"cleaning_script_{table}").count()
        assert cnt == 1  # upsert, not duplicate


# --------------------------------------------------------------------------
# Scenario 9 — Column Intelligence Gate: pre-classification (metadata-only)
# --------------------------------------------------------------------------
def test_scenario_9_pre_classify_pii_and_identifier():
    pk = ColumnMetadata(name="id", declared_type="INTEGER", is_primary_key=True,
                         null_pct=0.0, distinct_count=300)
    email = ColumnMetadata(name="customer_email", declared_type="VARCHAR",
                            null_pct=0.0, distinct_count=300)
    fk = ColumnMetadata(name="customer_id", declared_type="INTEGER",
                         null_pct=0.0, distinct_count=300)

    pk_c = pre_classify(pk)
    email_c = pre_classify(email)
    fk_c = pre_classify(fk)

    assert pk_c.classification == ColumnClass.IDENTIFIER
    assert email_c.classification == ColumnClass.PII
    assert fk_c.classification == ColumnClass.IDENTIFIER
    # SKIP classes never need a sample.
    assert not needs_sample(pk_c)
    assert not needs_sample(email_c)
    assert not needs_sample(fk_c)


def test_scenario_9b_pre_classify_boolean_routes_to_skip():
    flag = ColumnMetadata(name="is_active", declared_type="BOOLEAN",
                           null_pct=0.0, distinct_count=2)
    c = pre_classify(flag)
    # Declared-BOOLEAN columns must never reach TRIM/expression-building
    # (root fix for the v2.0 trim(BOOLEAN) binder crash).
    assert c.classification == ColumnClass.OBSERVE
    assert c.reasons == ["declared type is already BOOLEAN"]
    assert not needs_sample(c)


def test_scenario_9c_pre_classify_generic_string_needs_sample():
    region = ColumnMetadata(name="region", declared_type="VARCHAR",
                             null_pct=0.0, distinct_count=4)
    c = pre_classify(region)
    assert c.classification == ColumnClass.OBSERVE
    assert needs_sample(c)


def test_scenario_9d_post_classify_high_cardinality_free_text():
    # FREE_TEXT is only determined post-sampling (needs inferred_issues==[]),
    # so pre_classify marks it OBSERVE/pending and it does get sampled...
    notes = ColumnMetadata(name="notes", declared_type="VARCHAR",
                            null_pct=0.0, distinct_count=980, distinct_sample_ratio=0.98)
    assert needs_sample(pre_classify(notes))
    # ...but once the sample shows no data-changing issues, post_classify
    # upgrades the high-cardinality text column to FREE_TEXT.
    c = post_classify(notes)
    assert c.classification == ColumnClass.FREE_TEXT


# --------------------------------------------------------------------------
# Scenario 10 — Column Intelligence Gate: post-classification (after sampling)
# --------------------------------------------------------------------------
def test_scenario_10_post_classify_clean_det_vs_ambig(sqlite_uri):
    table = _tbl()
    build_source(sqlite_uri, table, n_rows=300)
    md, sample = profile_table(sqlite_uri, table)

    by_name = {col.name: post_classify(col) for col in md.columns}

    # amount: single-currency currency_string -> deterministic.
    assert by_name["amount"].classification == ColumnClass.CLEAN_DET
    assert "currency_string" in by_name["amount"].active_issues

    # order_date: mixed_date_format with no detectable column-wide format
    # -> genuinely ambiguous -> sent to the focused LLM resolver.
    assert by_name["order_date"].classification == ColumnClass.CLEAN_AMBIG

    # id: primary key -> always passthrough, never touched.
    assert by_name["id"].classification == ColumnClass.IDENTIFIER


# --------------------------------------------------------------------------
# Scenario 11 — Expression builder: deterministic SQL per issue type
# --------------------------------------------------------------------------
def test_scenario_11_build_expression_currency():
    col = ColumnMetadata(name="amount", declared_type="VARCHAR", null_pct=0.0,
                          distinct_count=100, sample_values=["$1,234.50", "$99.00", "N/A"])
    expr = build_expression(col, ["currency_string", "null_variant"])
    assert expr.col_name == "amount"
    assert expr.output_names == ["amount"]
    assert expr.source == "deterministic"
    sql = expr.sql_exprs[0]
    assert '"amount"' in sql
    # Build a full SELECT and confirm it's valid DuckDB SQL against a real table.
    select_sql = build_select("t", [expr])
    con = duckdb.connect()
    con.execute('CREATE TABLE t (amount VARCHAR)')
    con.execute("INSERT INTO t VALUES ('$1,234.50'), ('$99.00'), ('N/A')")
    rows = con.execute(select_sql.replace("FROM t", 'FROM t')).fetchall()
    con.close()
    assert rows[0][0] == 1234.50
    assert rows[2][0] is None


def test_scenario_11b_build_expression_preserves_zero_padded_codes():
    # Zero-padded numeric-looking codes ("00123") must stay text, not become 123.
    col = ColumnMetadata(name="zip_code", declared_type="VARCHAR", null_pct=0.0,
                          distinct_count=100, sample_values=["00123", "00456", "07890"])
    expr = build_expression(col, ["numeric_as_string"])
    select_sql = build_select("t", [expr])
    con = duckdb.connect()
    con.execute('CREATE TABLE t (zip_code VARCHAR)')
    con.execute("INSERT INTO t VALUES ('00123'), ('00456')")
    rows = con.execute(select_sql).fetchall()
    con.close()
    assert rows[0][0] == "00123"
    assert rows[1][0] == "00456"


def test_scenario_11c_build_passthrough():
    col = ColumnMetadata(name="region", declared_type="VARCHAR", null_pct=0.0, distinct_count=4)
    expr = build_passthrough(col)
    assert expr.source == "passthrough"
    assert expr.output_names == ["region"]
    assert expr.issues_handled == []


# --------------------------------------------------------------------------
# Scenario 12 — SQL assembler: mixed passthrough / cleaned / split columns
# --------------------------------------------------------------------------
def test_scenario_12_build_select_mixed_expressions():
    from app.preprocessing.models import ColumnExpression

    exprs = [
        ColumnExpression(col_name="id", output_names=["id"], sql_exprs=['"id"'], source="passthrough"),
        ColumnExpression(col_name="amount", output_names=["amount"],
                          sql_exprs=['TRY_CAST("amount" AS DOUBLE)'], source="deterministic"),
        ColumnExpression(
            col_name="price", output_names=["price_amount", "price_currency"],
            sql_exprs=['TRY_CAST("price" AS DOUBLE)', "REGEXP_EXTRACT(\"price\", '[$]')"],
            source="llm",
        ),
    ]
    sql = build_select("orders", exprs)
    assert '"id"' in sql
    assert 'AS "amount"' in sql
    assert 'AS "price_amount"' in sql
    assert 'AS "price_currency"' in sql
    assert "FROM" in sql and "orders" in sql

    con = duckdb.connect()
    con.execute('CREATE TABLE orders (id INTEGER, amount VARCHAR, price VARCHAR)')
    con.execute("INSERT INTO orders VALUES (1, '12.5', '$10')")
    cols = [d[0] for d in con.execute(sql).description]
    con.close()
    assert cols == ["id", "amount", "price_amount", "price_currency"]


# --------------------------------------------------------------------------
# Scenario 13 — LLM resolver: guaranteed fallback never raises
# --------------------------------------------------------------------------
def test_scenario_13_resolve_ambiguous_empty_list():
    assert resolve_ambiguous([]) == []


def test_scenario_13b_resolve_ambiguous_fallback_on_exception():
    col = ColumnMetadata(name="order_date", declared_type="VARCHAR", null_pct=0.0,
                          distinct_count=200, sample_values=["2021-01-05", "Jan 5, 2021"],
                          inferred_issues=["mixed_date_format"])
    classified = ClassifiedColumn(col, ColumnClass.CLEAN_AMBIG, ["mixed_date_format"],
                                   ["mixed_date_format"])

    def _boom(prompt, model, api_key, temperature, timeout):
        raise RuntimeError("provider down")

    register_provider("boomtest", _boom)
    exprs = resolve_ambiguous([classified], llm_provider="boomtest", api_key="x")
    assert len(exprs) == 1
    assert exprs[0].source == "llm_fallback_det"
    assert exprs[0].col_name == "order_date"


def test_scenario_13c_apply_clarification_answer_keep_as_text():
    from app.preprocessing.llm_resolver import apply_clarification_answer
    col = ColumnMetadata(name="discount", declared_type="VARCHAR", null_pct=0.0, distinct_count=30)
    expr = apply_clarification_answer(col, "Leave this column completely unchanged, as text")
    assert expr.source == "passthrough"
    assert expr.output_names == ["discount"]


# --------------------------------------------------------------------------
# Scenario 14 — End-to-end: PII column never profiled / never sent to LLM
# --------------------------------------------------------------------------
def test_scenario_14_pii_column_excluded_from_profiling_and_llm(sqlite_uri):
    table = f"customers_{uuid.uuid4().hex[:8]}"
    import sqlite3
    sconn = sqlite3.connect(sqlite_uri.replace("sqlite:///", ""))
    cur = sconn.cursor()
    cur.execute(
        f"CREATE TABLE {table} ("
        "id INTEGER PRIMARY KEY, customer_email TEXT, region TEXT)"
    )
    for i in range(50):
        cur.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?)",
            (i, f"user{i}@example.com", "North" if i % 2 == 0 else "South "),
        )
    sconn.commit()
    sconn.close()

    md, sample = profile_table(sqlite_uri, table)

    # customer_email is present in the raw in-memory sample (dry_run needs the
    # full row shape to execute the assembled SELECT, which still passes the
    # PII column through), but it is never profiled — no sample_values or
    # inferred_issues are derived from it, so the LLM resolver never sees it.
    assert "customer_email" in sample.columns
    email_col = next(c for c in md.columns if c.name == "customer_email")
    assert email_col.sample_values == []
    assert email_col.inferred_issues == []

    script = build_cleaning_script(md, sample)
    assert "customer_email" not in script.columns_transformed
    assert '"customer_email"' in script.duckdb_sql  # still selected, passthrough
    diff = run_dry_run(script, sample)
    assert diff.safe_to_lock


# --------------------------------------------------------------------------
# Scenario 15 — End-to-end: zero CLEAN_AMBIG columns -> LLM never invoked
# --------------------------------------------------------------------------
def test_scenario_15_no_ambiguous_columns_skips_llm(sqlite_uri):
    table = f"simple_{uuid.uuid4().hex[:8]}"
    import sqlite3
    sconn = sqlite3.connect(sqlite_uri.replace("sqlite:///", ""))
    cur = sconn.cursor()
    cur.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, status TEXT)")
    for i in range(50):
        cur.execute(f"INSERT INTO {table} VALUES (?, ?)", (i, "active" if i % 2 == 0 else "inactive"))
    sconn.commit()
    sconn.close()

    md, sample = profile_table(sqlite_uri, table)

    def _should_not_be_called(prompt, model, api_key, temperature, timeout):
        raise AssertionError("LLM resolver was called despite zero CLEAN_AMBIG columns")

    register_provider("nocalltest", _should_not_be_called)
    script = build_cleaning_script(md, sample, llm_provider="nocalltest", api_key="x")
    assert script.source == "deterministic"
    validate_cleaning_sql(script.duckdb_sql, table)


# --------------------------------------------------------------------------
# Scenario 16 — Stage 0.5: Cross-Table Consistency Layer (stage0_v3_spec.md)
# --------------------------------------------------------------------------

def _make_table(table_name: str, columns: list[ColumnMetadata], row_count: int) -> "TableMetadata":
    from app.preprocessing.models import TableMetadata
    return TableMetadata(
        table_name=table_name, row_count=row_count, columns=columns,
        detected_sync_mode="full_resync",
    )


def test_scenario_16a_phone_format_signature():
    from app.preprocessing.profiler import _phone_format_signature

    assert _phone_format_signature(["9876543210", "9123456780"]) == "local_10d"
    assert _phone_format_signature(["+91-9876543210", "+91-9123456780"]) == "intl_12d"
    assert _phone_format_signature([]) is None


def test_scenario_16b_date_format_signature():
    import pandas as pd
    from app.preprocessing.profiler import _date_format_signature

    # Native timestamp type -> always "native_timestamp".
    col_ts = ColumnMetadata(name="created_at", declared_type="TIMESTAMP")
    assert _date_format_signature(col_ts, pd.Series([], dtype=object)) == "native_timestamp"

    # mixed_date_format with a determined column-wide format.
    col_mixed = ColumnMetadata(
        name="order_date", declared_type="VARCHAR",
        inferred_issues=["mixed_date_format"], date_format="%d/%m/%Y",
    )
    assert _date_format_signature(col_mixed, pd.Series(["25/01/2021"])) == "%d/%m/%Y"

    # ISO-formatted string date column.
    col_iso = ColumnMetadata(name="signup_date", declared_type="VARCHAR")
    iso_series = pd.Series(["2021-01-05", "2021-02-06", "2021-03-07"])
    assert _date_format_signature(col_iso, iso_series) == "%Y-%m-%d"

    # Non-date column name -> no signature.
    col_other = ColumnMetadata(name="amount", declared_type="VARCHAR")
    assert _date_format_signature(col_other, pd.Series(["100", "200"])) is None


def test_scenario_16c_find_groups_dates_majority_rule():
    from app.preprocessing.cross_table_consistency import find_groups

    # Table A: 100 rows, no determinable format ("ambiguous"). Tables B and C
    # both have a determinable format -> after v3.0 cleaning both end up as a
    # native TIMESTAMP, so the canonical format is "native_timestamp" and only
    # table A (still "ambiguous" after cleaning) needs flagging.
    table_a = _make_table("orders", [
        ColumnMetadata(name="order_date", declared_type="VARCHAR",
                       inferred_issues=["mixed_date_format"], date_format=None,
                       format_signature="ambiguous"),
    ], row_count=100)
    table_b = _make_table("invoices", [
        ColumnMetadata(name="invoice_date", declared_type="VARCHAR", format_signature="%Y-%m-%d"),
    ], row_count=300)
    table_c = _make_table("shipments", [
        ColumnMetadata(name="ship_date", declared_type="VARCHAR", format_signature="%Y-%m-%d"),
    ], row_count=50)

    groups = find_groups({"orders": table_a, "invoices": table_b, "shipments": table_c})

    date_groups = [g for g in groups if g.group_type == "date"]
    assert len(date_groups) == 1
    g = date_groups[0]
    assert g.canonical_format == "native_timestamp"
    assert g.tables_needing_patch == ["orders"]
    assert set(g.tables_matching) == {"invoices", "shipments"}


def test_scenario_16d_find_groups_phone_tie_break_prefers_intl():
    from app.preprocessing.cross_table_consistency import find_groups

    table_a = _make_table("customers", [
        ColumnMetadata(name="phone", declared_type="VARCHAR", format_signature="local_10d"),
    ], row_count=100)
    table_b = _make_table("leads", [
        ColumnMetadata(name="phone", declared_type="VARCHAR", format_signature="intl_12d"),
    ], row_count=100)

    groups = find_groups({"customers": table_a, "leads": table_b})
    phone_groups = [g for g in groups if g.group_type == "phone"]
    assert len(phone_groups) == 1
    assert phone_groups[0].canonical_format == "intl_12d"
    assert phone_groups[0].tables_needing_patch == ["customers"]


def test_scenario_16e_find_groups_id_native_numeric_wins():
    from app.preprocessing.cross_table_consistency import find_groups

    table_a = _make_table("customers", [
        ColumnMetadata(name="customer_id", declared_type="UUID", is_primary_key=True, format_signature="alnum"),
    ], row_count=100)
    table_b = _make_table("orders", [
        ColumnMetadata(name="customer_id", declared_type="INTEGER", format_signature="numeric"),
    ], row_count=1000)

    groups = find_groups({"customers": table_a, "orders": table_b})
    id_groups = [g for g in groups if g.group_type == "id"]
    assert len(id_groups) == 1
    g = id_groups[0]
    # A native numeric declared_type anywhere in the group is sufficient
    # evidence the group is numeric-natured (Alphanumeric ID Guard).
    assert g.canonical_format == "numeric"
    assert "native numeric declared type" in g.canonical_reason
    # Every member gets patched to the canonical text form.
    assert set(g.tables_needing_patch) == {"customers", "orders"}
    assert g.tables_matching == []


def test_scenario_16e2_find_groups_id_all_varchar_letters_present():
    from app.preprocessing.cross_table_consistency import find_groups

    table_a = _make_table("customers", [
        ColumnMetadata(name="customer_id", declared_type="VARCHAR", is_primary_key=True, format_signature="alnum"),
    ], row_count=100)
    table_b = _make_table("orders", [
        ColumnMetadata(name="customer_id", declared_type="VARCHAR", format_signature="numeric"),
    ], row_count=1000)

    groups = find_groups({"customers": table_a, "orders": table_b})
    id_groups = [g for g in groups if g.group_type == "id"]
    assert len(id_groups) == 1
    g = id_groups[0]
    assert g.canonical_format == "alnum"
    assert "letters found" in g.canonical_reason


def test_scenario_16f_find_groups_conservative_single_table_no_group():
    from app.preprocessing.cross_table_consistency import find_groups

    table_a = _make_table("orders", [
        ColumnMetadata(name="order_date", declared_type="VARCHAR", format_signature="%d/%m/%Y"),
    ], row_count=100)

    assert find_groups({"orders": table_a}) == []


def test_scenario_16g_make_patcher_date_leaves_ambiguous_with_note():
    from app.preprocessing.cross_table_consistency import find_groups, make_patcher
    from app.preprocessing.expression_builder import build_passthrough

    table_a = _make_table("orders", [
        ColumnMetadata(name="order_date", declared_type="VARCHAR",
                       inferred_issues=["mixed_date_format"], date_format=None,
                       format_signature="ambiguous"),
    ], row_count=100)
    table_b = _make_table("invoices", [
        ColumnMetadata(name="invoice_date", declared_type="VARCHAR", format_signature="%Y-%m-%d"),
    ], row_count=300)

    groups = find_groups({"orders": table_a, "invoices": table_b})
    patcher = make_patcher("orders", groups, table_a)
    assert patcher is not None

    col = table_a.columns[0]
    expr = build_passthrough(col)
    expr.sql_exprs = ['"order_date"']  # simulate the existing v3.0-produced expression
    patched = patcher(expr, col)

    # No determinable format -> SQL is left unchanged, but a note is added
    # for the review UI.
    assert patched.sql_exprs == expr.sql_exprs
    assert any("cross_table_alignment_needed" in i for i in patched.issues_handled)

    # invoices doesn't need patching.
    assert make_patcher("invoices", groups, table_b) is None


def test_scenario_16h_make_patcher_id_numeric_zero_strip():
    from app.preprocessing.cross_table_consistency import find_groups, make_patcher
    from app.preprocessing.expression_builder import build_passthrough

    # Both members are numeric-natured -> canonical "numeric"; the VARCHAR
    # member with leading zeros needs zero-stripping via the safe regex.
    table_a = _make_table("customers", [
        ColumnMetadata(name="customer_id", declared_type="INTEGER", is_primary_key=True, format_signature="numeric"),
    ], row_count=100)
    table_b = _make_table("orders", [
        ColumnMetadata(name="customer_id", declared_type="VARCHAR", format_signature="numeric"),
    ], row_count=1000)

    groups = find_groups({"customers": table_a, "orders": table_b})
    patcher = make_patcher("orders", groups, table_b)
    assert patcher is not None

    col = table_b.columns[0]
    expr = build_passthrough(col)
    patched = patcher(expr, col)
    assert "REGEXP_REPLACE(" in patched.sql_exprs[0]
    assert "'^0+(?=[0-9])'" in patched.sql_exprs[0]
    assert "TRIM(CAST(" in patched.sql_exprs[0]


def test_scenario_16h2_make_patcher_id_alnum_member_skips_zero_strip():
    from app.preprocessing.cross_table_consistency import find_groups, make_patcher
    from app.preprocessing.expression_builder import build_passthrough

    # customers.customer_id is a native numeric type -> group canonical is
    # "numeric", but orders.customer_id is alnum (UUID/hash-like) — the hard
    # rule says it must NOT have leading zeros stripped, only trim/cast.
    table_a = _make_table("customers", [
        ColumnMetadata(name="customer_id", declared_type="INTEGER", is_primary_key=True, format_signature="numeric"),
    ], row_count=100)
    table_b = _make_table("orders", [
        ColumnMetadata(name="customer_id", declared_type="VARCHAR", format_signature="alnum"),
    ], row_count=1000)

    groups = find_groups({"customers": table_a, "orders": table_b})
    patcher = make_patcher("orders", groups, table_b)
    assert patcher is not None

    col = table_b.columns[0]
    expr = build_passthrough(col)
    patched = patcher(expr, col)
    assert patched.sql_exprs[0] == f"TRIM(CAST(({expr.sql_exprs[0]}) AS VARCHAR))"
    assert "REGEXP_REPLACE" not in patched.sql_exprs[0]
    assert any("leading-zero stripping was NOT applied" in note for note in patched.issues_handled)


def test_scenario_16i_build_summary_shape():
    from app.preprocessing.cross_table_consistency import build_summary, find_groups

    table_a = _make_table("customers", [
        ColumnMetadata(name="phone", declared_type="VARCHAR", format_signature="local_10d"),
    ], row_count=100)
    table_b = _make_table("leads", [
        ColumnMetadata(name="phone", declared_type="VARCHAR", format_signature="intl_12d"),
    ], row_count=100)

    groups = find_groups({"customers": table_a, "leads": table_b})
    summary = build_summary(groups)
    assert summary
    entry = summary[0]
    assert set(entry) == {
        "group_type", "label", "canonical_format", "canonical_reason",
        "tables_matching", "tables_needing_patch",
    }
