"""ClickHouse: immutable call revisions and narrow fact tables. Never query 'latest'."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import clickhouse_connect

from obsalt.analysis.rollups import latest_analysis_results
from obsalt.config import Settings
from obsalt.domain.models import AnalysisResult, CallRevision
from obsalt.store.sql_script import sql_statements
from obsalt.util import canonical_json, utcnow

SCHEMA_PATH = Path(__file__).resolve().parent / "sql" / "clickhouse.sql"

# Tables keyed by call_id. hangup_clusters and stage_quantile_states are not.
FACT_TABLES = (
    "call_revisions",
    "turns",
    "stage_measurements",
    "aggregate_measurements",
    "tool_invocations",
    "analysis_results",
    "rollup_contributions",
)


def as_clickhouse_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def clickhouse_client(settings: Settings) -> Any:
    parsed = urlparse(settings.clickhouse_url)
    return clickhouse_connect.get_client(
        host=parsed.hostname or "localhost",
        port=parsed.port or 8123,
        username=unquote(parsed.username) if parsed.username else "default",
        password=unquote(parsed.password) if parsed.password else "",
        database=settings.clickhouse_database,
        secure=parsed.scheme == "https",
    )


class _SyncClientGuard:
    """Serialize every call on a clickhouse_connect synchronous client.

    The synchronous client keeps a single HTTP session and is not safe for
    concurrent use across threads. The worker runs tier-2 analysis in a thread
    while the main loop drains the outbox, and the API serves webhook
    background tasks from a threadpool; both reach the same shared client.
    Uncoordinated use raises ``ProgrammingError: Attempt to execute concurrent
    queries within the same session``. A single lock prevents two threads from
    using the session at once while keeping one client instance (so the
    derived stores that receive this client are covered too).
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._lock = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if callable(attr):

            def _guarded(*args: Any, **kwargs: Any) -> Any:
                with self._lock:
                    return attr(*args, **kwargs)

            return _guarded
        return attr


def apply_schema(client: Any) -> None:
    for statement in sql_statements(SCHEMA_PATH.read_text()):
        client.command(statement)


