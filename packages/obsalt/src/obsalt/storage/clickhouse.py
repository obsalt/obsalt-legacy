"""Immutable ClickHouse facts. Complete revisions only — no last-write-wins."""

from __future__ import annotations

from urllib.parse import urlparse

from obsalt.domain.models import CallRevision
from obsalt.storage.sql import CLICKHOUSE_SCHEMA


class ClickHouseFacts:
    def __init__(
        self,
        url: str,
        database: str,
        username: str = "obsalt",
        password: str = "obsalt",
    ) -> None:
        import clickhouse_connect

        parsed = urlparse(url)
        self.client = clickhouse_connect.get_client(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 8123,
            username=username,
            password=password,
            database=database,
        )
        for statement in (part.strip() for part in CLICKHOUSE_SCHEMA.split(";") if part.strip()):
            self.client.command(statement)

    def write_revision(self, revision: CallRevision) -> None:
        created = revision.lifecycle.started_at or revision.lifecycle.ended_at
        created_s = created.isoformat() if created else "1970-01-01T00:00:00+00:00"
        self.client.insert(
            "call_revisions",
            [
                [
                    revision.org_id,
                    revision.call_id,
                    revision.revision,
                    revision.identity.source,
                    revision.identity.source_call_id,
                    revision.identity.agent_id,
                    revision.model_dump_json(),
                    revision.processing_run_id,
                    revision.assembler_version,
                    created_s,
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
                "processing_run_id",
                "assembler_version",
                "created_at",
            ],
        )
        if revision.stage_measurements:
            rows = []
            for item in revision.stage_measurements:
                rows.append(
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
                        item.started_at,
                        item.ended_at,
                        item.provenance.value,
                        item.source_path or "",
                        item.derivation or "",
                    ]
                )
            self.client.insert(
                "stage_measurements",
                rows,
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

    def verify(self, revision: CallRevision) -> None:
        result = self.client.query(
            "SELECT revision FROM call_revisions "
            "WHERE org_id = {org:String} AND call_id = {cid:String} AND revision = {rev:UInt32} LIMIT 1",
            parameters={
                "org": revision.org_id,
                "cid": revision.call_id,
                "rev": revision.revision,
            },
        )
        if not result.result_rows:
            raise RuntimeError("candidate revision is not query-visible in ClickHouse")

    def get_revision(self, org_id: str, call_id: str, revision: int) -> CallRevision | None:
        result = self.client.query(
            "SELECT payload FROM call_revisions "
            "WHERE org_id = {org:String} AND call_id = {cid:String} AND revision = {rev:UInt32} LIMIT 1",
            parameters={"org": org_id, "cid": call_id, "rev": revision},
        )
        if not result.result_rows:
            return None
        return CallRevision.model_validate_json(result.result_rows[0][0])
