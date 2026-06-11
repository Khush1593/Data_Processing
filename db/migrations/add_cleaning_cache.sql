-- ======================================================================
-- agent_memory (process.md §10 confirm step) — PostgreSQL dialect.
-- Permanent store for the LOCKED cleaning SQL. Once written here on user
-- confirmation it is never regenerated (Guiding Principle, process.md §0).
-- The unique key (project_id, domain, topic) makes the lock idempotent.
-- ======================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS agent_memory (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    user_id         TEXT,
    project_id      TEXT NOT NULL,
    domain          TEXT NOT NULL,        -- e.g. 'Business_Logic'
    topic           TEXT NOT NULL,        -- e.g. 'cleaning_script_<table_name>'
    content         TEXT NOT NULL,        -- the locked DuckDB cleaning SQL
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, domain, topic)
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_topic
    ON agent_memory (project_id, topic);
