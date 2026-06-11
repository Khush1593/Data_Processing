# Stage 0 — Hybrid Data Pre-Processing Layer

A self-contained, production-ready implementation of **Stage 0** for Clarum
Insights (process.md). It connects to a client's database, profiles a table,
asks an LLM to write a **DuckDB cleaning SQL script once**, validates it,
previews a Data Quality Diff, and — on confirmation — locks the SQL and builds a
clean per-project DuckDB cache that Stage 1 reads from.

This is the **backend library + migrations only** (no FastAPI app shell, no
Next.js frontend). It ships with import-compatible shims (`app.llm_engine`,
`app.memory_engine`) so it drops into the host pipeline unchanged.

## Layout

```
app/
  config.py                 # .env-driven settings (provider-agnostic LLM, DB, tuning)
  db.py                     # control-plane (Postgres) session/engine
  models.py                 # SQLAlchemy: sync_state, cold_start_progress, agent_memory
  memory_engine.py          # write_memory/read_memory (locks the cleaning SQL)
  llm_engine.py             # shim -> app.llm.engine._generate_structured
  llm/engine.py             # provider-agnostic structured generation (Groq + Gemini)
  preprocessing/
    models.py               # Pydantic contract types
    connector.py            # metadata extraction (multi-dialect via Inspector)
    sampler.py              # in-memory stratified sample + issue detection
    profiler.py             # orchestrates connector + sampler enrichment
    script_generator.py     # LLM prompt + cleaning SQL (deterministic fallback)
    ast_validator.py        # sqlglot safety check (SELECT-only, no DML/DDL)
    dry_run.py              # Data Quality Diff over the sample
    cache_engine.py         # chunked cold start -> DuckDB cache + atomic swap
db/migrations/              # PostgreSQL DDL (sync_state, cold_start, agent_memory)
tests/                      # scenarios 1-8 (SQLite + Postgres)
```

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # set LLM_PROVIDER + keys + CONTROL_DB_URI
```

### Control DB (PostgreSQL)

```bash
docker run -d --name clarum-pg \
  -e POSTGRES_USER=clarum -e POSTGRES_PASSWORD=clarum -e POSTGRES_DB=clarum_control \
  -p 5433:5432 postgres:17

# apply migrations
docker exec -i clarum-pg psql -U clarum -d clarum_control < db/migrations/add_sync_state.sql
docker exec -i clarum-pg psql -U clarum -d clarum_control < db/migrations/add_cleaning_cache.sql
```

## LLM provider (swap via `.env`)

```
LLM_PROVIDER=groq            # or: gemini
GROQ_MODEL=openai/gpt-oss-120b
GROQ_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash
GEMINI_API_KEY=...
```

Nothing in the codebase hardcodes a vendor — `_generate_structured()` reads the
active provider/model/key from settings. If the call fails, the script generator
returns a safe pass-through SELECT (`source="deterministic_fallback"`).

## End-to-end flow (programmatic)

```python
from app.db import session_scope
from app.preprocessing.profiler import profile_table
from app.preprocessing.script_generator import generate_cleaning_script
from app.preprocessing.ast_validator import validate_cleaning_sql
from app.preprocessing.dry_run import run_dry_run
from app.preprocessing.cache_engine import run_cold_start
from app.memory_engine import write_memory

# 1. analyse
metadata, sample = profile_table(db_uri, table_name)
script = generate_cleaning_script(metadata, sample)
validate_cleaning_sql(script.duckdb_sql, table_name)
diff = run_dry_run(script, sample)            # show diff.safe_to_lock + warnings to user

# 2. confirm (user approved): lock + cold start
with session_scope() as db:
    write_memory(db, project_id, "Business_Logic", f"cleaning_script_{table_name}", script.duckdb_sql)
    run_cold_start(project_id, db_uri, metadata, script, db)
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Scenarios run against SQLite always and PostgreSQL when reachable
(`TEST_PG_SOURCE_URI`, default `…@localhost:5433/clarum_source`).

## Wiring into the host app (not built here, by scope)

* **§10 API endpoints** — `analyse` / `confirm` / `status` map directly onto the
  functions above; add them to your `api.py`.
* **§11 Stage 1 hook** — before Stage 1, if a `SyncState` row is `completed`,
  read `clean_cache_<table>` from `projects/<project_id>.duckdb` instead of the
  source.
* **§12/§14 frontend** — add the three `preprocessing_*` states.

## Deviations from the reference spec (and why)

* **Multi-dialect connector.** Uses SQLAlchemy `Inspector` + quoted identifiers
  instead of Postgres-only `information_schema` PK SQL with f-string injection —
  works on SQLite and Postgres, and is injection-safe.
* **PostgreSQL DDL.** `gen_random_uuid()` / `timestamptz` replace the spec's
  SQLite `randomblob()` defaults.
* **sqlglot v30 node names.** No `Truncate`/`AlterTable`; TRUNCATE is `Command`,
  ALTER is `Alter`. Validator pinned accordingly.
* **DuckDB regex.** `REGEXP_REPLACE(..., 'g')` global flag added (DuckDB replaces
  only the first match without it).
* **Cold-start staging.** Built from the first non-empty cleaned chunk (real
  column types) rather than `read_csv_auto('/dev/null')`; removes the
  `chunks_done == 0` crash on a sparse first PK range; integer-PK range chunking
  only when the PK is actually integer, else LIMIT/OFFSET.
* **`safe_to_lock` is advisory.** Converting sentinels (`N/A`, `-`) to NULL
  legitimately raises null counts and may trip the null-spike guard; the confirm
  step locks whatever the user approves. Surface the warnings in the UI.
