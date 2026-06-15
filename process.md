## Stage 0 — Hybrid Data Pre-Processing Layer

## Clarum Insights — Implementation Reference (As Built)

**Version:** 2.0 (As-Built — supersedes v1.1 spec below)
**Codebase:** Clarum Insights (FastAPI + DuckDB + Next.js)
**Status:** Implemented, tested (24/24 scenarios passing), MVP-ready.

> This document describes Stage 0 **as it is actually implemented today**,
> including hardening and features added beyond the original v1.1 spec
> (preserved at the bottom of this file for historical reference). If this
> section and the v1.1 spec ever disagree, **this section is the source of
> truth** — the code matches this section.

---

## 0. What Stage 0 Does (End-to-End)

Stage 0 runs **before** Stage 1 (`data_engine.py`). It connects to a client's
database, figures out what's wrong with each table's data, writes SQL to fix
it, lets the user review/approve it, and then builds a clean cached copy that
Stage 1 reads from.

```
Client DB ──▶ [1] Profile + Sample ──▶ [2] Generate Cleaning SQL ──▶ [3] Validate (AST)
                                                                          │
                              ┌───────────────────────────────────────────┘
                              ▼
                  [4] Dry-Run on Sample → Data Quality Diff
                              │
                              ▼
                  [5] User Review UI (per table: SQL, diff, clarifications)
                              │  user approves / answers clarifications
                              ▼
                  [6] Lock SQL → agent_memory (permanent)
                              │
                              ▼
                  [7] Cold Start: run locked SQL over FULL table, chunked,
                      into a per-project DuckDB cache (atomic swap)
                              │
                              ▼
                  [8] sync_state row marked "completed"
                              │
                              ▼
                  Stage 1 reads from `clean_cache_<table>` view
```

### Guiding principles (unchanged from spec, still enforced)

- **AI writes cleaning SQL once.** After user confirmation it is locked to
  `agent_memory` and never regenerated for that table (unless the user
  answers a clarification question, which triggers one explicit regeneration
  with that answer folded in as an override).
- **Raw row data never leaves the client's server** beyond metadata + a small
  in-memory sample used to write the cleaning SQL.
- **The stratified sample is discarded from memory** once cold start
  completes — never persisted to disk.
- **All existing pipeline stages (1–11) remain unchanged.**
- **Deterministic fallback first, LLM enriches** — and this principle is now
  enforced at *three* separate layers (see §6).

---

## 1. File Layout (As Built)

```text
app/
  preprocessing/
    __init__.py
    connector.py            # DB connection + structural metadata (multi-dialect)
    sampler.py              # stratified sampling + deterministic issue detection
    profiler.py             # orchestrates connector + sampler -> enriched TableMetadata
    script_generator.py      # LLM prompt builder + cleaning SQL generation + Pass-2
    deterministic_cleaner.py # LLM-free cleaning SQL builder (fallback / safety net)
    ast_validator.py          # AST safety check (sqlglot) for cleaning SQL
    dry_run.py                # execute SQL on sample -> Data Quality Diff
    cache_engine.py            # cold-start execution, sync_state, atomic swap
    diff_engine.py             # full-table before/after diff (review UI)
    orchestrator.py            # glues everything together for the API layer
    models.py                  # Pydantic schemas for this layer

app/
  models.py     # + SyncState, ColdStartProgress, Project, TableAnalysis, AgentMemory
  api/
    projects.py  # Stage 0 review-UI endpoints (see §9)

db/
  migrations/
    add_sync_state.sql      # sync_state + cold_start_progress (Postgres)
    add_cleaning_cache.sql  # agent_memory (Postgres)
    add_projects.sql        # projects + table_analyses (Postgres)

frontend/
  lib/api.ts     # typed client for the review-UI endpoints
```

Everything is implemented (no gaps vs. the file list). `diff_engine.py` is an
addition beyond the original spec (full-table changed-rows diff for the
review UI).

---

## 2. Database Schema (As Built — PostgreSQL control plane)

The control plane lives in a separate Postgres database (`CONTROL_DB_URI`),
independent of the client's source DB. All DDL is idempotent
(`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`).

### 2a. `sync_state` / `cold_start_progress` (`add_sync_state.sql`)

Same shape as the v1.1 spec, adapted to Postgres (`gen_random_uuid()`,
`TIMESTAMPTZ`). Tracks per-(project, table) sync mode and cold-start progress
for crash recovery.

### 2b. `agent_memory` (`add_cleaning_cache.sql`)

Permanent store for the **locked** cleaning SQL, keyed by
`(project_id, domain, topic)` with `domain="Business_Logic"`,
`topic="cleaning_script_<table_name>"`. Written exactly once, on approval.

### 2c. `projects` / `table_analyses` (`add_projects.sql`) — new vs. spec

The v1.1 spec's three endpoints assumed a synchronous, single-shot
"analyse → confirm" flow with no persistence between steps. The as-built
system instead supports **analyzing every table in a project up front** and
presenting a review UI, so two new tables were added:

- **`projects`** — one row per "review session" against a source DB
  (`db_uri`, `source_schema`, `status`: `analyzing` → `ready` → `approving`
  → `completed` / `failed`).
- **`table_analyses`** — one row per `(project, table)` holding the full
  analysis result: structural metadata (JSON), generated cleaning SQL,
  explanation, transformed columns, Data Quality Diff (JSON), script source
  (`llm` / `llm_pass2` / `deterministic_fallback` / `llm_locked`),
  clarification questions + answers (JSON), and per-table status
  (`analyzed` → `cold_start_running` → `cold_start_done` / `failed`).

### 2d. SQLAlchemy models (`app/models.py`)

`SyncState`, `ColdStartProgress`, `Project`, `TableAnalysis`, `AgentMemory` —
all match the migrations above field-for-field.

---

## 3. Pydantic Schemas — `app/preprocessing/models.py` (As Built)

Differences from the v1.1 draft:

