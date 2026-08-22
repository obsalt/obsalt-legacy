-- Postgres inbox, tenancy, search, active revisions.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    key_hash TEXT NOT NULL UNIQUE,
    scopes TEXT[] NOT NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    provider TEXT NOT NULL,
    ingest_key_hash TEXT NOT NULL UNIQUE,
    secrets_ciphertext BYTEA NOT NULL,
    settings JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw_envelopes (
    envelope_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    delivery_key TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    event_kind TEXT,
    source_call_id TEXT,
    headers JSONB NOT NULL DEFAULT '{}',
    received_at TIMESTAMPTZ NOT NULL,
    UNIQUE (org_id, delivery_key)
);

CREATE TABLE IF NOT EXISTS outbox (
    id BIGSERIAL PRIMARY KEY,
    envelope_id TEXT NOT NULL REFERENCES raw_envelopes(envelope_id),
    org_id TEXT NOT NULL,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INT NOT NULL DEFAULT 0,
    leased_until TIMESTAMPTZ,
    lease_owner TEXT,
    last_error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS outbox_envelope_id_uidx ON outbox (envelope_id);

CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    source_call_id TEXT,
    caller_token TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS active_calls (
    org_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    fact_frontier TEXT[] NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, call_id)
);

CREATE TABLE IF NOT EXISTS rollup_generations (
    name TEXT PRIMARY KEY,
    generation TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS search_documents (
    org_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    body TEXT NOT NULL,
    tsv TSVECTOR,
    embedding VECTOR(256),
    index_version TEXT NOT NULL,
    PRIMARY KEY (org_id, call_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    org_id TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rubrics (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    threshold DOUBLE PRECISION NOT NULL DEFAULT 0.7,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_destinations (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    url TEXT NOT NULL,
    secret_ciphertext BYTEA NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deletion_requests (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    call_id TEXT,
    source_call_id TEXT,
    caller_token TEXT,
    status TEXT NOT NULL DEFAULT 'accepted',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS processing_runs (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    envelope_id TEXT,
    decoder_version TEXT,
    assembler_version TEXT,
    analyzer_version TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, email)
);

CREATE TABLE IF NOT EXISTS trace_assemblies (
    org_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    rooted BOOLEAN NOT NULL DEFAULT FALSE,
    root_ended_at TIMESTAMPTZ,
    call_id TEXT,
    events JSONB NOT NULL DEFAULT '[]',
    finalized_at TIMESTAMPTZ,
    mapper_name TEXT,
    unrooted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (org_id, trace_id)
);

CREATE TABLE IF NOT EXISTS otlp_forward_outbox (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    content_type TEXT NOT NULL,
    destination_id TEXT,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    delivered_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS otlp_span_identities (
    org_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, trace_id, span_id)
);

CREATE TABLE IF NOT EXISTS quality_reviews (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    call_id TEXT NOT NULL,
    agree BOOLEAN NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decode_dlq (
    envelope_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_outbox (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    destination_id TEXT,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    call_id TEXT,
    revision TEXT,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    delivered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS tombstones_org_call ON tombstones (org_id, source_call_id);
CREATE INDEX IF NOT EXISTS outbox_lease ON outbox (available_at, leased_until);
CREATE INDEX IF NOT EXISTS search_documents_tsv ON search_documents USING gin (tsv);
CREATE INDEX IF NOT EXISTS search_documents_hnsw ON search_documents USING hnsw (embedding vector_cosine_ops);

ALTER TABLE search_documents ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE search_documents ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE search_documents ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE search_documents ADD COLUMN IF NOT EXISTS hangup_reason TEXT;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE webhook_destinations ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS call_id TEXT;
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS envelope_id TEXT;
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS analyzer_version TEXT;
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'running';
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
ALTER TABLE trace_assemblies ADD COLUMN IF NOT EXISTS mapper_name TEXT;
ALTER TABLE trace_assemblies ADD COLUMN IF NOT EXISTS unrooted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE trace_assemblies ADD COLUMN IF NOT EXISTS late_after_finalize BOOLEAN NOT NULL DEFAULT FALSE;

