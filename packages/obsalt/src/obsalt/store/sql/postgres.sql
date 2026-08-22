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
    lease_owner TEXT
);

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
