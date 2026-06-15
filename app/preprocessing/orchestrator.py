"""Stage 0 review UI — orchestration glue between the API and the library.

Two entry points, both designed to run inside a FastAPI ``BackgroundTasks``
worker (synchronous, single project at a time):

* :func:`analyze_project` — profile every table in the source DB/schema,
  generate + validate + dry-run a cleaning script for each, and persist a
  :class:`TableAnalysis` row per table.
* :func:`approve_and_process` — lock the (possibly user-edited) cleaning SQL
  for the requested tables and run the cold-start cache build for each.
"""
from __future__ import annotations

import asyncio

import sqlalchemy

from app.db import session_scope
from app.debug_logger import DebugLogger, new_run_id
from app.memory_engine import write_memory
from app.models import ColdStartProgress, Project, TableAnalysis
from app.preprocessing.ast_validator import SQLValidationError, validate_cleaning_sql
from app.preprocessing.cache_engine import run_cold_start
from app.preprocessing.cross_table_consistency import build_summary, find_groups, make_patcher
from app.preprocessing.dry_run import run_dry_run
from app.preprocessing.models import CleaningScript, DataQualityDiff, TableMetadata
from app.preprocessing.profiler import build_cleaning_script, profile_table
from app.preprocessing.sampler import CURRENCY_SYMBOL_TO_CODE, extract_stratified_sample


# Standard clarification options offered for a column where the sample data
# contains multiple distinct currency symbols (e.g. '$20.00', '£16.00').
CURRENCY_CLARIFICATION_OPTIONS = [
    {
        "id": "strip_assume_same",
        "label": "Strip currency symbols and treat all values as the same unit (default)",
    },
    {
        "id": "split_amount_currency",
        "label": "Split into '<column>_amount' (number) and '<column>_currency' "
                 "(currency code, e.g. USD/GBP/EUR) columns",
    },
    {
        "id": "keep_as_text",
        "label": "Leave the column as text, unchanged",
    },
]

# Always appended to every clarification so the user can override any
# AI decision with free-form guidance, even when our/the LLM's preset
# options don't cover their case.
_CUSTOM_OPTION = {
    "id": "custom",
    "label": "Other — describe how this column should be handled",
    "requires_note": True,
}


def _build_clarifications(metadata: TableMetadata, script: CleaningScript | None = None) -> list[dict]:
    """Merge deterministic (currency) clarifications with any the LLM flagged
    for this table, and append a free-form "Other" option to each so the
    user can always provide additional guidance."""
    clarifications: list[dict] = []
    seen_columns: set[str] = set()

    for col in metadata.columns:
        if col.currency_symbols:
            symbol_codes = [
                CURRENCY_SYMBOL_TO_CODE.get(s, s) for s in col.currency_symbols
            ]
            clarifications.append({
                "column": col.name,
                "question": (
                    f"Column '{col.name}' contains values in multiple currencies "
                    f"({', '.join(col.currency_symbols)} → {', '.join(symbol_codes)}, "
                    f"and/or values with no symbol). How should this be handled?"
                ),
                "options": [*CURRENCY_CLARIFICATION_OPTIONS, _CUSTOM_OPTION],
                "default": "strip_assume_same",
            })
            seen_columns.add(col.name)

    if script is not None:
        for q in script.clarification_questions:
            if q["column"] in seen_columns:
                continue
            clarifications.append({
                **q,
                "options": [*q["options"], _CUSTOM_OPTION],
            })
            seen_columns.add(q["column"])

    return clarifications


