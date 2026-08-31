"""ClickHouse contribution SQL must never fold provider aggregates into t-digest."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obsalt.analysis.cluster import ClickHouseHangupClusterStore
from obsalt.analysis.contributions import ClickHouseRollupStore
from obsalt.config import Settings
from obsalt.domain.enums import (
    AnalysisState,
    HangupReason,
    MeasurementPlacement,
    Metric,
    Provenance,
    Stage,
)
from obsalt.domain.models import (
    AnalysisExecution,
    AnalysisResult,
    CallRevision,
    Hangup,
    StageMeasurement,
)
from obsalt.store.clickhouse import ClickHouseSink
from obsalt.store.postgres import PostgresInbox


class _RecordingCH:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict | None]] = []
        self.inserts: list[tuple[str, list, list | None]] = []

    def command(self, sql: str, parameters: dict | None = None) -> None:
        self.commands.append((sql, parameters))

    def insert(self, table: str, rows: list, column_names: list | None = None) -> None:
        self.inserts.append((table, rows, column_names))

    def query(self, sql: str, parameters: dict | None = None) -> object:
        return type("R", (), {"result_rows": []})()


def _revision() -> CallRevision:
    return CallRevision(
        org_id="acme",
        call_id="c1",
        revision="r1",
        source="example",
        source_call_id="s1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        hangup=Hangup(reason=HangupReason.USER_HANGUP),
        stage_measurements=[
            StageMeasurement(
                fact_id="f1",
                stage=Stage.STT,
                metric=Metric.DURATION,
                value_ms=12.0,
                placement=MeasurementPlacement.INTERVAL,
                provenance=Provenance.PROVIDER_REPORTED,
            )
        ],
    )


def test_quantile_tdigest_insert_sql_excludes_aggregates() -> None:
    client = _RecordingCH()
    store = ClickHouseRollupStore(client)
    store.contribute(_revision())
    assert any("quantileTDigestState" in sql for sql, _params in client.commands)
    assert all("aggregate" not in sql.lower() for sql, _params in client.commands)


def test_hangup_clusters_persist_payload() -> None:
    client = _RecordingCH()
    store = ClickHouseHangupClusterStore(client)
    store.refresh("acme", [_revision()], "gen-1")
    assert any(table == "hangup_clusters" for table, _rows, _cols in client.inserts)


def test_analysis_insert_persists_execution_state() -> None:
    client = _RecordingCH()
    sink = ClickHouseSink(Settings(environment="test"), client=client)
    sink.write_analysis(
        "acme",
        "c1",
        "r1",
        [
            AnalysisResult(
                execution=AnalysisExecution(
                    call_id="c1",
                    revision="r1",
                    analyzer_id="tier2",
                    analyzer_version="1",
                    rubric_version="2",
                    state=AnalysisState.BUDGET_BLOCKED,
                    error="cap",
                ),
                payload={"selection": "budget_blocked"},
            )
        ],
    )
    table, rows, cols = next(item for item in client.inserts if item[0] == "analysis_results")
    assert cols is not None
    assert rows[0][cols.index("state")] == "budget_blocked"
    assert rows[0][cols.index("error")] == "cap"
    assert rows[0][cols.index("rubric_version")] == "2"

    client.query = lambda sql, parameters=None: type(
        "R",
        (),
        {
            "result_rows": [
                (
                    "c1",
                    "r1",
                    "tier2",
                    "1",
                    '{"selection":"budget_blocked"}',
                    "budget_blocked",
                    "cap",
                    "2",
                )
            ]
        },
    )()
    loaded = sink.list_analysis("acme", "c1", "r1")
    assert loaded[0].execution.state is AnalysisState.BUDGET_BLOCKED
    assert loaded[0].payload.get("passed") is None


def test_fair_claim_sql_partitions_by_org() -> None:
    source = Path(PostgresInbox.claim_outbox.__code__.co_filename).read_text()
    assert "PARTITION BY org_id" in source
    assert "ROW_NUMBER()" in source
    # FOR UPDATE must not share a SELECT with the window function; Postgres
    # rejects that (FeatureNotSupported) and crash-looped the worker.
    assert "FOR UPDATE OF o SKIP LOCKED" in source
    assert "FOR UPDATE SKIP LOCKED\n                    ) ranked" not in source