```python
class ColumnMetadata(BaseModel):
    name: str
    declared_type: str
    null_pct: float = 0.0
    distinct_count: int = 0
    is_primary_key: bool = False
    has_created_at: bool = False
    has_updated_at: bool = False
    has_deleted_at: bool = False
    sample_values: list[str] = Field(default_factory=list)
    inferred_issues: list[str] = Field(default_factory=list)   # <- LIST, not single issue
    currency_symbols: list[str] = Field(default_factory=list)  # <- new: multi-currency detection
    date_format: Optional[str] = None                          # <- new: column-wide date format
    null_sentinel_pct: float = 0.0                              # <- new: for dry-run null-spike math
    issue_ratios: dict[str, float] = Field(default_factory=dict) # <- new: raw heuristic ratios for LLM

class TableMetadata(BaseModel):
    table_name: str
    row_count: int
    columns: list[ColumnMetadata]
    detected_sync_mode: str
    primary_key_column: Optional[str] = None
    change_tracking_column: Optional[str] = None
    source_schema: Optional[str] = None   # <- new: multi-schema support

class CleaningScript(BaseModel):
    table_name: str
    duckdb_sql: str
    explanation: str
    columns_transformed: list[str] = Field(default_factory=list)
    source: str = "llm"   # 'llm' | 'llm_pass2' | 'deterministic_fallback' | 'llm_locked'
    clarification_questions: list[dict[str, Any]] = Field(default_factory=list)  # <- new

class DataQualityDiff(BaseModel):
    table_name: str
    row_count_before: int
    row_count_after: int
    column_diffs: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    safe_to_lock: bool = True

class PreprocessingResult(BaseModel):  # kept for compatibility; not on the hot path anymore
    project_id: str
    table_name: str
    cleaning_script: CleaningScript
    diff: DataQualityDiff
    status: str
```

**Why `inferred_issues` became a list:** a single column routinely has
*multiple* simultaneous problems (e.g. a currency column that also needs
trimming and has null-sentinel rows). The v1.1 spec's single
`inferred_issue: Optional[str]` could only report one; the cleaner now
combines all of them into one expression (see §5/§6).

---

## 4. `connector.py` — Structural Metadata (As Built)

Multi-dialect via SQLAlchemy's `Inspector` (works for PostgreSQL **and**
SQLite — used by the test suite). Key hardening vs. spec:

- **All identifiers are safely quoted** via
  `engine.dialect.identifier_preparer.quote(...)` — never raw f-string
  interpolation of column/table names.
- **Table existence is validated up front** (`TableNotFoundError`) before any
  identifier is interpolated into SQL.
- **Null percentages computed in ONE query** (`SELECT SUM(CASE WHEN col IS
  NULL ...), SUM(...), ...`) instead of one `COUNT(*)` scan per column.
- **`COUNT(DISTINCT ...)` is intentionally NOT run on the live DB** — that's
  a per-column hash-aggregate over the full table, exactly the kind of heavy
  analytics this layer must avoid imposing on the client. `distinct_count` is
  instead estimated later, locally, from the in-memory sample
  (`sampler.extract_stratified_sample`).
- **Multi-schema support**: `schema` parameter threaded through, stored on
  `TableMetadata.source_schema`.

Sync-mode inference rules (unchanged from spec):
`has_deleted_at` → `delete_aware`; `pk + has_updated_at` → `upsert`;
`row_count < 10,000 and no updated_at` → `full_resync`; else `append_only`.

---

## 5. `sampler.py` — Stratified Sampling + Issue Detection (As Built)

### 5a. Sampling strategy

Pulls a naive chunk (`PREPROCESSING_NAIVE_CHUNK_SIZE`, default 10,000), then
stratifies **locally in pandas** (never via `ORDER BY` on the live DB):

1. **Random baseline** — up to 400 rows.
2. **Null-revealing rows** — up to 10 rows per column that has any nulls.
3. **Numeric boundary rows** — top/bottom 20 rows per numeric column (finds
   outliers like `-999999`).
4. **PK-range endpoints** *(new vs. spec)* — first/last 20 rows by primary
   key, via indexed `ORDER BY ... LIMIT` (cheap even on huge tables). Catches
   anomalies clustered in the most-recently-loaded batch.
5. **PostgreSQL `TABLESAMPLE SYSTEM (1)`** *(new vs. spec)* — when the source
   is Postgres, an additional random-page sample is taken first so the naive
   chunk isn't blind to anomalies clustered later in physical storage.

All frames are concatenated, deduplicated, and capped at
`PREPROCESSING_SAMPLE_SIZE` (default 1000).

### 5b. Issue detection — presence-based, not threshold-based (major change)

The v1.1 spec's `detect_column_issues` used **ratio thresholds** (e.g.
"currency symbols in >30% of rows"). **As built, this is presence-based**: a
single dirty row anywhere in a million-row table still needs cleaning, and
must not be silently ignored. `detect_column_issues` now returns **a list of
ALL matching issues** (a column can have multiple), one of:

| Issue | Trigger (as built) |
|---|---|
| `currency_string` | ANY value contains a currency symbol (20+ symbols recognized: $ € £ ¥ ₹ ₩ ₽ ₪ ₫ ₴ ₦ ₱ ฿ ₲ ₡ ₵ ₸ ₮ ₭ ₼ ₾ ₺) |
| `percentage_string` | ANY value ends in `%`, OR the column mixes `%`-suffixed and bare numeric values (`mixed_percent_format_ratio`) |
| `null_variant` | ANY value (case/whitespace-insensitive) is one of: `n/a, na, null, none, -, –, —, "", nan, #n/a` |
| `needs_trim` | ANY value has leading/trailing whitespace |
| `inconsistent_casing` | column is categorical (distinct < 20) AND has case-only duplicate values |
| `inconsistent_boolean` | column is a **string** type, ≤2 distinct values, all drawn from `{y,n,yes,no,true,false,1,0,t,f}`, with both a truthy AND falsy value present |
| `mixed_date_format` | ≥10% of values look date-shaped (`date_like_ratio`) |
| `numeric_as_string` | <10% date-shaped AND ≥10% of values are numeric-after-symbol-stripping (`numeric_like_ratio`) |

Only `date_like_ratio` / `numeric_like_ratio` remain ratio-based (with a
"near-threshold" margin surfaced to the LLM for judgment calls — see §6).

**Important fix (2026-06-13):** `inconsistent_boolean` previously fired on
*any* non-numeric column with ≤2 distinct boolean-like values — including
columns whose **declared type is already `BOOLEAN`** (e.g.
`marketing_campaigns.is_active`, which pandas loads as native `bool`/`True`/
`False`, and `True`/`False` happen to be in the boolean-token set in some
encodings). Flagging an already-clean boolean column caused the cleaner to
wrap it in `LOWER(TRIM(col))`, and DuckDB rejects `TRIM(BOOLEAN)` with a
binder error — **which aborted the entire cleaning SQL for the table**,
including unrelated fixes like `ctr`/`roas` normalization. Fixed by requiring
the declared type to be a **string** type for this issue to fire.