def _column_override_for_answer(
    column: str,
    option_id: str,
    note: str | None = None,
    clarification: dict | None = None,
) -> str | None:
    if option_id == "split_amount_currency":
        symbol_map = ", ".join(
            f"{sym}→{code}" for sym, code in CURRENCY_SYMBOL_TO_CODE.items()
        )
        return (
            f"Split this column into two output columns: `{column}_amount` "
            f"(DOUBLE — the numeric value with any currency symbol stripped) "
            f"and `{column}_currency` (VARCHAR — the 3-letter currency code "
            f"derived from the symbol: {symbol_map}; "
            f"default 'USD' if the value has no symbol). Do NOT include the "
            f"original `{column}` column in the output."
        )
    if option_id == "keep_as_text":
        return f"Leave this column completely unchanged: select `{column}` as-is, with no transformation or casting."
    if option_id == "custom":
        if not note:
            return None
        return (
            f"The user has given the following specific instruction for this "
            f"column — follow it exactly, in addition to the general cleaning "
            f"rules (e.g. still strip symbols, parse any K/M/B magnitude "
            f"suffixes per the rule above, and cast to a usable type so the "
            f"result is consistent). If this instruction specifies a currency "
            f"conversion rate, apply it as a single deterministic final "
            f"multiplier on the cleaned numeric value, applied uniformly to "
            f"EVERY non-null row in the column (not just rows with a "
            f"particular currency symbol) — per the Currency unit conversion "
            f"rule above. User instruction: {note.strip()}"
        )
    if option_id == "strip_assume_same":
        return None
    # Generic LLM-proposed option (not one of our hardcoded currency ids):
    # translate the user's choice back into a plain-English instruction using
    # the option's label from the original clarification.
    if clarification:
        opt = next((o for o in clarification.get("options", []) if o["id"] == option_id), None)
        if opt:
            instr = f"For this column, apply the following user-selected handling: {opt['label']}."
            if note:
                instr += f" Additional user note: {note.strip()}"
            return instr
    return None


def _list_tables(db_uri: str, schema: str | None) -> list[str]:
    engine = sqlalchemy.create_engine(db_uri)
    try:
        inspector = sqlalchemy.inspect(engine)
        names = inspector.get_table_names(schema=schema)
        return sorted(names)
    finally:
        engine.dispose()


# Cap on how many tables are profiled/generated/dry-run concurrently per
# project, so a wide schema doesn't open dozens of simultaneous DB
# connections or LLM requests at once.
_ANALYZE_CONCURRENCY = 4


def _analyze_one_table(
    project_id: str, db_uri: str, schema: str | None, table: str,
    run_id: str | None = None,
) -> None:
    """Profile + generate + validate + dry-run a single table; persist the result."""
    debug = DebugLogger(project_id, table, run_id=run_id)
    try:
        metadata, sample = profile_table(db_uri, table, schema=schema, debug=debug)
        script = build_cleaning_script(metadata, sample, debug=debug)

        # The v3.0 per-column expression model makes a column being silently
        # "skipped" structurally impossible (process.md / stage0_v3_spec.md
        # §13). The only remaining failure mode is the focused LLM resolver
        # producing an invalid expression for a CLEAN_AMBIG column — if the
        # assembled SQL fails AST validation, rebuild with the LLM resolver
        # disabled so those columns get their deterministic fallback instead
        # (no second LLM round-trip needed).
        try:
            validate_cleaning_sql(script.duckdb_sql, table)
        except SQLValidationError as e:
            if debug:
                debug.section(
                    "AST validation FAILED — rebuilding with LLM resolver disabled",
                    str(e),
                )
            script = build_cleaning_script(metadata, sample, disable_llm=True, debug=debug)
            try:
                validate_cleaning_sql(script.duckdb_sql, table)
            except SQLValidationError as e2:
                if debug:
                    debug.section("AST validation", f"FAILED: {e2}")
                _save_table_analysis(
                    project_id, table, metadata, script,
                    diff=None, status="failed", cold_start_error=f"AST validation failed: {e2}",
                )
                return

        if debug:
            debug.section("AST validation", "PASSED")

        diff = run_dry_run(script, sample)
        if debug:
            debug.code("Dry-run diff", diff.model_dump(), lang="json")
        _save_table_analysis(project_id, table, metadata, script, diff, status="analyzed")
    except Exception as e:
        if debug:
            debug.section("Unhandled exception", str(e))
        with session_scope() as db:
            existing = (
                db.query(TableAnalysis)
                .filter_by(project_id=project_id, table_name=table)
                .first()
            )
            if existing:
                existing.status = "failed"
                existing.cold_start_error = str(e)
            else:
                db.add(TableAnalysis(
                    project_id=project_id, table_name=table,
                    metadata_json="{}", cleaning_sql="", explanation="",
                    columns_transformed_json="[]", diff_json="{}",
                    script_source="error", status="failed", cold_start_error=str(e),
                ))


