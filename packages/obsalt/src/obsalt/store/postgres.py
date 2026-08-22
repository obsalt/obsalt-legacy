"""Postgres: inbox/outbox, tenants, pointers, keys, rubrics, search, deletion, audit."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeAlias

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Json

from obsalt.assemble.promote import RevisionPointerStore
from obsalt.domain.enums import EnvelopeState, KeyScope, ObservationalEventKind
from obsalt.domain.models import CallRevision, Rubric
from obsalt.plugin.types import ConnectionConfig, RawEnvelope, TombstoneHints
from obsalt.security.secrets import decrypt_secret, encrypt_secret, hash_key
from obsalt.util import new_id, utcnow

SCHEMA_PATH = Path(__file__).resolve().parent / "sql" / "postgres.sql"

PgConn: TypeAlias = Connection[Any]


def connect(dsn: str) -> PgConn:
    return Connection.connect(dsn, row_factory=dict_row)


def ping(conn: PgConn) -> None:
    conn.execute("SELECT 1")


def apply_schema(conn: PgConn) -> None:
    previous = conn.autocommit
    conn.autocommit = True
    try:
        for statement in _sql_statements(SCHEMA_PATH.read_text()):
            conn.execute(statement)
    finally:
        conn.autocommit = previous


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


def _embed_sync(text: str, dim: int = 256, onnx_path: str | None = None) -> list[float]:
    from obsalt.search.hybrid import OnnxEmbedder

    embedder = OnnxEmbedder(dim=dim, model_path=onnx_path) if onnx_path else OnnxEmbedder(dim=dim)
    return embedder._project(embedder._bag.bag(text))


def _envelope_from_row(row: dict[str, Any]) -> RawEnvelope:
    kind = row.get("event_kind")
    event_kind = None
    if kind:
        try:
            event_kind = ObservationalEventKind(kind)
        except ValueError:
            event_kind = ObservationalEventKind.UNKNOWN_OBSERVATIONAL
    state_raw = row.get("state") or EnvelopeState.QUEUED.value
    try:
        state = EnvelopeState(state_raw)
    except ValueError:
        state = EnvelopeState.QUEUED
    headers = row.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    return RawEnvelope(
        envelope_id=row["envelope_id"],
        org_id=row["org_id"],
        provider=row["provider"],
        connection_id=row["connection_id"],
        object_key=row["object_key"],
        delivery_key=row["delivery_key"],
        content_sha256=row["content_sha256"],
        state=state,
        event_kind=event_kind,
        source_call_id=row.get("source_call_id"),
        headers={str(k): str(v) for k, v in headers.items()},
        received_at=row["received_at"],
        body=None,
    )


class PostgresInbox:
    """Transactional envelope index, delivery-key dedupe, and outbox."""

    def __init__(self, conn: PgConn) -> None:
        self._conn = conn

    def is_tombstoned(self, org_id: str, hints: TombstoneHints) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM tombstones
            WHERE org_id = %s
              AND (
                    (%s IS NOT NULL AND source_call_id = %s)
                 OR (%s IS NOT NULL AND caller_token = %s)
              )
            LIMIT 1
            """,
            (
                org_id,
                hints.source_call_id,
                hints.source_call_id,
                hints.caller_token,
                hints.caller_token,
            ),
        ).fetchone()
        return row is not None

    def tombstone(self, org_id: str, hints: TombstoneHints) -> None:
        with self._conn.transaction():
            self._conn.execute(
                """
                INSERT INTO tombstones (id, org_id, source_call_id, caller_token)
                VALUES (%s, %s, %s, %s)
                """,
                (new_id(), org_id, hints.source_call_id, hints.caller_token),
            )
            self._conn.execute(
                """
                INSERT INTO deletion_requests (id, org_id, source_call_id, caller_token, status)
                VALUES (%s, %s, %s, %s, 'accepted')
                """,
                (new_id(), org_id, hints.source_call_id, hints.caller_token),
            )
            if hints.source_call_id:
                self._conn.execute(
                    """
                    UPDATE raw_envelopes
                    SET state = %s
                    WHERE org_id = %s AND source_call_id = %s
                    """,
                    (EnvelopeState.TOMBSTONED.value, org_id, hints.source_call_id),
                )
                self._conn.execute(
                    """
                    DELETE FROM outbox o
                    USING raw_envelopes e
                    WHERE o.envelope_id = e.envelope_id
                      AND e.org_id = %s AND e.source_call_id = %s
                    """,
                    (org_id, hints.source_call_id),
                )
            self._conn.execute(
                """
                INSERT INTO audit_events (org_id, action, detail)
                VALUES (%s, 'privacy.tombstone', %s)
                """,
                (org_id, Json({"source_call_id": hints.source_call_id})),
            )

    def accept(self, envelope: RawEnvelope, *, tombstone_hints: TombstoneHints) -> tuple[RawEnvelope, bool]:
        with self._conn.transaction():
            locked = self._conn.execute(
                """
                SELECT 1 FROM tombstones
                WHERE org_id = %s
                  AND (
                        (%s IS NOT NULL AND source_call_id = %s)
                     OR (%s IS NOT NULL AND caller_token = %s)
                  )
                FOR SHARE
                """,
                (
                    envelope.org_id,
                    tombstone_hints.source_call_id,
                    tombstone_hints.source_call_id,
                    tombstone_hints.caller_token,
                    tombstone_hints.caller_token,
                ),
            ).fetchone()
            if locked is not None:
                envelope.state = EnvelopeState.TOMBSTONED
                return envelope, False

            inserted = self._conn.execute(
                """
                INSERT INTO raw_envelopes (
                    envelope_id, org_id, provider, connection_id, object_key,
                    delivery_key, content_sha256, state, event_kind, source_call_id,
                    headers, received_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (org_id, delivery_key) DO NOTHING
                RETURNING *
                """,
                (
                    envelope.envelope_id,
                    envelope.org_id,
                    envelope.provider,
                    envelope.connection_id,
                    envelope.object_key,
                    envelope.delivery_key,
                    envelope.content_sha256,
                    envelope.state.value,
                    envelope.event_kind.value if envelope.event_kind else None,
                    envelope.source_call_id,
                    Json(envelope.headers),
                    envelope.received_at,
                ),
            ).fetchone()
            if inserted is not None:
                self._conn.execute(
                    """
                    INSERT INTO outbox (envelope_id, org_id)
                    VALUES (%s, %s)
                    ON CONFLICT (envelope_id) DO UPDATE
                    SET available_at = now(), leased_until = NULL, lease_owner = NULL
                    """,
                    (envelope.envelope_id, envelope.org_id),
                )
                stored = _envelope_from_row(inserted)
                stored.body = envelope.body
                return stored, True

            existing = self._conn.execute(
                """
                SELECT * FROM raw_envelopes
                WHERE org_id = %s AND delivery_key = %s
                FOR UPDATE
                """,
                (envelope.org_id, envelope.delivery_key),
            ).fetchone()
            if existing is None:
                raise RuntimeError("delivery_key conflict without a stored envelope")
            stored = _envelope_from_row(existing)
            stored.body = envelope.body
            if stored.state is EnvelopeState.TOMBSTONED:
                return stored, False
            if stored.state is not EnvelopeState.ASSEMBLED:
                if stored.object_key != envelope.object_key:
                    self._conn.execute(
                        """
                        UPDATE raw_envelopes
                        SET object_key = %s, content_sha256 = %s
                        WHERE envelope_id = %s
                        """,
                        (envelope.object_key, envelope.content_sha256, stored.envelope_id),
                    )
                    stored.object_key = envelope.object_key
                    stored.content_sha256 = envelope.content_sha256
                self._conn.execute(
                    """
                    INSERT INTO outbox (envelope_id, org_id)
                    VALUES (%s, %s)
                    ON CONFLICT (envelope_id) DO UPDATE
                    SET available_at = now(), leased_until = NULL, lease_owner = NULL
                    """,
                    (stored.envelope_id, stored.org_id),
                )
            return stored, False

    def claim_outbox(self, limit: int = 32) -> list[RawEnvelope]:
        with self._conn.transaction():
            rows = self._conn.execute(
                """
                UPDATE outbox
                SET leased_until = now() + interval '30 seconds',
                    lease_owner = COALESCE(lease_owner, 'worker'),
                    attempts = attempts + 1
                WHERE id IN (
                    SELECT o.id
                    FROM outbox o
                    JOIN raw_envelopes e ON e.envelope_id = o.envelope_id
                    WHERE o.available_at <= now()
                      AND (o.leased_until IS NULL OR o.leased_until < now())
                      AND e.state NOT IN ('assembled', 'tombstoned')
                      AND o.attempts < 16
                    ORDER BY o.id
                    FOR UPDATE OF o SKIP LOCKED
                    LIMIT %s
                )
                RETURNING envelope_id
                """,
                (limit,),
            ).fetchall()
            ids = [row["envelope_id"] for row in rows]
            if not ids:
                return []
            envelopes = self._conn.execute(
                "SELECT * FROM raw_envelopes WHERE envelope_id = ANY(%s)",
                (ids,),
            ).fetchall()
            by_id = {row["envelope_id"]: _envelope_from_row(row) for row in envelopes}
            return [by_id[eid] for eid in ids if eid in by_id]

    def mark_assembled(self, envelope_id: str) -> None:
        with self._conn.transaction():
            self._conn.execute(
                "UPDATE raw_envelopes SET state = %s WHERE envelope_id = %s",
                (EnvelopeState.ASSEMBLED.value, envelope_id),
            )
            self._conn.execute("DELETE FROM outbox WHERE envelope_id = %s", (envelope_id,))

    def mark_failed(self, envelope_id: str, error: str) -> None:
        with self._conn.transaction():
            self._conn.execute(
                """
                UPDATE raw_envelopes
                SET state = %s
                WHERE envelope_id = %s AND state <> %s
                """,
                (EnvelopeState.FAILED.value, envelope_id, EnvelopeState.ASSEMBLED.value),
            )
            updated = self._conn.execute(
                """
                UPDATE outbox
                SET leased_until = NULL,
                    lease_owner = NULL,
                    attempts = attempts + 1,
                    available_at = now() + interval '5 seconds' * LEAST(attempts + 1, 10),
                    last_error = %s
                WHERE envelope_id = %s
                RETURNING envelope_id, attempts
                """,
                (error, envelope_id),
            ).fetchone()
            attempts = int(updated["attempts"]) if updated else 0
            if attempts >= 8:
                self._conn.execute("DELETE FROM outbox WHERE envelope_id = %s", (envelope_id,))
                self._conn.execute(
                    """
                    INSERT INTO decode_dlq (envelope_id, org_id, error)
                    SELECT envelope_id, org_id, %s FROM raw_envelopes WHERE envelope_id = %s
                    ON CONFLICT (envelope_id) DO UPDATE SET error = EXCLUDED.error
                    """,
                    (error, envelope_id),
                )
                from obsalt.metrics import dlq_inserts_total

                dlq_inserts_total.inc()
                return
            if updated is None:
                self._conn.execute(
                    """
                    INSERT INTO outbox (envelope_id, org_id, last_error)
                    SELECT envelope_id, org_id, %s FROM raw_envelopes WHERE envelope_id = %s
                    ON CONFLICT (envelope_id) DO UPDATE SET last_error = EXCLUDED.last_error
                    """,
                    (error, envelope_id),
                )

    def drop_outbox(self, envelope_id: str) -> None:
        with self._conn.transaction():
            self._conn.execute("DELETE FROM outbox WHERE envelope_id = %s", (envelope_id,))

    def get_by_id(self, envelope_id: str) -> RawEnvelope | None:
        row = self._conn.execute(
            "SELECT * FROM raw_envelopes WHERE envelope_id = %s",
            (envelope_id,),
        ).fetchone()
        return _envelope_from_row(row) if row else None

    def list_envelopes(self, org_id: str) -> list[RawEnvelope]:
        rows = self._conn.execute(
            "SELECT * FROM raw_envelopes WHERE org_id = %s ORDER BY received_at",
            (org_id,),
        ).fetchall()
        return [_envelope_from_row(row) for row in rows]

    def requeue(self, envelope_id: str) -> None:
        with self._conn.transaction():
            row = self._conn.execute(
                "SELECT state, org_id FROM raw_envelopes WHERE envelope_id = %s FOR UPDATE",
                (envelope_id,),
            ).fetchone()
            if row is None or row["state"] == EnvelopeState.TOMBSTONED.value:
                return
            self._conn.execute(
                "UPDATE raw_envelopes SET state = %s WHERE envelope_id = %s",
                (EnvelopeState.QUEUED.value, envelope_id),
            )
            self._conn.execute(
                """
                INSERT INTO outbox (envelope_id, org_id)
                VALUES (%s, %s)
                ON CONFLICT (envelope_id) DO UPDATE
                SET available_at = now(), leased_until = NULL, lease_owner = NULL
                """,
                (envelope_id, row["org_id"]),
            )

    def index_revision(self, revision: CallRevision, *, index_version: str = "1") -> None:
        PostgresSearchDocuments(self._conn).upsert_revision(revision, index_version=index_version)

    def record_run(
        self,
        *,
        org_id: str,
        envelope_id: str | None,
        decoder_version: str,
        status: str,
        error: str | None = None,
        run_id: str | None = None,
    ) -> str:
        rid = run_id or new_id()
        finished = utcnow() if status in {"completed", "failed"} else None
        with self._conn.transaction():
            self._conn.execute(
                """
                INSERT INTO processing_runs (
                    id, org_id, envelope_id, decoder_version, status, error, finished_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    error = EXCLUDED.error,
                    finished_at = EXCLUDED.finished_at
                """,
                (rid, org_id, envelope_id, decoder_version, status, error, finished),
            )
        return rid