### 5c. Other deterministic helpers (new vs. spec)

- **`detect_currency_symbols`** — returns the distinct set of currency
  symbols seen in a column; if >1, flags a multi-currency clarification.
- **`detect_date_format`** — scans an ENTIRE slash/dash-date column and picks
  ONE column-wide format (`%d/%m/%Y` vs `%m/%d/%Y`) by checking whether any
  value's day/month component exceeds 12. Prevents the classic per-row
  ambiguity bug where `04/05/2023` could silently be parsed as either 4
  April or 5 April depending on which `TRY_STRPTIME` happens to match first.
- **`select_diverse_sample_values`** — picks the 10 values shown to the LLM
  using longest/shortest/non-alphanumeric-first selection, so a single row
  with a stray `%` or currency symbol is surfaced even in a large column.
- **`get_suspicious_pass_throughs`** and **`get_uncleaned_flagged_columns`** —
  see §6 (safety nets against the LLM skipping columns).

---

## 6. Cleaning SQL Generation — Three Layers of Defense (SUPERSEDED by §15)

> **This entire section describes the v2.0 monolithic-SQL architecture and is
> now historical.** `script_generator.py` (Layer 1 + Layer 2/"Pass-2") and
> `deterministic_cleaner.py` (Layer 3) **no longer exist in the codebase** —
> they were fully replaced by the v3.0 per-column architecture in §15
> (Column Intelligence Gate → `expression_builder.build_expression` /
> `build_passthrough` for CLEAN_DET/passthrough columns, `llm_resolver.py`'s
> single focused call for CLEAN_AMBIG columns, `sql_assembler.py` for final
> assembly). In particular, "Layer 2 — Pass-2" below — which re-prompted the
> LLM to rewrite an *entire* monolithic `SELECT` statement — is structurally
> incompatible with v3.0's per-column `ColumnExpression` slots and was
> removed rather than ported: v3.0 makes "the LLM missed a column" impossible
> by construction (every column has exactly one expression slot, assigned
> deterministically for CLEAN_DET and via the focused resolver for
> CLEAN_AMBIG — see §15c), so a corrective re-pass over the whole statement is
> unnecessary. Retained below for historical context only; **§15 is the
> source of truth for current SQL-generation behavior.**

This is the biggest expansion vs. the v1.1 spec, which had a single
"LLM, else do-nothing pass-through" path. **The do-nothing fallback has been
completely removed** — every fallback path now produces real cleaning SQL.

### Layer 1 — LLM generation (`script_generator.py`)

`generate_cleaning_script(metadata, sample, ...)`:

- Builds a detailed prompt (`_build_prompt`) containing, per column: declared
  type, null %, distinct count, **all** inferred issues, top-10 diverse
  sample values, detected date format (if any), near-threshold ratios, and
  explicit mixed-percentage warnings.
- Requires the LLM to emit `column_decisions` (one entry per column,
  reasoned about **before** writing SQL — chain-of-thought forcing) followed
  by `duckdb_sql`, `explanation`, `columns_transformed`, and optional
  `clarification_questions`.
- The prompt encodes exact, worked SQL templates for every issue type:
  currency (incl. K/M/B magnitude suffixes, European decimal commas,
  accounting-style negatives), percentage (magnitude-aware: `2%`→0.02,
  bare `45`→0.45, bare `0.01`→0.01, all in ONE consistent fraction scale),
  null variants, trim, casing, boolean, mixed dates (column-wide format,
  epoch-seconds and Excel-serial-date detection with range guards), and
  numeric-as-string (with leading-zero-code protection).
- `_validate_column_decisions` enforces that if the LLM's own reasoning says
  a column needs cleaning, it MUST appear in `columns_transformed` — a
  mismatch raises and triggers the deterministic fallback.
- `_sanitize_sql` fixes a common LLM mistake (writing `"null"` as a quoted
  *identifier* instead of `'null'` as a string literal).
- Clarification questions are validated (`_validate_clarifications`):
  malformed entries, unknown columns, <2 options, or bad defaults are
  dropped; capped at 5 per table.

**On any exception** (rate limit, bad key, network, validation failure):
falls through to Layer 3 (`build_deterministic_cleaning_sql`) — see below.

### Layer 2 — Pass-2 "suspicious pass-through" rewrite (`script_generator.py` + `sampler.py`)

After Pass 1, `get_suspicious_pass_throughs(sample, metadata,
columns_transformed)` re-checks every column the LLM left untransformed:

- **Ground truth check** *(new, 2026-06-13)*: any column **our own
  deterministic detector** flagged with a data-changing issue
  (`currency_string`, `percentage_string`, `inconsistent_boolean`,
  `mixed_date_format`, `numeric_as_string`, `null_variant`) but the LLM did
  not transform → flagged. This is the primary fix for "LLM sometimes skips
  important columns" — e.g. a `ctr` column mixing `2.3%` / `0.023` / `2.3`
  will be caught even if the LLM passed it through.
- **Heuristic checks** on remaining string columns (substring type match, so
  `VARCHAR(10)` etc. are covered — the original spec's exact-match
  `{"varchar","text",...}` filter silently excluded every length-qualified
  type):
  - Rule 1: >50% of values start with a digit/currency symbol AND contain
    letters (e.g. `"14 days"`, `"$100 paid"`).
  - Rule 2: average ≥2 occurrences of `-`, `/`, `T`, `:`, `Z` per value
    (messy timestamps).

If any columns are flagged, `generate_pass2_cleaning_script` re-prompts the
LLM with the original SQL + the missed columns' sample values, asking it to
rewrite the **entire** statement keeping correct parts and adding cleaning
for the missed columns. Source becomes `llm_pass2`. If Pass 2 itself fails
(SQL invalid or LLM error), Pass 1's script is kept as-is — which is why
Layer 3 exists.

### Layer 3 — Deterministic fallback (`deterministic_cleaner.py`) — LLM-free

`build_deterministic_cleaning_sql(metadata)` builds correct DuckDB SQL
**directly from `inferred_issues`**, with no LLM involved. Used when:

