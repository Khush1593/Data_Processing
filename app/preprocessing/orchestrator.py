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

import sqlalchemy

from app.db import session_scope
from app.memory_engine import write_memory
from app.models import Project, TableAnalysis
from app.preprocessing.ast_validator import SQLValidationError, validate_cleaning_sql
from app.preprocessing.cache_engine import run_cold_start
from app.preprocessing.dry_run import run_dry_run
from app.preprocessing.models import CleaningScript, TableMetadata
from app.preprocessing.profiler import profile_table
from app.preprocessing.script_generator import generate_cleaning_script


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
    {
        "id": "custom",
        "label": "Other — describe how to handle this (e.g. \"convert everything to "
                 "USD; EUR is worth 1.08 USD, GBP is worth 1.27 USD\")",
        "requires_note": True,
    },
]

_CURRENCY_SYMBOL_TO_CODE = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "¥": "JPY"}


def _build_clarifications(metadata: TableMetadata) -> list[dict]:
    clarifications = []
    for col in metadata.columns:
        if col.currency_symbols:
            symbol_codes = [
                _CURRENCY_SYMBOL_TO_CODE.get(s, s) for s in col.currency_symbols
            ]
            clarifications.append({
                "column": col.name,
                "question": (
                    f"Column '{col.name}' contains values in multiple currencies "
                    f"({', '.join(col.currency_symbols)} → {', '.join(symbol_codes)}, "
                    f"and/or values with no symbol). How should this be handled?"
                ),
                "options": CURRENCY_CLARIFICATION_OPTIONS,
                "default": "strip_assume_same",
            })
    return clarifications


def _column_override_for_answer(
    column: str, option_id: str, note: str | None = None
) -> str | None:
    if option_id == "split_amount_currency":
        return (
            f"Split this column into two output columns: `{column}_amount` "
            f"(DOUBLE — the numeric value with any currency symbol stripped) "
            f"and `{column}_currency` (VARCHAR — the 3-letter currency code "
            f"derived from the symbol: $→USD, £→GBP, €→EUR, ₹→INR, ¥→JPY; "
            f"default 'USD' if the value has no symbol). Do NOT include the "
            f"original `{column}` column in the output."
        )
    if option_id == "keep_as_text":
        return f"Leave this column completely unchanged: select `{column}` as-is, with no transformation or casting."
    if option_id == "custom" and note:
        return (
            f"The user has given the following specific instruction for this "
            f"column — follow it exactly, in addition to the general cleaning "
            f"rules (e.g. still strip currency symbols / cast to a numeric type "
            f"as appropriate so the result is usable): {note.strip()}"
        )
    return None


def _list_tables(db_uri: str, schema: str | None) -> list[str]:
    engine = sqlalchemy.create_engine(db_uri)
    try:
        inspector = sqlalchemy.inspect(engine)
        names = inspector.get_table_names(schema=schema)
        return sorted(names)
    finally:
        engine.dispose()


def analyze_project(project_id: str, db_uri: str, schema: str | None) -> None:
    """Profile + generate + validate + dry-run every table; persist results."""
    try:
        tables = _list_tables(db_uri, schema)
    except Exception as e:
        with session_scope() as db:
            project = db.get(Project, project_id)
            if project:
                project.status = "failed"
                project.error = f"Could not connect / list tables: {e}"
        return

    for table in tables:
        try:
            metadata, sample = profile_table(db_uri, table, schema=schema)
            script = generate_cleaning_script(metadata, sample)

            try:
                validate_cleaning_sql(script.duckdb_sql, table)
            except SQLValidationError as e:
                _save_table_analysis(
                    project_id, table, metadata, script,
                    diff=None, status="failed", cold_start_error=f"AST validation failed: {e}",
                )
                continue

            diff = run_dry_run(script, sample)
            _save_table_analysis(project_id, table, metadata, script, diff, status="analyzed")
        except Exception as e:
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

    with session_scope() as db:
        project = db.get(Project, project_id)
        if project and project.status == "analyzing":
            project.status = "ready"


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
            clarifications_json=json.dumps(_build_clarifications(metadata)),
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
        targets = [(a.table_name, a.cleaning_sql, a.columns_transformed_json) for a in analyses]
        for a in analyses:
            a.status = "cold_start_running"
            answers = clarification_answers.get(a.table_name)
            if answers:
                a.clarification_answers_json = json.dumps(answers)

    any_failed = False
    for table_name, cleaning_sql, columns_transformed_json in targets:
        try:
            metadata, sample = profile_table(db_uri, table_name, schema=schema)

            answers = clarification_answers.get(table_name) or {}
            overrides = {
                col: instr
                for col, answer in answers.items()
                if (
                    instr := _column_override_for_answer(
                        col, answer.get("option", ""), answer.get("note")
                    )
                )
                is not None
            }
            if overrides:
                script = generate_cleaning_script(metadata, sample, column_overrides=overrides)
                validate_cleaning_sql(script.duckdb_sql, table_name)
            else:
                script = CleaningScript(
                    table_name=table_name,
                    duckdb_sql=cleaning_sql,
                    explanation="",
                    columns_transformed=json.loads(columns_transformed_json),
                    source="llm_locked",
                )
            with session_scope() as db:
                write_memory(
                    db, project_id=project_id, domain="Business_Logic",
                    topic=f"cleaning_script_{table_name}", content=script.duckdb_sql,
                )
                run_cold_start(project_id, db_uri, metadata, script, db)

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