class PostgresResolver:
    """Hashed ingest keys. Secrets are decrypted with the master key just-in-time."""

    def __init__(self, conn: PgConn, *, master_key: bytes) -> None:
        self._conn = conn
        self._master_key = master_key

    def add(self, cfg: ConnectionConfig, ingest_key: str) -> None:
        digest = hash_key(ingest_key)
        cfg.ingest_key_hash = digest
        ciphertext = encrypt_secret(json.dumps(cfg.secrets), self._master_key)
        with self._conn.transaction():
            self._conn.execute(
                "INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (cfg.org_id, cfg.org_id),
            )
            self._conn.execute(
                """
                INSERT INTO connections (id, org_id, provider, ingest_key_hash, secrets_ciphertext, settings)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    ingest_key_hash = EXCLUDED.ingest_key_hash,
                    secrets_ciphertext = EXCLUDED.secrets_ciphertext,
                    settings = EXCLUDED.settings
                """,
                (
                    cfg.connection_id,
                    cfg.org_id,
                    cfg.provider,
                    digest,
                    ciphertext,
                    Json(cfg.settings),
                ),
            )

    def resolve(self, provider: str, ingest_key: str) -> ConnectionConfig | None:
        digest = hash_key(ingest_key)
        row = self._conn.execute(
            """
            SELECT id, org_id, provider, ingest_key_hash, secrets_ciphertext, settings
            FROM connections
            WHERE provider = %s AND ingest_key_hash = %s
            """,
            (provider, digest),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_cfg(row)

    def _row_to_cfg(self, row: dict[str, Any]) -> ConnectionConfig:
        plaintext = decrypt_secret(bytes(row["secrets_ciphertext"]), self._master_key)
        loaded = json.loads(plaintext)
        secrets = {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}
        settings = row.get("settings") or {}
        if not isinstance(settings, dict):
            settings = {}
        return ConnectionConfig(
            org_id=row["org_id"],
            provider=row["provider"],
            connection_id=row["id"],
            ingest_key_hash=row["ingest_key_hash"],
            secrets=secrets,
            settings=settings,
        )

    @property
    def connections(self) -> dict[tuple[str, str], ConnectionConfig]:
        rows = self._conn.execute(
            "SELECT id, org_id, provider, ingest_key_hash, secrets_ciphertext, settings FROM connections"
        ).fetchall()
        return {(row["provider"], row["ingest_key_hash"]): self._row_to_cfg(row) for row in rows}

    def delete(self, org_id: str, connection_id: str) -> bool:
        with self._conn.transaction():
            row = self._conn.execute(
                "DELETE FROM connections WHERE org_id = %s AND id = %s RETURNING id",
                (org_id, connection_id),
            ).fetchone()
        return row is not None


class PostgresPointerStore(RevisionPointerStore):
    def __init__(self, conn: PgConn) -> None:
        self._conn = conn

    def compare_and_swap(
        self,
        org_id: str,
        call_id: str,
        expected: str | None,
        candidate: str,
        *,
        fact_frontier: frozenset[str],
    ) -> bool:
        frontier = list(fact_frontier)
        with self._conn.transaction():
            row = self._conn.execute(
                """
                SELECT revision FROM active_calls
                WHERE org_id = %s AND call_id = %s
                FOR UPDATE
                """,
                (org_id, call_id),
            ).fetchone()
            current = row["revision"] if row else None
            if current != expected:
                return False
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO active_calls (org_id, call_id, revision, fact_frontier)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (org_id, call_id, candidate, frontier),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE active_calls
                    SET revision = %s, fact_frontier = %s, updated_at = now()
                    WHERE org_id = %s AND call_id = %s
                    """,
                    (candidate, frontier, org_id, call_id),
                )
            return True

    def get(self, org_id: str, call_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT revision FROM active_calls WHERE org_id = %s AND call_id = %s",
            (org_id, call_id),
        ).fetchone()
        return row["revision"] if row else None

    def frontier(self, org_id: str, call_id: str) -> frozenset[str]:
        row = self._conn.execute(
            "SELECT fact_frontier FROM active_calls WHERE org_id = %s AND call_id = %s",
            (org_id, call_id),
        ).fetchone()
        if row is None:
            return frozenset()
        values = row["fact_frontier"] or []
        return frozenset(str(item) for item in values)

    def list_org(self, org_id: str) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            """
            SELECT call_id, revision FROM active_calls
            WHERE org_id = %s
            ORDER BY updated_at DESC
            """,
            (org_id,),
        ).fetchall()
        return [(row["call_id"], row["revision"]) for row in rows]

    def delete(self, org_id: str, call_id: str) -> None:
        self._conn.execute(
            "DELETE FROM active_calls WHERE org_id = %s AND call_id = %s",
            (org_id, call_id),
        )


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    org_id: str
    scopes: frozenset[KeyScope]


class PostgresKeyDirectory:
    """Hashed API keys. Missing, revoked, and expired keys fail closed."""

    def __init__(self, conn: PgConn) -> None:
        self._conn = conn

    def lookup(self, plaintext: str) -> ApiKeyRecord | None:
        digest = hash_key(plaintext)
        row = self._conn.execute(
            """
            SELECT id, org_id, scopes, expires_at, revoked_at
            FROM api_keys
            WHERE key_hash = %s
            """,
            (digest,),
        ).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None:
            return None
        expires_at = row["expires_at"]
        if expires_at is not None and expires_at < utcnow():
            return None
        scopes = frozenset(KeyScope(str(item)) for item in (row["scopes"] or []))
        return ApiKeyRecord(key_id=row["id"], org_id=row["org_id"], scopes=scopes)

    def insert(
        self,
        org_id: str,
        plaintext: str,
        scopes: Iterable[KeyScope],
        *,
        expires_at: datetime | None = None,
    ) -> str:
        key_id = new_id()
        with self._conn.transaction():
            self._conn.execute(
                """
                INSERT INTO api_keys (id, org_id, key_hash, scopes, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (key_id, org_id, hash_key(plaintext), [s.value for s in scopes], expires_at),
            )
        return key_id


class PostgresRubricStore:
    def __init__(self, conn: PgConn) -> None:
        self._conn = conn

    def list(self, org_id: str) -> list[Rubric]:
        rows = self._conn.execute(
            "SELECT * FROM rubrics WHERE org_id = %s ORDER BY created_at",
            (org_id,),
        ).fetchall()
        return [self._rubric(row) for row in rows]

    def get(self, org_id: str, rubric_id: str) -> Rubric | None:
        row = self._conn.execute(
            "SELECT * FROM rubrics WHERE org_id = %s AND id = %s",
            (org_id, rubric_id),
        ).fetchone()
        return self._rubric(row) if row else None

    def insert(self, rubric: Rubric) -> Rubric:
        with self._conn.transaction():
            self._conn.execute(
                """
                INSERT INTO rubrics (id, org_id, name, description, version, threshold, enabled, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    rubric.id,
                    rubric.org_id,
                    rubric.name,
                    rubric.description,
                    rubric.version,
                    rubric.threshold,
                    rubric.enabled,
                    rubric.created_at,
                ),
            )
        return rubric

    def new_version(self, previous: Rubric, *, name: str | None = None, description: str | None = None) -> Rubric:
        updated = previous.model_copy(
            update={
                "id": new_id(),
                "version": previous.version + 1,
                "name": name if name is not None else previous.name,
                "description": description if description is not None else previous.description,
                "created_at": utcnow(),
            }
        )
        return self.insert(updated)

    def delete(self, org_id: str, rubric_id: str) -> bool:
        with self._conn.transaction():
            row = self._conn.execute(
                "DELETE FROM rubrics WHERE org_id = %s AND id = %s RETURNING id",
                (org_id, rubric_id),
            ).fetchone()
        return row is not None

    def _rubric(self, row: dict[str, Any]) -> Rubric:
        return Rubric(
            id=row["id"],
            org_id=row["org_id"],
            name=row["name"],
            description=row["description"],
            version=int(row["version"]),
            threshold=float(row["threshold"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
        )


class PostgresAuditLog:
    def __init__(self, conn: PgConn) -> None:
        self._conn = conn

    def record(
        self,
        org_id: str,
        action: str,
        *,
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO audit_events (org_id, actor, action, detail)
            VALUES (%s, %s, %s, %s)
            """,
            (org_id, actor, action, Json(detail or {})),
        )


class PostgresSearchDocuments:
    def __init__(self, conn: PgConn, *, onnx_path: str | None = None) -> None:
        self._conn = conn
        self._onnx_path = onnx_path

    def upsert_revision(self, revision: CallRevision, *, index_version: str = "1") -> None:
        body = " ".join(turn.text for turn in revision.turns)
        embedding = _embed_sync(body, onnx_path=self._onnx_path)
        literal = "[" + ",".join(str(v) for v in embedding) + "]"
        hangup = revision.hangup.reason.value if revision.hangup else None
        with self._conn.transaction():
            self._conn.execute(
                """
                INSERT INTO search_documents (
                    org_id, call_id, revision, body, tsv, embedding, index_version,
                    agent_id, started_at, source, hangup_reason
                )
                VALUES (
                    %s, %s, %s, %s, to_tsvector('simple', %s), %s::vector, %s, %s, %s, %s, %s
                )
                ON CONFLICT (org_id, call_id) DO UPDATE SET
                    revision = EXCLUDED.revision,
                    body = EXCLUDED.body,
                    tsv = EXCLUDED.tsv,
                    embedding = EXCLUDED.embedding,
                    index_version = EXCLUDED.index_version,
                    agent_id = EXCLUDED.agent_id,
                    started_at = EXCLUDED.started_at,
                    source = EXCLUDED.source,
                    hangup_reason = EXCLUDED.hangup_reason
                """,
                (
                    revision.org_id,
                    revision.call_id,
                    revision.revision,
                    body,
                    body,
                    literal,
                    index_version,
                    revision.agent_id,
                    revision.started_at,
                    revision.source,
                    hangup,
                ),
            )

    def delete_for_call(self, org_id: str, call_id: str) -> None:
        self._conn.execute(
            "DELETE FROM search_documents WHERE org_id = %s AND call_id = %s",
            (org_id, call_id),
        )

    def query(
        self,
        org_id: str,
        q: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from obsalt.search.hybrid import rrf

        filters = filters or {}
        agent_id = filters.get("agent_id")
        source = filters.get("source")
        hangup_reason = filters.get("hangup_reason")
        qvec = _embed_sync(q or " ", onnx_path=self._onnx_path)
        literal = "[" + ",".join(str(v) for v in qvec) + "]"
        rows = self._conn.execute(
            """
            SELECT call_id, revision, body, agent_id,
                   COALESCE(ts_rank(tsv, plainto_tsquery('simple', %s)), 0) AS lex,
                   (embedding <=> %s::vector) AS dist
            FROM search_documents
            WHERE org_id = %s
              AND (%s IS NULL OR agent_id = %s)
              AND (%s IS NULL OR source = %s)
              AND (%s IS NULL OR hangup_reason = %s)
            """,
            (q or "", literal, org_id, agent_id, agent_id, source, source, hangup_reason, hangup_reason),
        ).fetchall()
        lexical_ids = [
            row["call_id"]
            for row in sorted(rows, key=lambda item: (-float(item["lex"]), item["call_id"]))
            if float(row["lex"]) > 0
        ]
        vector_ids = [row["call_id"] for row in sorted(rows, key=lambda item: (float(item["dist"]), item["call_id"]))]
        fused = rrf(vector_ids, lexical_ids) if q.strip() else [row["call_id"] for row in rows]
        by_id = {row["call_id"]: row for row in rows}
        items = []
        for call_id in fused[:limit]:
            row = by_id.get(call_id)
            if row is None:
                continue
            items.append(
                {
                    "call_id": row["call_id"],
                    "revision": row["revision"],
                    "agent_id": row.get("agent_id") or "",
                    "score": float(row["lex"]),
                }
            )
        return {"items": items, "lexical_ids": lexical_ids, "vector_ids": vector_ids, "fused_ids": fused}


class PostgresDeletionStore:
    def __init__(self, conn: PgConn, inbox: PostgresInbox) -> None:
        self._conn = conn
        self._inbox = inbox
        self._search = PostgresSearchDocuments(conn)

    def request_delete_by_call(
        self,
        org_id: str,
        *,
        source_call_id: str,
        call_id: str | None = None,
    ) -> str:
        self._inbox.tombstone(org_id, TombstoneHints(source_call_id=source_call_id))
        if call_id:
            self._search.delete_for_call(org_id, call_id)
        row = self._conn.execute(
            """
            SELECT id FROM deletion_requests
            WHERE org_id = %s AND source_call_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (org_id, source_call_id),
        ).fetchone()
        return str(row["id"]) if row else ""


# Names used by runtime.production_state and the plugin contract.
PostgresConnectionResolver = PostgresResolver
KeyDirectory = PostgresKeyDirectory
RubricStore = PostgresRubricStore
