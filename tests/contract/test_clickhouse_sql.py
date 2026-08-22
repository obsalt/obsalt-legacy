"""ClickHouse contribution SQL must never fold provider aggregates into t-digest."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obsalt.analysis.cluster import ClickHouseHangupClusterStore
from obsalt.analysis.contributions import ClickHouseRollupStore
from obsalt.domain.enums import HangupReason, MeasurementPlacement, Metric, Provenance, Stage
from obsalt.domain.models import CallRevision, Hangup, StageMeasurement
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


def test_fair_claim_sql_partitions_by_org() -> None:
    source = Path(PostgresInbox.claim_outbox.__code__.co_filename).read_text()
    assert "PARTITION BY o.org_id" in source
    assert "ROW_NUMBER()" in source