- The LLM is unavailable for Pass 1 (`source="deterministic_fallback"`), or
- The LLM's Pass-1 SQL fails AST validation (`source="deterministic_fallback"`), or
- **Final safety gate** *(new, 2026-06-13, in `orchestrator.py`)*: after
  Pass 1 (+ Pass 2 if it ran), if ANY column with a data-changing
  `inferred_issue` is still not in `columns_transformed`, the whole script
  is discarded and replaced with the deterministic build. This is the
  backstop for "LLM available for Pass 1, Pass 2 also fails, column still
  dirty" — it guarantees a flagged column is **never** shipped uncleaned.

Per-column logic mirrors the LLM prompt's rules exactly (so output is
comparable across paths):

- **Type-safety, as of 2026-06-13**: every column expression now starts with
  `CAST(col AS VARCHAR)` before any `TRIM`/`LOWER`/`REGEXP_REPLACE`/`strpos`
  is applied. This makes the generated SQL safe even if a column ends up
  flagged despite having a non-text declared type — defense in depth on top
  of the §5b detection fix.
- `needs_trim` → `TRIM(...)`, applied first so downstream checks see trimmed
  values.
- `currency_string` → magnitude-aware (K/M/B), European-decimal-aware,
  accounting-negative-aware `CASE` → `DOUBLE`.
- `percentage_string` → magnitude-aware fraction normalization (`%`→÷100,
  bare `>1`→÷100, bare `≤1`→unchanged).
- `mixed_date_format` → `COALESCE` chain: `TRY_CAST AS TIMESTAMP`, detected
  column-wide slash/dash format, `%b %d %Y`, `%B %d %Y`, range-guarded
  epoch-seconds, range-guarded Excel-serial-date.
- `numeric_as_string` → `TRY_CAST(REGEXP_REPLACE(...) AS DOUBLE)`, **unless**
  sample values look like zero-padded codes (`0\d+`), in which case the
  column is left as text to preserve leading zeros.
- `inconsistent_boolean` → `CASE` → `TRUE`/`FALSE`/`NULL`.
- `null_variant` / `inconsistent_casing` → layered on top of the above if not
  already the "primary" conversion.

Only ONE "primary" type-conversion issue applies per column (priority:
currency > percentage > date > numeric > boolean), but `needs_trim`,
`null_variant`, and `inconsistent_casing` can be layered on top of any of
them or on a pass-through column.

---

## 7. `ast_validator.py` — Safety Check (As Built)

Parses with `sqlglot(dialect="duckdb")`. Rejects:

- Empty SQL, unparseable SQL, anything but exactly 1 statement, anything that
  isn't a `SELECT`.
- Any `Drop`, `Delete`, `Insert`, `Update`, `Create`, `Alter`, or `Command`
  node anywhere in the tree (`Command` catches `TRUNCATE`, `COPY`, `VACUUM`,
  `CALL`, `PRAGMA`, etc. — node names are pinned to the installed sqlglot v30,
  which has no separate `Truncate`/`AlterTable`).
- **Table-valued function calls in FROM** *(new vs. spec)* — rejects
  `read_csv_auto(...)`, `read_parquet(...)`, `glob(...)` etc., which would let
  LLM-authored SQL read arbitrary files/URLs instead of the sampled table.
- More than one base table (no joins/subqueries pulling in other tables), and
  the single table must match the expected `table_name`.

`dry_run.py` and `cache_engine.py` add a further belt-and-braces layer:
`SET enable_external_access=false` on every DuckDB connection that runs
LLM-authored SQL.

---

## 8. `dry_run.py` — Data Quality Diff (As Built)

Runs the cleaning SQL against the **in-memory sample only** (sandboxed
DuckDB connection, external access disabled). Computes:

- Row count before/after — any change → `safe_to_lock=False`.
- Per-column null counts before/after, with **expected-null accounting**
  *(new vs. spec)*: null-sentinel strings (`N/A`, `null`, `-`, etc.) that the
  cleaning SQL correctly converts to real `NULL` are subtracted from the
  "null spike" calculation — they're a correct conversion, not a regression.
  Only *unexpected* new nulls beyond that count toward
  `PREPROCESSING_NULL_SPIKE_THRESHOLD` (default 10%).
- Per-column dtype before/after.
- Missing columns → `safe_to_lock=False`, **unless** the column is in
  `expected_missing_columns` *(new vs. spec)* — used when a user's
  clarification answer says "split `revenue` into `revenue_amount` +
  `revenue_currency`", which legitimately removes the original column.
- `warnings: list[str]` + `safe_to_lock: bool`, gating whether the script can
  be locked.

---

## 9. Review-UI Orchestration & API (As Built — replaces v1.1 §10)

The v1.1 spec's three synchronous endpoints
(`analyse` / `confirm` / `status`) were replaced with a **project-based,
analyze-all-tables-then-review** flow, implemented in
`orchestrator.py` + `app/api/projects.py`.

### 9a. `orchestrator.py`

- **`analyze_project(project_id, db_uri, schema)`** — lists all tables,
  profiles + generates + validates + dry-runs each **concurrently**
  (semaphore-limited to 4 at a time), persists a `TableAnalysis` row per
  table. After all tables: **`_align_cross_table_key_types`** *(new vs.
  spec)* — finds columns sharing a name across ≥2 tables (e.g. a
  `customer_id` FK and `customers.id` PK) whose cleaned types diverged, and
  regenerates the divergent table's script with a "must end up as type X"
  override so cross-table joins don't break downstream.
- **`approve_and_process(project_id, table_names, clarification_answers)`** —
  for each selected table: applies any clarification-answer overrides
  (regenerating via the LLM if needed), re-validates, re-runs the dry-run,
  and **only if `safe_to_lock`** writes the SQL to `agent_memory` (locked,
  permanent) and runs `run_cold_start`.
- **`resume_incomplete_cold_starts()`** — crash recovery, called once at API
  startup: re-runs cold start for any table whose `cold_start_progress.status
  == "in_progress"` (cold start is idempotent — full rebuild + atomic swap).

### 9b. API endpoints (`app/api/projects.py`)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/projects` | Create a project for `(db_uri, schema_name)`, kick off `analyze_project` in the background. Returns `project_id`. |
| `GET` | `/api/projects/{id}` | Project status + per-table summary: row count, sync mode, issues, `columns_transformed`, `script_source`, `safe_to_lock`, warnings, status, clarifications. |
| `GET` | `/api/projects/{id}/tables/{table}` | Full detail for one table: metadata, cleaning SQL, explanation, full Data Quality Diff. |
| `POST` | `/api/projects/{id}/approve` | Lock + cold-start selected tables (or all `ready` tables), with optional `clarification_answers`. |
| `GET` | `/api/projects/{id}/tables/{table}/diff` | Paginated full-table before/after diff (changed rows only) via `diff_engine.get_diff_page`. |

