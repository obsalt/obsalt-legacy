-- Immutable call revisions. Postgres holds the active pointer; never query CH for "latest".
CREATE DATABASE IF NOT EXISTS obsalt;
CREATE TABLE IF NOT EXISTS obsalt.call_revisions (
    org_id String,
    call_id String,
    revision String,
    source String,
    source_call_id String,
    agent_id String,
    payload String,
    created_at DateTime64(3)
) ENGINE = MergeTree
ORDER BY (org_id, call_id, revision);

CREATE TABLE IF NOT EXISTS obsalt.turns (
    org_id String,
    call_id String,
    revision String,
    turn_index Int32,
    speaker String,
    started_at Nullable(DateTime64(3)),
    ended_at Nullable(DateTime64(3))
) ENGINE = MergeTree
PARTITION BY toYYYYMM(coalesce(started_at, toDateTime64('1970-01-01', 3)))
ORDER BY (org_id, call_id, turn_index);

CREATE TABLE IF NOT EXISTS obsalt.stage_measurements (
    org_id String,
    call_id String,
    revision String,
    fact_id String,
    stage String,
    metric String,
    value_ms Float64,
    turn_index Nullable(Int32),
    placement String,
    started_at Nullable(DateTime64(3)),
    ended_at Nullable(DateTime64(3)),
    provenance String,
    source_path String,
    derivation String
) ENGINE = MergeTree
ORDER BY (org_id, call_id, revision, fact_id);

CREATE TABLE IF NOT EXISTS obsalt.aggregate_measurements (
    org_id String,
    call_id String,
    revision String,
    fact_id String,
    stage String,
    metric String,
    statistic String,
    value_ms Float64,
    provenance String,
    source_path String
) ENGINE = MergeTree
ORDER BY (org_id, call_id, revision, fact_id);

CREATE TABLE IF NOT EXISTS obsalt.tool_invocations (
    org_id String,
    call_id String,
    revision String,
    tool_id String,
    name String,
    status String,
    duration_ms Nullable(Float64)
) ENGINE = MergeTree
ORDER BY (org_id, call_id, revision, tool_id);

CREATE TABLE IF NOT EXISTS obsalt.rollup_contributions (
    org_id String,
    agent_id String,
    call_id String,
    revision String,
    stage String,
    metric String,
    value_ms Float64,
    bucket DateTime64(3),
    generation String
) ENGINE = MergeTree
ORDER BY (org_id, agent_id, stage, metric, bucket, call_id, revision);

CREATE TABLE IF NOT EXISTS obsalt.analysis_results (
    org_id String,
    call_id String,
    revision String,
    analyzer_id String,
    analyzer_version String,
    payload String,
    created_at DateTime64(3)
) ENGINE = MergeTree
ORDER BY (org_id, call_id, revision, analyzer_id, analyzer_version);
