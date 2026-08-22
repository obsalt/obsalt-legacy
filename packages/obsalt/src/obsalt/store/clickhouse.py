"""ClickHouse: immutable call revisions and narrow fact tables. Never query 'latest'."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import clickhouse_connect

from obsalt.config import Settings
from obsalt.domain.models import AnalysisResult, CallRevision
from obsalt.util import canonical_json, utcnow
from obsalt.worker.process import RevisionSink

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


def _ch_dt(value: datetime | None) -> datetime | None:
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


def apply_schema(client: Any) -> None:
    for statement in _sql_statements(SCHEMA_PATH.read_text()):
        client.command(statement)


def _sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            buf = []
            if stmt:
                statements.append(stmt)
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


class ClickHouseSink(RevisionSink):
    """Writes a complete CallRevision payload plus narrow facts. GET is exact-revision only."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._client = client or clickhouse_client(settings)
        self.revisions: dict[tuple[str, str, str], CallRevision] = {}
        self.analysis: dict[tuple[str, str, str], list[AnalysisResult]] = {}

    def ping(self) -> None:
        self._client.command("SELECT 1")

    def write(self, revision: CallRevision) -> None:
        created = _ch_dt(revision.created_at) or _ch_dt(utcnow())
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
                        _ch_dt(turn.started_at),
                        _ch_dt(turn.ended_at),
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
                        _ch_dt(item.started_at),
                        _ch_dt(item.ended_at),
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
                SELECT call_id, revision, analyzer_id, analyzer_version, payload
                FROM analysis_results
                WHERE org_id = {org:String}
                  AND ({cid:String} = '' OR call_id = {cid:String})
                  AND ({rev:String} = '' OR revision = {rev:String})
                """,
                parameters={"org": org_id, "cid": call_id or "", "rev": revision or ""},
            )
        except Exception:
            return self._memory_analysis(org_id, call_id, revision)
        rows: list[AnalysisResult] = []
        for call, rev, analyzer_id, analyzer_version, payload in result.result_rows:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            parsed = payload
            if isinstance(payload, str):
                import json

                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = {"raw": payload}
            rows.append(
                AnalysisResult(
                    execution=AnalysisExecution(
                        call_id=str(call),
                        revision=str(rev),
                        analyzer_id=str(analyzer_id),
                        analyzer_version=str(analyzer_version),
                        state=AnalysisState.COMPLETED,
                    ),
                    payload=parsed if isinstance(parsed, dict) else {"value": parsed},
                )
            )
        if rows:
            return rows
        return self._memory_analysis(org_id, call_id, revision)

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
        now = _ch_dt(utcnow())
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


ClickHouseRevisionSink = ClickHouseSink