### 9c. Clarification questions (new vs. spec)

Two sources, merged by `_build_clarifications`:

1. **Deterministic currency clarifications** — if a column has >1 distinct
   currency symbol, the user is asked: strip-and-assume-same-unit (default),
   split into `<col>_amount` + `<col>_currency`, or leave as text.
2. **LLM-flagged ambiguities** — anything the LLM marked in
   `clarification_questions` (e.g. ambiguous DD/MM vs MM/DD dates with no
   column-wide signal, unclear leading-zero codes, inconsistent categorical
   spellings, mixed units).

Every clarification gets a free-form **"Other"** option appended so the user
can always give custom instructions, which get folded into a column override
and used to regenerate that table's script before locking.

---

## 10. `cache_engine.py` — Cold Start (As Built, hardened vs. spec)

`run_cold_start(project_id, db_uri, metadata, script, db_session, diff=None)`:

- **Output schema is fixed up front from the dry-run's `DataQualityDiff`**
  (`type_after` per column → DuckDB type), not inferred from whichever chunk
  happens to run first — avoids a later chunk's different inferred type (e.g.
  an all-null column in chunk 1) corrupting the staging schema. Falls back to
  inferring from the first non-empty chunk if no diff was passed (e.g. called
  directly).
- **No `read_csv_auto('/dev/null')` hack** from the spec — staging table is
  created with explicit `CREATE TABLE (...)` from the computed schema, so
  empty source tables work correctly.
- **Integer-PK range chunking** only when the PK is actually an integer type
  (`_pk_is_integer`); otherwise **LIMIT/OFFSET inside a single
  `REPEATABLE READ` transaction with explicit `ORDER BY`** (Postgres) so
  paginated reads are consistent even with concurrent writes.
- Each chunk is cleaned in an **isolated in-memory DuckDB connection**
  (`enable_external_access=false`) running the locked SQL, then `CAST` to the
  fixed staging schema and `INSERT`ed.
- **Atomic swap**: `staging` → `live` via `DROP` + `RENAME` + `CREATE OR
  REPLACE VIEW` inside one transaction — Stage 1 never sees a half-built
  cache.
- **Reconciliation check**: cached row count vs. source row count, warning if
  delta > `PREPROCESSING_RECONCILIATION_THRESHOLD` (default 0.5%).
- **Post-cold-start quality check** *(new vs. spec)* —
  `_post_cold_start_quality_check`: after the swap, compares each
  dry-run-diffed column's **full-data** null rate (source vs. cache) against
  what the sample-based dry-run predicted. If the real data has a bigger null
  spike than the sample suggested (e.g. a `TRY_CAST` that silently nulls a
  value pattern absent from the sample), it's written to
  `agent_memory` (`domain="Audit_Log"`) — best-effort, never fails the run.
- Progress (`cold_start_progress`) is updated per chunk for crash recovery.

---

## 11. Configuration (As Built)

`.env` / `app/config.py` (`Settings`):

```bash
# LLM provider — groq | gemini (see app/llm/engine.py)
LLM_PROVIDER=groq
GROQ_MODEL=openai/gpt-oss-120b
GROQ_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
GEMINI_API_KEY=
LLM_TEMPERATURE=0.1
LLM_MAX_RETRIES=2
LLM_TIMEOUT_SECONDS=60

# Control-plane DB (Postgres) — sync_state, cold_start_progress, agent_memory, projects, table_analyses
CONTROL_DB_URI=postgresql+psycopg2://clarum:clarum@localhost:5433/clarum_control

# Source DB (test/example only — supplied per-project in production)
SOURCE_DB_URI=

# Stage 0 tuning
PREPROCESSING_SAMPLE_SIZE=1000          # final sample row cap
PREPROCESSING_NAIVE_CHUNK_SIZE=10000    # initial naive pull size
PREPROCESSING_CHUNK_SIZE=100000         # cold-start chunk size
PREPROCESSING_NULL_SPIKE_THRESHOLD=0.10
PREPROCESSING_RECONCILIATION_THRESHOLD=0.005
PREPROCESSING_ENABLED=true

# Per-project clean DuckDB cache location
DUCKDB_CACHE_DIR=projects

# Debug logging (per-run, per-table prompt/response/SQL/diff dumps)
PREPROCESSING_DEBUG_LOG=true
DEBUG_LOG_DIR=debug_logs
```

`app/debug_logger.py` *(new vs. spec)* — when enabled, writes a structured
per-run, per-table log (prompts sent, LLM responses, generated SQL, AST
validation results, dry-run diffs) to `DEBUG_LOG_DIR` for troubleshooting
exactly the kind of issue described in §6 (e.g. seeing the raw "Binder Error"
and which fallback path fired).

---

## 12. Frontend (As Built)

`frontend/lib/api.ts` provides a typed client:
`createProject`, `getProject`, `getTableDetail`, `approveProject`,
`getTableDiff`, plus `TableSummary` / `TableDetail` / `Clarification` /
`DiffPage` types matching the API responses in §9b exactly.

State machine (conceptually, per the v1.1 spec's
`preprocessing_analysis` / `preprocessing_review` /
`preprocessing_cold_start`): the UI polls `GET /api/projects/{id}` while
`status == "analyzing"`, shows the per-table review list (issues, diff,
clarifications, "Show cleaning SQL & explanation") once `status == "ready"`,
posts to `/approve` with any clarification answers, then polls again while
`status == "approving"` until `"completed"`.

---

## 13. Test Coverage (`tests/test_scenarios.py`)

49 scenarios, all passing, covering:

1. Connector metadata extraction + sync-mode inference + delete-aware
2. Sampler stratification + issue detection (currency, percentage, date,
   null-variant)
3. Profiler enrichment (sample values + inferred issues)
4. Script generator — LLM path (fake provider) + LLM-unavailable fallback
   (must produce real cleaning SQL, not pass-through)
5. AST validator — blocks all destructive ops + accepts valid SELECTs
6. Dry-run — Data Quality Diff correctness
7. Cold start — chunked build + atomic swap + `sync_state`
8. Multi-chunk cold start with monkeypatched small chunk size (memory/offset
   correctness)
