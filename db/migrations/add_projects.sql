-- ======================================================================
-- Stage 0 review-UI control-plane tables — PostgreSQL dialect.
-- Idempotent: safe to run more than once.
-- ======================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    db_uri          TEXT NOT NULL,
    source_schema   TEXT,
    status          TEXT NOT NULL DEFAULT 'analyzing',  -- 'analyzing' | 'ready' | 'approving' | 'completed' | 'failed'
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS table_analyses (
    id                          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    project_id                  TEXT NOT NULL,
    table_name                  TEXT NOT NULL,
    metadata_json               TEXT NOT NULL,
    cleaning_sql                TEXT NOT NULL,
    explanation                 TEXT NOT NULL,
    columns_transformed_json    TEXT NOT NULL,
    diff_json                   TEXT NOT NULL,
    script_source               TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'analyzed', -- 'analyzed' | 'approved' | 'cold_start_running' | 'cold_start_done' | 'failed'
    cold_start_error            TEXT,
    clarifications_json         TEXT NOT NULL DEFAULT '[]',
    clarification_answers_json  TEXT NOT NULL DEFAULT '{}',
    created_at                  TIMESTAMPTZ DEFAULT now(),
    updated_at                  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, table_name)
);

ALTER TABLE table_analyses ADD COLUMN IF NOT EXISTS clarifications_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE table_analyses ADD COLUMN IF NOT EXISTS clarification_answers_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_table_analyses_project
    ON table_analyses (project_id);
