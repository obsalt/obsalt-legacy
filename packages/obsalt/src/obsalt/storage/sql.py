"""Postgres DDL. Inbox/dedupe/outbox live here; ClickHouse holds immutable facts."""

POSTGRES_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingest_keys (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    provider TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    provider TEXT NOT NULL,
    name TEXT NOT NULL,
    credentials_encrypted BYTEA NOT NULL,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw_envelopes (
    envelope_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    delivery_key TEXT NOT NULL,
    state TEXT NOT NULL,
    event_kind TEXT,
    received_at TIMESTAMPTZ NOT NULL,
    assembled_revision INT,
    headers JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (org_id, delivery_key)
);

CREATE TABLE IF NOT EXISTS outbox (
    id BIGSERIAL PRIMARY KEY,
    envelope_id TEXT NOT NULL REFERENCES raw_envelopes(envelope_id),
    org_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    leased_until TIMESTAMPTZ,
    lease_owner TEXT,
    attempts INT NOT NULL DEFAULT 0,
    done_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_call_id TEXT,
    caller_token TEXT,
    range_start TIMESTAMPTZ,
    range_end TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS active_revisions (
    org_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    revision INT NOT NULL,
    source TEXT NOT NULL,
    source_call_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    status TEXT,
    hangup_reason TEXT,
    timeline_fidelity TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, call_id)
);

CREATE TABLE IF NOT EXISTS rollup_generations (
    org_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    generation BIGINT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, kind)
);

CREATE TABLE IF NOT EXISTS search_documents (
    org_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    revision INT NOT NULL,
    transcript TEXT NOT NULL,
    tsv TSVECTOR,
    embedding VECTOR(384),
    index_version TEXT NOT NULL,
    embedder_version TEXT,
    facets JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (org_id, call_id)
);

CREATE TABLE IF NOT EXISTS rubrics (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    name TEXT NOT NULL,
    version INT NOT NULL,
    body TEXT NOT NULL,
    threshold DOUBLE PRECISION NOT NULL DEFAULT 0.7,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, name, version)
);

CREATE TABLE IF NOT EXISTS analysis_executions (
    org_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    revision INT NOT NULL,
    analyzer_id TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    rubric_version TEXT,
    prompt_version TEXT,
    judge_version TEXT,
    content_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    error TEXT,
    PRIMARY KEY (org_id, call_id, revision, analyzer_id, analyzer_version)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    org_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS llm_spend (
    org_id TEXT PRIMARY KEY,
    month TEXT NOT NULL,
    spent_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    cap_usd DOUBLE PRECISION NOT NULL DEFAULT 0
);
"""

CLICKHOUSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_revisions (
    org_id String,
    call_id String,
    revision UInt32,
    source LowCardinality(String),
    source_call_id String,
    agent_id String,
    payload String,
    processing_run_id String,
    assembler_version LowCardinality(String),
    created_at DateTime64(3, 'UTC')
) ENGINE = MergeTree
ORDER BY (org_id, call_id, revision);

CREATE TABLE IF NOT EXISTS turns (
    org_id String,
    call_id String,
    revision UInt32,
    turn_index UInt32,
    speaker LowCardinality(String),
    text_ref String,
    started_at Nullable(DateTime64(3, 'UTC')),
    ended_at Nullable(DateTime64(3, 'UTC')),
    interrupted Nullable(UInt8),
    confidence Nullable(Float64)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(coalesce(started_at, toDateTime64('1970-01-01', 3, 'UTC')))
ORDER BY (org_id, call_id, revision, turn_index);

CREATE TABLE IF NOT EXISTS stage_measurements (
    org_id String,
    call_id String,
    revision UInt32,
    fact_id String,
    stage LowCardinality(String),
    metric LowCardinality(String),
    value_ms Float64,
    turn_index Nullable(Int32),
    placement LowCardinality(String),
    started_at Nullable(DateTime64(3, 'UTC')),
    ended_at Nullable(DateTime64(3, 'UTC')),
    provenance LowCardinality(String),
    source_path String,
    derivation String
) ENGINE = MergeTree
PARTITION BY toYYYYMM(coalesce(started_at, toDateTime64('1970-01-01', 3, 'UTC')))
ORDER BY (org_id, call_id, revision, stage, metric);

CREATE TABLE IF NOT EXISTS aggregate_measurements (
    org_id String,
    call_id String,
    revision UInt32,
    fact_id String,
    stage LowCardinality(String),
    metric LowCardinality(String),
    statistic LowCardinality(String),
    value_ms Float64,
    population Nullable(UInt32),
    provenance LowCardinality(String),
    source_path String
) ENGINE = MergeTree
ORDER BY (org_id, call_id, revision, stage, statistic);

CREATE TABLE IF NOT EXISTS tool_invocations (
    org_id String,
    call_id String,
    revision UInt32,
    fact_id String,
    tool_id String,
    name String,
    turn_index Nullable(Int32),
    duration_ms Nullable(Float64),
    status LowCardinality(String),
    retry_count UInt32,
    argument_hash String
) ENGINE = MergeTree
ORDER BY (org_id, call_id, revision, name);

CREATE TABLE IF NOT EXISTS analysis_results (
    org_id String,
    call_id String,
    revision UInt32,
    analyzer_id String,
    analyzer_version String,
    kind LowCardinality(String),
    passed Nullable(UInt8),
    score Nullable(Float64),
    label String,
    rationale String,
    payload String
) ENGINE = MergeTree
ORDER BY (org_id, call_id, revision, analyzer_id);

CREATE TABLE IF NOT EXISTS latency_contrib (
    org_id String,
    agent_id String,
    stage LowCardinality(String),
    metric LowCardinality(String),
    bucket DateTime,
    value_ms Float64,
    revision UInt32
) ENGINE = MergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY (org_id, agent_id, stage, bucket);
"""