9. Stage 0 v3.0 Selective Intelligence Architecture (Column Intelligence
   Gate, targeted sampling, focused LLM resolver, deterministic expression
   builder, SQL assembler) — see §15.
10. Stage 0.5 Cross-Table Consistency Layer + Alphanumeric ID Guard — see §16.

---

## 15. Stage 0 v3.0 — Selective Intelligence Architecture (As Built)

Layered on top of the v2.0 pipeline described in §1–12, v3.0 narrows the
"surface area" that ever reaches the LLM or gets sampled, while keeping every
guarantee from v2.0 (AST validation, dry-run gating, locking, cold start).

### 15a. Column Intelligence Gate (`column_classifier.py`)

Every column is classified **twice**:

- **`pre_classify`** (before sampling) — using only structural metadata
  (declared type, name, PK/FK-ish heuristics via `_is_identifier`,
  `_name_tokens`) into a `ColumnClass`:
  `PII`, `IDENTIFIER`, `STRUCTURAL`, `FREE_TEXT`, `OBSERVE`,
  `CLEAN_DET` / `CLEAN_AMBIG` (deferred until issues are known).
  `needs_sample(c)` computes the **candidate set** — the columns whose
  values get profiled into `sample_values`/`inferred_issues` (and therefore
  could reach the LLM). `PII`/`IDENTIFIER`/`STRUCTURAL` columns are excluded
  from this candidate set, so their raw values never populate
  `sample_values`/`inferred_issues`/`issue_ratios`/etc. and never reach the
  LLM. This is distinct from the `sample` DataFrame itself: `profile_table`
  still pulls the **full row shape** (every column, including
  PII/IDENTIFIER) into `sample`, because `dry_run` must execute the assembled
  `SELECT` against it (§15b) and Stage 0.5 needs `format_signature` for
  every column (§16b) — `enrich_metadata_with_sample` is what enforces the
  privacy boundary by only *writing LLM-bound fields* for candidate columns,
  not by removing columns from `sample`.
- **`post_classify`** (after `inferred_issues` are known) — refines
  `CLEAN_DET` vs `CLEAN_AMBIG` vs pass-through based on which issues actually
  fired for that column.

### 15b. Targeted sampling (`profiler.py`)

`profile_table` still pulls the **full row shape** for every column (so
`dry_run` can execute the assembled `SELECT`), but
`enrich_metadata_with_sample` only populates `sample_values` /
`inferred_issues` / `issue_ratios` / `currency_symbols` / `date_format` /
`null_sentinel_pct` for **candidate** (non-PII/IDENTIFIER/STRUCTURAL)
columns. Excluded columns get empty `sample_values`/`inferred_issues` and a
pure passthrough expression — they are never profiled from the in-memory
sample, even though their bytes are present in the DataFrame.

### 15c. Per-column expression slots (`expression_builder.py` + `profiler.py`)

`build_cleaning_script` now builds **exactly one `ColumnExpression` per
column**, so a column can never be silently dropped from the output SELECT:

- `CLEAN_DET` → `build_expression(col, active_issues)` — deterministic SQL
  built directly from `inferred_issues`, same per-issue templates as the v2.0
  Layer 3 deterministic cleaner (currency, percentage, date, numeric-as-string,
  boolean, trim, null-variant, casing), but built once per column without an
  LLM round trip.
- `CLEAN_AMBIG` → batched into a single **focused LLM resolver** call
  (`llm_resolver.resolve_ambiguous`), which only ever sees the ambiguous
  columns' sample values/issues — not the whole table. `_deterministic_fallback`
  covers the same columns if the LLM is disabled/unavailable, ensuring
  `disable_llm=True` (used internally by the Stage 0.5 patcher, §16) still
  produces a complete, valid script.
- Everything else (`PII`, `IDENTIFIER`, `FREE_TEXT`, `STRUCTURAL`, `OBSERVE`)
  → `build_passthrough(col)`.

A `column_overrides` map (from clarification answers) can replace any
column's expression with a deterministic split/leave-as-text rewrite or
forward a free-form instruction to the LLM resolver as additional context.

### 15d. Assembly (`sql_assembler.py`)

`build_select` assembles all `ColumnExpression`s (restored to original column
order) into the final `SELECT`; `build_audit_log` produces the
human-readable explanation from the same expression list plus the
classification summary. Output then goes through the same AST validation +
dry-run + lock + cold-start pipeline as v2.0 (§7–10), unchanged.

---

## 16. Stage 0.5 — Cross-Table Consistency Layer (As Built)