async def analyze_project(project_id: str, db_uri: str, schema: str | None) -> None:
    """Profile + generate + validate + dry-run every table (concurrently); persist results."""
    try:
        tables = _list_tables(db_uri, schema)
    except Exception as e:
        with session_scope() as db:
            project = db.get(Project, project_id)
            if project:
                project.status = "failed"
                project.error = f"Could not connect / list tables: {e}"
        return

    semaphore = asyncio.Semaphore(_ANALYZE_CONCURRENCY)
    run_id = new_run_id()  # one folder for all tables of this analyze run

    async def _run(table: str) -> None:
        async with semaphore:
            await asyncio.to_thread(
                _analyze_one_table, project_id, db_uri, schema, table, run_id
            )

    await asyncio.gather(*(_run(table) for table in tables))

    _run_cross_table_consistency(project_id, db_uri, schema)

    with session_scope() as db:
        project = db.get(Project, project_id)
        if project and project.status == "analyzing":
            project.status = "ready"


def _run_cross_table_consistency(project_id: str, db_uri: str, schema: str | None) -> None:
    """Stage 0.5 — Cross-Table Consistency Pass (stage0_v3_spec.md).

    Runs once per project, after every table's v3.0 column-wise processing,
    before locking/cold start. Groups same-kind columns (dates, phone
    numbers, ID/key columns) across tables using only metadata already
    computed during profiling (declared type, inferred_issues, date_format,
    format_signature — never raw row values), picks one canonical format per
    group, and deterministically patches the cleaning expressions of any
    table that doesn't match (no additional LLM calls, no additional
    sampling). The resulting groups/canonical-formats/patched-tables summary
    is persisted on the project for the review UI.
    """
    import json

    with session_scope() as db:
        analyses = (
            db.query(TableAnalysis)
            .filter_by(project_id=project_id, status="analyzed")
            .all()
        )
        tables: dict[str, TableMetadata] = {}
        for a in analyses:
            if not a.metadata_json or a.metadata_json == "{}":
                continue
            tables[a.table_name] = TableMetadata.model_validate_json(a.metadata_json)

    groups = find_groups(tables)

    with session_scope() as db:
        project = db.get(Project, project_id)
        if project:
            project.cross_table_summary_json = json.dumps(build_summary(groups))

    tables_to_patch = sorted({t for g in groups for t in g.tables_needing_patch})

    for table in tables_to_patch:
        try:
            metadata = tables[table]
            patcher = make_patcher(table, groups, metadata)
            if patcher is None:
                continue

            sample = extract_stratified_sample(db_uri, metadata)
            if sample.empty:
                continue

            script = build_cleaning_script(
                metadata, sample, disable_llm=True, expression_patch=patcher,
            )

            try:
                validate_cleaning_sql(script.duckdb_sql, table)
            except SQLValidationError:
                continue

            diff = run_dry_run(script, sample)
            if not diff.safe_to_lock:
                continue

            _save_table_analysis(project_id, table, metadata, script, diff, status="analyzed")
        except Exception:
            # Cross-table alignment is a best-effort enhancement; a failure
            # here must not affect the per-table analyses already saved.
            continue


