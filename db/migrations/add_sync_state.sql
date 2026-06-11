-- ======================================================================
-- Stage 0 control-plane tables (process.md §2a) — PostgreSQL dialect.
-- Adapted from the spec's SQLite DDL:
--   * lower(hex(randomblob(16)))  ->  gen_random_uuid()::text
--   * CURRENT_TIMESTAMP            ->  now() (timestamptz)
-- Idempotent: safe to run more than once.
-- ======================================================================

-- gen_random_uuid() lives in pgcrypto on older servers; built-in on PG13+.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS sync_state (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    project_id      TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    sync_mode       TEXT NOT NULL,        -- 'append_only' | 'upsert' | 'full_resync' | 'delete_aware'
    last_sync_utc   TIMESTAMPTZ,
    last_row_count  INTEGER,              -- for reconciliation check
    status          TEXT NOT NULL,        -- 'completed' | 'in_progress' | 'failed'
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, table_name)
);

CREATE TABLE IF NOT EXISTS cold_start_progress (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    project_id      TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    last_chunk_id   TEXT,                 -- last PK value or offset window completed
    chunks_done     INTEGER DEFAULT 0,
    total_chunks    INTEGER,
    status          TEXT NOT NULL,        -- 'in_progress' | 'completed'
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, table_name)
);

CREATE INDEX IF NOT EXISTS idx_sync_state_lookup
    ON sync_state (project_id, table_name, status);