**Spec:** `stage0_v3_spec.md` (incl. the "Final Addendum: Alphanumeric ID
Guard"). Implemented in `app/preprocessing/cross_table_consistency.py`, wired
into `orchestrator.py` as `_run_cross_table_consistency`.

### 16a. When it runs

Once per project, **after** every table has finished its v3.0 column-wise
`analyze` pass (each table independently classified, sampled, cleaned,
AST-validated, dry-run) but **before** the project moves to `ready` for
user review/locking. It is purely a **project-wide alignment pass** over
metadata already computed — it does **not** re-sample, does **not** call the
LLM, and never looks at raw row values.

```
... per-table v3.0 analyze (×N tables, concurrent) ...
        │
        ▼
_run_cross_table_consistency(project_id, db_uri, schema)
   1. find_groups(tables)        — group same-kind columns across tables,
                                    pick one canonical format per group
   2. build_summary(groups)      — persist to Project.cross_table_summary_json
   3. for each table needing a patch:
        make_patcher(table, groups, metadata)
        → re-run build_cleaning_script(..., disable_llm=True,
                                        expression_patch=patcher)
        → AST-validate, dry-run; only overwrite the saved TableAnalysis
          if still safe_to_lock (best-effort; failures are swallowed)
        │
        ▼
project.status = "ready"
```

### 16b. `format_signature` — the PII-free pattern descriptor

A new field on `ColumnMetadata` (`app/preprocessing/models.py`):

```python
class ColumnMetadata(BaseModel):
    ...
    format_signature: Optional[str] = None
```

Computed for **every** column present in the sample during
`enrich_metadata_with_sample` (`profiler.py`) — including PII/IDENTIFIER
columns that are otherwise excluded from `sample_values`/`inferred_issues`,
because a format descriptor is a *shape*, never a value:

| Column kind (detected via `_name_tokens`/`_is_identifier`/declared type) | `format_signature` |
|---|---|
| Phone-like (`phone`, `mobile`, `cell`, `tel`, `fax`, `whatsapp` in name) | `_phone_format_signature` → `"local_10d"` / `"intl_12d"` (digit-count + intl `+` prefix ratio) |
| ID/key columns (`_is_identifier`) | `_id_format_signature` → `"numeric"` / `"alnum"` / `None` (see §16d) |
| Everything else | `_date_format_signature` → `"native_timestamp"` / a detected strptime format (e.g. `"%d/%m/%Y"`) / `"%Y-%m-%d"` / `"ambiguous"` / `None` (not date-like) |

### 16c. Step A+B — `find_groups` (date & phone groups)

For each "kind" (date / phone / id), columns are bucketed across all tables
by a normalized label (`_label_key` — e.g. `order_date` and `created_date`
both bucket under `"date"`; phone variants under their token; ID columns
bucket by exact lower-cased column name). A bucket only becomes a
`ConsistencyGroup` if it has members from **2+ different tables**
(conservative — single-table "groups" are dropped).

- **Date groups**: v3.0's column-wise cleaning already converts any column
  with a determinable format (declared `TIMESTAMP`/`DATE`, or a detected
  strptime format) to a native `TIMESTAMP` — so `"native_timestamp"` and any
  detected strptime format are *output-equivalent* and normalized to
  `"native_timestamp"` for grouping; only `"ambiguous"` (no determinable
  format) differs. Canonical format = weighted-majority (by row count),
  preferring `"native_timestamp"` on ties. `_date_patch_expr` **always
  returns `None`** — there is no safe SQL rewrite for an "ambiguous" column
  (would require re-sampling/re-resolving), so mismatching tables get a
  `cross_table_alignment_needed: ... needs manual review` note instead of a
  SQL change.
- **Phone groups**: canonical = weighted-majority, preferring an
  international (`intl_*`) format on ties. `_phone_patch_expr` only rewrites
  a mismatch when it's a pure separator/punctuation difference at the **same
  digit count and locality** (`REGEXP_REPLACE(expr, '[^0-9]', '', 'g')`);
  anything else (e.g. adding a country code) is left as-is + flagged for
  manual review, same reasoning as dates.

### 16d. Step 1+2 — ID/key groups & the Alphanumeric ID Guard

This is the part covered by the **Final Addendum**. Unlike date/phone
columns (which v3.0 already cleans to a canonical *type*), v3.0's
`IDENTIFIER` columns are pure passthrough — so **every** ID/key group member
needs a Stage 0.5 patch, not just the minority that mismatch
(`ConsistencyGroup` for `"id"` groups sets `tables_matching=[]` and
`tables_needing_patch=` all member tables).

**Step 1 — classify the group as `"numeric"` or `"alnum"` (mandatory
precondition, runs before any SQL is generated):**

```python
_NUMERIC_DECLARED_TOKENS = ("int", "numeric", "decimal", "serial")

def _is_numeric_declared(declared_type: str) -> bool:
    return any(t in declared_type.lower() for t in _NUMERIC_DECLARED_TOKENS)
```

- If **any** member's `declared_type` is a native numeric type
  (`INT`/`BIGINT`/`SMALLINT`/`NUMERIC`/`DECIMAL`/`SERIAL`, ...) →
  group is **`"numeric"`** (highest precedence — `canonical_reason`:
  `"native numeric declared type in table '<table>'"`).
- Else, if **any** member's `format_signature == "alnum"` (i.e. its sampled
  values contained a letter A–Z, computed by `_id_format_signature` during
  profiling — `_HAS_LETTER_RE` over up to the first 50 sampled values) →
  group is **`"alnum"`** (`"letters found in sampled ID values across the
  group (UUID/hash-like)"`).
- Else (all `VARCHAR`/`TEXT`, no letters anywhere) → **`"numeric"`**
  (`"no letters found in sampled ID values across the group"`).

**Step 2 — `_id_patch_expr(existing, current_sig, canonical_sig)` applies the
transformation per member**, gated strictly by that member's *own*
`format_signature` (never the group canonical alone — the hard rule applies
per-column):

| This member's `format_signature` | Output expression | Note added |
|---|---|---|
| `"alnum"` | `TRIM(CAST((<existing>) AS VARCHAR))` — cast/trim only, **no content transform** | If group canonical is `"numeric"`: `"...leading-zero stripping was NOT applied — only trimmed/cast to VARCHAR..."` (manual-review hint) |
| `"numeric"`, canonical `"numeric"` | `REGEXP_REPLACE(TRIM(CAST((<existing>) AS VARCHAR)), '^0+(?=[0-9])', '')` | — |
| `"numeric"`, canonical `"alnum"` | `TRIM(CAST((<existing>) AS VARCHAR))` (no zero-strip, to match the no-content-transform rule for the group) | — |

The zero-strip regex `'^0+(?=[0-9])'` is used instead of `LTRIM(col, '0')`
because `LTRIM` would over-strip an all-zero value like `"000"` down to
`""`; the regex correctly leaves `"0"`.

**Hard rule**: `_id_patch_expr` is only ever reached after Step 1 has
positively classified the group — and even then, the zero-strip branch only
fires for a member whose *own* signature is `"numeric"`. A member sampled as
`"alnum"` (UUID/Git-SHA/hex-hash-like) **never** has the regex applied to it,
regardless of the group's canonical format.

### 16e. `make_patcher` / `expression_patch` wiring

`make_patcher(table_name, groups, metadata)` returns an `expression_patch`
callable (or `None` if the table needs no changes), consumed by
`build_cleaning_script(..., expression_patch=patcher)` — applied to each
column's `ColumnExpression` just before the final column-order sort
(`profiler.py`). For a patched column, `sql_exprs` is replaced with the new
expression and `issues_handled` gets a `cross_table_alignment: aligned to
<canonical>` note (plus any `extra_note` from `_id_patch_expr`). For an
unpatchable mismatch (date/phone "needs manual review" cases), the original
expression is kept and a `cross_table_alignment_needed: ... needs manual
review` note is appended instead.

Each patched table is re-validated (AST) and re-dry-run; if `safe_to_lock`
becomes `False` or AST validation fails, that table's analysis is left
**unchanged** (best-effort — Stage 0.5 never makes a table worse than its
v3.0 result).

### 16f. Project-level summary & review UI

`build_summary(groups)` produces, per group:

```json
{
  "group_type": "date | phone | id",
  "label": "...",
  "canonical_format": "...",
  "canonical_reason": "...",
  "tables_matching": ["..."],
  "tables_needing_patch": ["..."]
}
```