def _save_table_analysis(
    project_id: str,
    table: str,
    metadata: TableMetadata,
    script: CleaningScript,
    diff,
    status: str,
    cold_start_error: str | None = None,
) -> None:
    import json

    with session_scope() as db:
        existing = (
            db.query(TableAnalysis)
            .filter_by(project_id=project_id, table_name=table)
            .first()
        )
        fields = dict(
            metadata_json=metadata.model_dump_json(),
            cleaning_sql=script.duckdb_sql,
            explanation=script.explanation,
            columns_transformed_json=json.dumps(script.columns_transformed),
            diff_json=diff.model_dump_json() if diff is not None else "{}",
            script_source=script.source,
            status=status,
            cold_start_error=cold_start_error,
            clarifications_json=json.dumps(_build_clarifications(metadata, script)),
        )
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            db.add(TableAnalysis(project_id=project_id, table_name=table, **fields))


def approve_and_process(
    project_id: str,
    table_names: list[str] | None,
    clarification_answers: dict[str, dict[str, dict]] | None = None,
) -> None:
    """Lock cleaning SQL + run cold start for the given tables (default: all).

    ``clarification_answers`` maps
    ``table_name -> {column_name: {"option": option_id, "note": str | None}}``
    for any ambiguity questions the user answered (e.g. mixed-currency
    columns). Tables with non-default answers have their cleaning script
    regenerated with column-specific overrides before being locked.
    """
    import json

    clarification_answers = clarification_answers or {}

    with session_scope() as db:
        project = db.get(Project, project_id)
        if not project:
            return
        db_uri, schema = project.db_uri, project.source_schema
        project.status = "approving"

        query = db.query(TableAnalysis).filter_by(project_id=project_id)
        if table_names:
            query = query.filter(TableAnalysis.table_name.in_(table_names))
        else:
            query = query.filter(TableAnalysis.status == "analyzed")
        analyses = query.all()
        targets = [
            (a.table_name, a.cleaning_sql, a.columns_transformed_json, a.clarifications_json, a.metadata_json)
            for a in analyses
        ]
        for a in analyses:
            a.status = "cold_start_running"
            answers = clarification_answers.get(a.table_name)
            if answers:
                a.clarification_answers_json = json.dumps(answers)

    any_failed = False
    approve_run_id = new_run_id()  # one folder for all tables of this approve run
    for table_name, cleaning_sql, columns_transformed_json, clarifications_json, metadata_json in targets:
        debug = DebugLogger(project_id, f"{table_name}_approve", run_id=approve_run_id)
        try:
            if metadata_json and metadata_json != "{}":
                # Reuse the metadata persisted during analyze_project instead of
                # re-running profile_table's full-table profiling scan (#20);
                # only the sample needs to be refreshed for the dry-run.
                metadata = TableMetadata.model_validate_json(metadata_json)
                sample = extract_stratified_sample(db_uri, metadata)
            else:
                metadata, sample = profile_table(db_uri, table_name, schema=schema)

            answers = clarification_answers.get(table_name) or {}
            clarifications_by_col = {
                c["column"]: c for c in json.loads(clarifications_json or "[]")
            }
            overrides = {
                col: instr
                for col, answer in answers.items()
                if (
                    instr := _column_override_for_answer(
                        col,
                        answer.get("option", ""),
                        answer.get("note"),
                        clarifications_by_col.get(col),
                    )
                )
                is not None
            }
            # Columns the user chose to split (e.g. revenue -> revenue_amount,
            # revenue_currency) are legitimately absent from the cleaned
            # output under their original name — don't flag that in dry-run.
            expected_missing_columns = [
                col for col, answer in answers.items()
                if answer.get("option") == "split_amount_currency"
            ]

            if debug:
                debug.code("Clarification answers", answers, lang="json")
                debug.code("Column overrides", overrides, lang="json")
            if overrides:
                script = build_cleaning_script(metadata, sample, column_overrides=overrides, debug=debug)
                if script.source == "deterministic_fallback":
                    raise RuntimeError(
                        "Clarification-driven regeneration failed (LLM unavailable "
                        "or returned an invalid response for the overridden "
                        "column(s)); the table's cleaning script was not changed "
                        "or locked. Please retry."
                    )
                validate_cleaning_sql(script.duckdb_sql, table_name)
            else:
                script = CleaningScript(
                    table_name=table_name,
                    duckdb_sql=cleaning_sql,
                    explanation="",
                    columns_transformed=json.loads(columns_transformed_json),
                    source="llm_locked",
                )

            diff = run_dry_run(script, sample, expected_missing_columns=expected_missing_columns)
            if not diff.safe_to_lock:
                raise RuntimeError(
                    f"Dry-run safety check failed for '{table_name}': "
                    f"{'; '.join(diff.warnings) or 'unsafe to lock'}. "
                    f"The cleaning script was not locked or cold-started."
                )

            with session_scope() as db:
                write_memory(
                    db, project_id=project_id, domain="Business_Logic",
                    topic=f"cleaning_script_{table_name}", content=script.duckdb_sql,
                )
                run_cold_start(project_id, db_uri, metadata, script, db, diff=diff)

            with session_scope() as db:
                a = db.query(TableAnalysis).filter_by(
                    project_id=project_id, table_name=table_name
                ).first()
                if a:
                    a.status = "cold_start_done"
                    if overrides:
                        a.cleaning_sql = script.duckdb_sql
                        a.explanation = script.explanation
                        a.columns_transformed_json = json.dumps(script.columns_transformed)
        except Exception as e:
            any_failed = True
            with session_scope() as db:
                a = db.query(TableAnalysis).filter_by(
                    project_id=project_id, table_name=table_name
                ).first()
                if a:
                    a.status = "failed"
                    a.cold_start_error = str(e)

    with session_scope() as db:
        project = db.get(Project, project_id)
        if project:
            project.status = "completed"
            if any_failed:
                project.error = "One or more tables failed during cold start; see per-table status."