class ClickHouseSink:
    """Writes a complete CallRevision payload plus narrow facts. GET is exact-revision only."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._client = _SyncClientGuard(client or clickhouse_client(settings))
        self.revisions: dict[tuple[str, str, str], CallRevision] = {}
        self.analysis: dict[tuple[str, str, str], list[AnalysisResult]] = {}

    def ping(self) -> None:
        self._client.command("SELECT 1")

    def verify_visible(self, revision: CallRevision) -> CallRevision:
        from obsalt.worker.process import verify_revision_visible

        return verify_revision_visible(self, revision)

    def write(self, revision: CallRevision) -> None:
        created = as_clickhouse_datetime(revision.created_at) or as_clickhouse_datetime(utcnow())
        self._client.insert(
            "call_revisions",
            [
                [
                    revision.org_id,
                    revision.call_id,
                    revision.revision,
                    revision.source,
                    revision.source_call_id,
                    revision.agent_id,
                    revision.model_dump_json(),
                    created,
                ]
            ],
            column_names=[
                "org_id",
                "call_id",
                "revision",
                "source",
                "source_call_id",
                "agent_id",
                "payload",
                "created_at",
            ],
        )
        if revision.turns:
            self._client.insert(
                "turns",
                [
                    [
                        revision.org_id,
                        revision.call_id,
                        revision.revision,
                        turn.index,
                        turn.speaker.value,
                        as_clickhouse_datetime(turn.started_at),
                        as_clickhouse_datetime(turn.ended_at),
                    ]
                    for turn in revision.turns
                ],
                column_names=[
                    "org_id",
                    "call_id",
                    "revision",
                    "turn_index",
                    "speaker",
                    "started_at",
                    "ended_at",
                ],
            )
        if revision.stage_measurements:
            self._client.insert(
                "stage_measurements",
                [
                    [
                        revision.org_id,
                        revision.call_id,
                        revision.revision,
                        item.fact_id,
                        item.stage.value,
                        item.metric.value,
                        item.value_ms,
                        item.turn_index,
                        item.placement.value,
                        as_clickhouse_datetime(item.started_at),
                        as_clickhouse_datetime(item.ended_at),
                        item.provenance.value,
                        item.source_path or "",
                        item.derivation or "",
                    ]
                    for item in revision.stage_measurements
                ],
                column_names=[
                    "org_id",
                    "call_id",
                    "revision",
                    "fact_id",
                    "stage",
                    "metric",
                    "value_ms",
                    "turn_index",
                    "placement",
                    "started_at",
                    "ended_at",
                    "provenance",
                    "source_path",
                    "derivation",
                ],
            )
        if revision.aggregate_measurements:
            self._client.insert(
                "aggregate_measurements",
                [
                    [
                        revision.org_id,
                        revision.call_id,
                        revision.revision,
                        item.fact_id,
                        item.stage.value,
                        item.metric.value,
                        item.statistic.value,
                        item.value_ms,
                        item.provenance.value,
                        item.source_path or "",
                    ]
                    for item in revision.aggregate_measurements
                ],
                column_names=[
                    "org_id",
                    "call_id",
                    "revision",
                    "fact_id",
                    "stage",
                    "metric",
                    "statistic",
                    "value_ms",
                    "provenance",
                    "source_path",
                ],
            )
        if revision.tools:
            self._client.insert(
                "tool_invocations",
                [
                    [
                        revision.org_id,
                        revision.call_id,
                        revision.revision,
                        tool.id,
                        tool.name,
                        tool.status.value,
                        tool.duration_ms,
                    ]
                    for tool in revision.tools
                ],
                column_names=[
                    "org_id",
                    "call_id",
                    "revision",
                    "tool_id",
                    "name",
                    "status",
                    "duration_ms",
                ],
            )
        loaded = self.get(revision.org_id, revision.call_id, revision.revision)
        if loaded is None:
            raise RuntimeError("clickhouse write is not query-visible")
        self.revisions[(revision.org_id, revision.call_id, revision.revision)] = revision

    def list_analysis(
        self,
        org_id: str,
        call_id: str | None = None,
        revision: str | None = None,
    ) -> list[AnalysisResult]:
        from obsalt.domain.enums import AnalysisState
        from obsalt.domain.models import AnalysisExecution

        try:
            result = self._client.query(
                """
                SELECT call_id, revision, analyzer_id, analyzer_version, payload,
                       state, error, rubric_version, created_at
                FROM analysis_results
                WHERE org_id = {org:String}
                  AND ({cid:String} = '' OR call_id = {cid:String})
                  AND ({rev:String} = '' OR revision = {rev:String})
                ORDER BY created_at ASC
                """,
                parameters={"org": org_id, "cid": call_id or "", "rev": revision or ""},
            )
        except Exception:
            return self._memory_analysis(org_id, call_id, revision)
        rows: list[AnalysisResult] = []
        for row in result.result_rows:
            call, rev, analyzer_id, analyzer_version, payload, state, error, rubric_version = (
                _analysis_row(row)
            )
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            parsed = payload
            if isinstance(payload, str):
                import json

                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = {"raw": payload}
            try:
                exec_state = AnalysisState(str(state or AnalysisState.COMPLETED.value))
            except ValueError:
                exec_state = AnalysisState.COMPLETED
            rows.append(
                AnalysisResult(
                    execution=AnalysisExecution(
                        call_id=str(call),
                        revision=str(rev),
                        analyzer_id=str(analyzer_id),
                        analyzer_version=str(analyzer_version),
                        rubric_version=str(rubric_version) or None,
                        state=exec_state,
                        error=str(error) or None,
                    ),
                    payload=parsed if isinstance(parsed, dict) else {"value": parsed},
                )
            )
        if rows:
            return latest_analysis_results(rows)
        return latest_analysis_results(self._memory_analysis(org_id, call_id, revision))

    def _memory_analysis(
        self,
        org_id: str,
        call_id: str | None,
        revision: str | None,
    ) -> list[AnalysisResult]:
        if call_id and revision:
            return list(self.analysis.get((org_id, call_id, revision), []))
        out: list[AnalysisResult] = []
        for (stored_org, stored_call, stored_rev), values in self.analysis.items():
            if stored_org != org_id:
                continue
            if call_id and stored_call != call_id:
                continue
            if revision and stored_rev != revision:
                continue
            out.extend(values)
        return out

    def get(self, org_id: str, call_id: str, revision: str) -> CallRevision | None:
        result = self._client.query(
            """
            SELECT payload FROM call_revisions
            WHERE org_id = {org:String}
              AND call_id = {cid:String}
              AND revision = {rev:String}
            LIMIT 1
            """,
            parameters={"org": org_id, "cid": call_id, "rev": revision},
        )
        if not result.result_rows:
            return self.revisions.get((org_id, call_id, revision))
        payload = result.result_rows[0][0]
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return CallRevision.model_validate_json(payload)

    def write_analysis(
        self,
        org_id: str,
        call_id: str,
        revision: str,
        results: list[AnalysisResult],
    ) -> None:
        self.analysis[(org_id, call_id, revision)] = results
        if not results:
            return
        now = as_clickhouse_datetime(utcnow())
        self._client.insert(
            "analysis_results",
            [
                [
                    org_id,
                    call_id,
                    revision,
                    item.execution.analyzer_id,
                    item.execution.analyzer_version,
                    canonical_json(item.payload),
                    item.execution.state.value,
                    item.execution.error or "",
                    item.execution.rubric_version or "",
                    now,
                ]
                for item in results
            ],
            column_names=[
                "org_id",
                "call_id",
                "revision",
                "analyzer_id",
                "analyzer_version",
                "payload",
                "state",
                "error",
                "rubric_version",
                "created_at",
            ],
        )

    def mask_call(self, org_id: str, call_id: str) -> None:
        """Prompt privacy mask. Physical removal is delete_call."""
        self._client.command(
            """
            ALTER TABLE call_revisions
            UPDATE payload = {p:String}
            WHERE org_id = {org:String} AND call_id = {cid:String}
            """,
            parameters={"p": '{"redacted":true}', "org": org_id, "cid": call_id},
        )
        for key in [k for k in self.revisions if k[0] == org_id and k[1] == call_id]:
            self.revisions.pop(key, None)
            self.analysis.pop(key, None)

    def delete_call(self, org_id: str, call_id: str) -> None:
        self.mask_call(org_id, call_id)
        for table in FACT_TABLES:
            self._client.command(
                f"ALTER TABLE {table} DELETE WHERE org_id = {{org:String}} AND call_id = {{cid:String}}",
                parameters={"org": org_id, "cid": call_id},
            )

    def list_for_call(self, org_id: str, call_id: str) -> list[CallRevision]:
        result = self._client.query(
            """
            SELECT payload FROM call_revisions
            WHERE org_id = {org:String} AND call_id = {cid:String}
            """,
            parameters={"org": org_id, "cid": call_id},
        )
        out: list[CallRevision] = []
        for row in result.result_rows:
            payload = row[0].decode("utf-8") if isinstance(row[0], bytes) else row[0]
            out.append(CallRevision.model_validate_json(payload))
        return out


def _analysis_row(row: Any) -> tuple[Any, ...]:
    """New rows are 8+ columns. Older inserts were payload-only (5 columns)."""
    values = tuple(row)
    if len(values) >= 8:
        return values[:8]
    call, rev, analyzer_id, analyzer_version, payload = values[:5]
    return (call, rev, analyzer_id, analyzer_version, payload, "completed", "", "")