Persisted as `Project.cross_table_summary_json` (`db/migrations/add_projects.sql`:
`cross_table_summary_json TEXT NOT NULL DEFAULT '[]'`), exposed via
`GET /api/projects/{id}` as `cross_table_summary` (`app/api/projects.py`),
typed in `frontend/lib/api.ts` as `CrossTableGroup[]`, and rendered in
`frontend/app/projects/[id]/page.tsx` as a "Cross-table consistency" card
above the per-table list — listing the canonical format/reason and which
tables already matched vs. were adjusted, framed as intentional alignment
rather than a defect.

### 16g. Scope boundaries (unchanged guarantees)

- No additional LLM calls anywhere in Stage 0.5.
- No additional sampling — only metadata already gathered during each
  table's v3.0 profiling (`declared_type`, `inferred_issues`, `date_format`,
  `format_signature`) is used.
- Raw row values are never inspected by Stage 0.5; `format_signature` is a
  pattern descriptor computed once during profiling and never re-derives from
  values afterward.
- Conservative by construction: groups require 2+ different tables;
  conversions that aren't provably safe (ambiguous dates, phone country-code
  differences, UUID vs. non-UUID hyphenation mismatches) are flagged for
  manual review rather than auto-patched.

---

## 17. Known Remaining Edge Cases / Follow-ups

- **FREE_TEXT classification fixed (2026-06-14)**: `_is_free_text`
  previously compared `distinct_count` against an absolute threshold
  (`PREPROCESSING_FREE_TEXT_CARDINALITY = 10000`), but `distinct_count` is
  estimated from the naive chunk (`nunique` over ≤`PREPROCESSING_NAIVE_CHUNK_SIZE`
  rows, default 10000) and so could **never exceed 10000** — the `>10000`
  check was unreachable, and FREE_TEXT never fired for any column. Fixed by
  adding `ColumnMetadata.distinct_sample_ratio` (= `distinct_count / len(naive_chunk)`,
  computed alongside `distinct_count` in `sampler.py`) and switching
  `_is_free_text` to a ratio check: `distinct_sample_ratio >
  PREPROCESSING_FREE_TEXT_CARDINALITY_RATIO` (default `0.95`), with a
  `PREPROCESSING_FREE_TEXT_MIN_DISTINCT` (default `20`) floor on
  `distinct_count` to avoid misclassifying tiny all-unique tables.

- **`roas`-style ambiguous columns**: a column mixing a "times" multiplier
  (`3.2x`), a bare ratio (`3.2`), and a percentage (`320%`) is *genuinely*
  ambiguous — is `3.2` a multiplier or 3.2%? The LLM path can raise a
  clarification question for this; the deterministic fallback (Layer 3) has
  no clarification mechanism and currently treats it as a percentage. If a
  table is consistently falling back to deterministic cleaning for such a
  column, consider a column-specific override via the clarification UI once
  the LLM is available again.
- **LLM rate limits** (e.g. Groq free-tier `TPM` limits) are expected and
  handled — they trigger Layer 3, which is correctness-preserving but less
  nuanced than an LLM-reviewed script. `PREPROCESSING_DEBUG_LOG=true` +
  `debug_logs/` makes it easy to confirm which layer produced a given table's
  script and why.

---

---

# Appendix — Original v1.1 Spec (Historical Reference)

> Everything below this line is the **original planning document** the
> implementation was built from. It is retained for historical context and
> traceability only. Where it differs from the sections above, **the sections
> above describe the actual, current behavior** — this appendix does not.

## Agent Instructions: Hybrid Data Pre-Processing Layer

## Clarum Insights — MVP Implementation Spec

**Version:** 1.1 (Production Ready)
**Target Codebase:** Clarum Insights (FastAPI + DuckDB + Next.js)
**Scope:** Implement a hybrid metadata + stratified sample data pre-processing layer that runs before Stage 1 of the existing pipeline. This is a new Stage 0 — it does not replace or modify any existing stage.

---

### 0. Context — What Exists, What You Are Adding

#### Existing Pipeline (DO NOT MODIFY)

* **Stage 1** → `data_engine.py` (`detect_schema()`, `normalise_string_dimensions()`)
* **Stage 2** → `semantic_engine.py` (keyword-score heuristics)
* **Stage 3** → `understanding_engine.py` + `llm_engine.py`
* **Stage 3.5**→ User blueprint review
* **Stage 4** → `clarification_engine.py`
* **Stage 5** → `goal_engine.py`
* **Stage 6** → `context_engine.py`
* **Stage 7** → `confirmation_engine.py`
* **Stage 8** → `dashboard_engine.py`
* **Stage 9** → `sql_safety.py` + DuckDB execution
* **Stage 10** → `insight_engine.py`
* **Stage 11** → Q&A loop in `api.py`

#### What You Are Building — Stage 0

A new pre-processing layer inserted before Stage 1. It:

1. Connects to the client's database and extracts schema metadata.
2. Pulls a naive chunk of rows and creates a stratified sample locally.
3. Sends metadata + sample to the LLM to generate a DuckDB-dialect cleaning SQL script.
4. Validates the SQL via AST check.
5. Runs a dry-run preview and shows a Data Quality Diff to the user.
6. On user confirmation, locks the SQL permanently to `agent_memory`.
7. Executes the locked cleaning SQL on full data to produce a clean DuckDB cache.
8. Sets up a `sync_state` table to track sync status.
9. Passes clean data to Stage 1 exactly as before.

#### Guiding Principles

- **AI writes cleaning SQL once. It is locked permanently after user confirmation. Never regenerated.**
- **Raw row data never leaves the client's server in Tier 2 mode. Only metadata and sample rows (for script generation) touch our cloud in Tier 1.**
- **The stratified sample is discarded from memory after the cleaning SQL is locked. Never persisted.**
- **All existing pipeline stages remain completely unchanged.**
- **Every new function follows the existing pattern: deterministic fallback first, LLM enriches.**

*(Sections 1–13 of the original v1.1 draft — including the original
single-issue `detect_column_issues`, ratio-threshold detection, the
do-nothing `SELECT col1, col2 FROM table` fallback, and the synchronous
`analyse`/`confirm`/`status` endpoint trio — have been superseded in full by
sections 1–13 above and are omitted here to avoid duplication/confusion. See
git history for the original full text if needed.)*