def resume_incomplete_cold_starts() -> None:
    """Crash-recovery: re-run cold start for any table whose previous build
    was interrupted mid-flight (``cold_start_progress.status == 'in_progress'``).

    ``run_cold_start`` always rebuilds staging from scratch and atomically
    swaps it in, so re-running it for an interrupted table is safe and
    idempotent. Intended to be called once from the API's startup hook —
    without this, ``cold_start_progress`` rows from a crashed worker would
    sit at ``in_progress`` forever and never be retried.
    """
    import json

    with session_scope() as db:
        targets = [
            (p.project_id, p.table_name)
            for p in db.query(ColdStartProgress).filter_by(status="in_progress").all()
        ]

    for project_id, table_name in targets:
        try:
            with session_scope() as db:
                project = db.get(Project, project_id)
                a = (
                    db.query(TableAnalysis)
                    .filter_by(project_id=project_id, table_name=table_name)
                    .first()
                )
                if not project or not a or a.metadata_json == "{}":
                    continue
                db_uri = project.db_uri
                metadata = TableMetadata.model_validate_json(a.metadata_json)
                script = CleaningScript(
                    table_name=table_name,
                    duckdb_sql=a.cleaning_sql,
                    explanation=a.explanation,
                    columns_transformed=json.loads(a.columns_transformed_json),
                    source="llm_locked",
                )
                diff = (
                    DataQualityDiff.model_validate_json(a.diff_json)
                    if a.diff_json and a.diff_json != "{}"
                    else None
                )

            with session_scope() as db:
                run_cold_start(project_id, db_uri, metadata, script, db, diff=diff)
                a = (
                    db.query(TableAnalysis)
                    .filter_by(project_id=project_id, table_name=table_name)
                    .first()
                )
                if a:
                    a.status = "cold_start_done"
        except Exception as e:
            with session_scope() as db:
                a = (
                    db.query(TableAnalysis)
                    .filter_by(project_id=project_id, table_name=table_name)
                    .first()
                )
                if a:
                    a.status = "failed"
                    a.cold_start_error = f"Resume after restart failed: {e}"
