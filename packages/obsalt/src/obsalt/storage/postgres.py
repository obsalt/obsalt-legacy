"""Postgres: inbox, dedupe, outbox, tenants, active-revision pointers, search."""

from __future__ import annotations

import json
from typing import Any

from obsalt.crypto.envelope import encrypt_secret
from obsalt.crypto.keys import hash_ingest_key
from obsalt.domain.enums import EnvelopeState
from obsalt.ingest.receive import Tombstoned
from obsalt.plugin.protocol import ConnectionConfig, RawEnvelope
from obsalt.storage.sql import POSTGRES_SCHEMA


class PostgresStore:
    def __init__(self, dsn: str, *, master_key: str = "") -> None:
        import psycopg

        self.master_key = master_key
        self.conn = psycopg.connect(dsn, autocommit=False)
        self.conn.execute(POSTGRES_SCHEMA)
        self.conn.commit()
        self.envelopes: dict[str, RawEnvelope] = {}
        self._load_envelopes()

    def _load_envelopes(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT envelope_id, org_id, provider, connection_id, object_key, "
                "delivery_key, event_kind, received_at, headers FROM raw_envelopes"
            )
            for row in cur.fetchall():
                self.envelopes[row[0]] = RawEnvelope(
                    envelope_id=row[0],
                    org_id=row[1],
                    provider=row[2],
                    connection_id=row[3],
                    object_key=row[4],
                    delivery_key=row[5],
                    event_kind=row[6],
                    received_at=row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7]),
                    headers=[(k, v) for k, v in (row[8] or [])],
                )
        self.conn.commit()

    def resolve(self, provider: str, ingest_key: str) -> ConnectionConfig | None:
        digest = hash_ingest_key(ingest_key)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT c.org_id, c.provider, c.id, c.settings "
                "FROM ingest_keys k JOIN connections c ON c.id = k.connection_id "
                "WHERE k.provider = %s AND k.key_hash = %s AND k.revoked_at IS NULL",
                (provider, digest),
            )
            row = cur.fetchone()
        self.conn.commit()
        if row is None:
            return None
        return ConnectionConfig(
            org_id=row[0],
            provider=row[1],
            connection_id=row[2],
            credentials=self._decrypt_credentials(row[2]),
            settings=row[3] or {},
        )

    def _decrypt_credentials(self, connection_id: str) -> dict[str, str]:
        if not self.master_key:
            return {}
        from obsalt.crypto.envelope import decrypt_secret

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT credentials_encrypted FROM connections WHERE id = %s",
                (connection_id,),
            )
            row = cur.fetchone()
        self.conn.commit()
        if not row:
            return {}
        try:
            return json.loads(decrypt_secret(bytes(row[0]), key=self.master_key))
        except Exception:
            return {}

    def persist_connection(self, cfg: ConnectionConfig, key_hash: str) -> None:
        blob = b"{}"
        if self.master_key:
            blob = encrypt_secret(json.dumps(cfg.credentials), key=self.master_key)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (cfg.org_id, cfg.org_id),
            )
            cur.execute(
                "INSERT INTO connections (id, org_id, provider, name, credentials_encrypted, settings) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (id) DO UPDATE SET settings = EXCLUDED.settings",
                (
                    cfg.connection_id,
                    cfg.org_id,
                    cfg.provider,
                    cfg.provider,
                    blob,
                    json.dumps(cfg.settings),
                ),
            )
            cur.execute(
                "INSERT INTO ingest_keys (id, org_id, provider, connection_id, key_hash) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key_hash) DO NOTHING",
                (key_hash[:32], cfg.org_id, cfg.provider, cfg.connection_id, key_hash),
            )
        self.conn.commit()

    def is_tombstoned(self, hints: dict[str, Any]) -> bool:
        org_id = hints.get("org_id")
        source_call_id = hints.get("source_call_id")
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM tombstones WHERE org_id = %s AND "
                "(source_call_id = %s OR (kind = 'caller' AND caller_token = %s)) LIMIT 1",
                (org_id, source_call_id, hints.get("caller_token")),
            )
            hit = cur.fetchone()
        self.conn.commit()
        return hit is not None

    def accept(
        self,
        envelope: RawEnvelope,
        *,
        delivery_key: str,
        tombstone_hints: dict[str, Any],
    ) -> tuple[str, bool]:
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM tombstones WHERE org_id = %s AND source_call_id = %s LIMIT 1",
                    (tombstone_hints.get("org_id"), tombstone_hints.get("source_call_id")),
                )
                if cur.fetchone():
                    raise Tombstoned()
                cur.execute(
                    "SELECT envelope_id FROM raw_envelopes WHERE org_id = %s AND delivery_key = %s",
                    (envelope.org_id, delivery_key),
                )
                existing = cur.fetchone()
                if existing:
                    return existing[0], False
                cur.execute(
                    "INSERT INTO raw_envelopes "
                    "(envelope_id, org_id, provider, connection_id, object_key, delivery_key, "
                    "state, event_kind, received_at, headers) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                    (
                        envelope.envelope_id,
                        envelope.org_id,
                        envelope.provider,
                        envelope.connection_id,
                        envelope.object_key,
                        delivery_key,
                        EnvelopeState.QUEUED.value,
                        envelope.event_kind.value if envelope.event_kind else None,
                        envelope.received_at,
                        json.dumps(envelope.headers),
                    ),
                )
                cur.execute(
                    "INSERT INTO outbox (envelope_id, org_id, kind) VALUES (%s, %s, %s)",
                    (envelope.envelope_id, envelope.org_id, "decode"),
                )
        self.envelopes[envelope.envelope_id] = envelope
        return envelope.envelope_id, True

    def get(self, envelope_id: str) -> RawEnvelope | None:
        return self.envelopes.get(envelope_id)

    def claim(self, limit: int = 32) -> list[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT envelope_id FROM outbox WHERE done_at IS NULL "
                "ORDER BY id ASC LIMIT %s FOR UPDATE SKIP LOCKED",
                (limit,),
            )
            rows = [row[0] for row in cur.fetchall()]
        self.conn.commit()
        return rows

    def mark_assembled(self, envelope_id: str, revision: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE raw_envelopes SET state = %s, assembled_revision = %s WHERE envelope_id = %s",
                (EnvelopeState.ASSEMBLED.value, revision, envelope_id),
            )
            cur.execute(
                "UPDATE outbox SET done_at = now() WHERE envelope_id = %s AND done_at IS NULL",
                (envelope_id,),
            )
        self.conn.commit()

    def cas_active(self, revision: Any, *, expected: int | None) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT revision FROM active_revisions WHERE org_id = %s AND call_id = %s",
                (revision.org_id, revision.call_id),
            )
            row = cur.fetchone()
            current = row[0] if row else None
            if current != expected:
                self.conn.rollback()
                return False
            cur.execute(
                "INSERT INTO active_revisions "
                "(org_id, call_id, revision, source, source_call_id, agent_id, "
                "started_at, ended_at, status, hangup_reason, timeline_fidelity) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (org_id, call_id) DO UPDATE SET "
                "revision = EXCLUDED.revision, agent_id = EXCLUDED.agent_id, "
                "started_at = EXCLUDED.started_at, ended_at = EXCLUDED.ended_at, "
                "status = EXCLUDED.status, hangup_reason = EXCLUDED.hangup_reason, "
                "timeline_fidelity = EXCLUDED.timeline_fidelity, updated_at = now()",
                (
                    revision.org_id,
                    revision.call_id,
                    revision.revision,
                    revision.identity.source,
                    revision.identity.source_call_id,
                    revision.identity.agent_id,
                    revision.lifecycle.started_at,
                    revision.lifecycle.ended_at,
                    revision.lifecycle.status.value if revision.lifecycle.status else None,
                    revision.hangup.reason.value if revision.hangup else None,
                    revision.lifecycle.timeline_fidelity.value,
                ),
            )
        self.conn.commit()
        return True

    def list_active_pointers(self) -> list[tuple[str, str, int]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT org_id, call_id, revision FROM active_revisions")
            rows = list(cur.fetchall())
        self.conn.commit()
        return [(row[0], row[1], row[2]) for row in rows]

    def add_tombstone(self, **hints: Any) -> None:
        from uuid import uuid4

        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tombstones (id, org_id, kind, source_call_id, caller_token) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    str(uuid4()),
                    hints.get("org_id"),
                    hints.get("kind") or "call",
                    hints.get("source_call_id"),
                    hints.get("caller_token"),
                ),
            )
        self.conn.commit()
