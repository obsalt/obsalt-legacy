"""Real-Postgres bootstrap tests.

These exercise the exact connect -> ping -> apply_schema sequence used by
`production_state` (`obsalt serve` / `obsalt worker`). They skip when the
compose stack is not reachable so `make ci` stays green without Docker.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from obsalt.config import Settings
from obsalt.store.postgres import PgConn, apply_schema, connect, ping

pytestmark = pytest.mark.integration


def _pg_dsn() -> str:
    dsn = Settings().postgres_dsn
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}connect_timeout=2"


@pytest.fixture(scope="module")
def pg_conn() -> PgConn:
    try:
        conn: Connection[Any] = Connection.connect(_pg_dsn(), row_factory=dict_row)
    except Exception as exc:  # pragma: no cover - depends on local docker state
        pytest.skip(f"Postgres not reachable; run docker compose up -d ({exc})")
    yield conn
    conn.close()


def _is_idle(conn: PgConn) -> bool:
    return conn.pgconn.transaction_status == TransactionStatus.IDLE


def test_ping_leaves_connection_idle(pg_conn: PgConn) -> None:
    ping(pg_conn)
    assert _is_idle(pg_conn)


def test_production_connect_autocommit_is_visible_to_a_second_session() -> None:
    from obsalt.plugin.types import ConnectionConfig
    from obsalt.store.postgres import PostgresResolver

    writer = None
    reader = None
    try:
        writer = connect(_pg_dsn())
        reader = connect(_pg_dsn())
    except Exception as exc:  # pragma: no cover - depends on local docker state
        pytest.skip(f"Postgres not reachable; run docker compose up -d ({exc})")
    try:
        ping(writer)
        apply_schema(writer)
        assert writer.autocommit
        assert _is_idle(writer)
        resolver = PostgresResolver(writer, master_key=b"test-master-key-not-for-production!!")
        ingest_key = "txn-visibility-key"
        cfg = ConnectionConfig(
            org_id="local",
            provider="vapi",
            connection_id="conn-txn-visibility",
            ingest_key_hash="",
            secrets={"legacy_secret": "s"},
            settings={"auth_mode": "legacy_secret"},
        )
        resolver.add(cfg, ingest_key)
        assert _is_idle(writer)
        seen = PostgresResolver(reader, master_key=b"test-master-key-not-for-production!!").resolve(
            "vapi", ingest_key
        )
        assert seen is not None
        assert seen.connection_id == "conn-txn-visibility"
        writer.execute("DELETE FROM connections WHERE id = %s", ("conn-txn-visibility",))
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()


def test_ping_then_apply_schema_survives(pg_conn: PgConn) -> None:
    # Regression: ping used to leave INTRANS and apply_schema then crashed on
    # `can't change 'autocommit' now`, which surfaced as a bogus "Could not
    # connect to Postgres" error on every serve/worker start.
    ping(pg_conn)
    apply_schema(pg_conn)
    assert _is_idle(pg_conn)


def test_apply_schema_is_idempotent(pg_conn: PgConn) -> None:
    apply_schema(pg_conn)
    apply_schema(pg_conn)
    row = pg_conn.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_name = 'raw_envelopes'"
    ).fetchone()
    assert row is not None and row["n"] == 1


def test_apply_schema_works_on_mid_transaction_connection(pg_conn: PgConn) -> None:
    pg_conn.execute("SELECT 1")  # deliberately leaves the session INTRANS
    apply_schema(pg_conn)


def test_list_summaries_null_filters_do_not_raise_indeterminate_datatype(pg_conn: PgConn) -> None:
    # `%s IS NULL` with a Python None has no Postgres type; seed's GET /v1/calls
    # hits this path (start/end set, agent/source/outcome unset) and 500'd.
    from datetime import UTC, datetime

    from obsalt.store.postgres import PostgresPointerStore

    apply_schema(pg_conn)
    pg_conn.execute(
        "INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        ("local", "local"),
    )
    pg_conn.execute(
        """
        INSERT INTO active_calls (org_id, call_id, revision, agent_id, started_at, source, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (org_id, call_id) DO UPDATE SET started_at = EXCLUDED.started_at
        """,
        (
            "local",
            "seed-list-null-filters",
            "rev-1",
            "support",
            datetime(2026, 8, 28, tzinfo=UTC),
            "vapi",
            "ended",
        ),
    )
    items, _cursor = PostgresPointerStore(pg_conn).list_summaries(
        "local",
        start=datetime(2026, 8, 22, tzinfo=UTC),
        end=datetime(2026, 8, 29, tzinfo=UTC),
    )
    assert any(row["call_id"] == "seed-list-null-filters" for row in items)


def test_search_null_filters_do_not_raise_indeterminate_datatype(pg_conn: PgConn) -> None:
    # Console GET /v1/ui/search always sends start/end and leaves agent/source
    # unset. `%s IS NULL` with Python None has no Postgres type and 500'd.
    from datetime import UTC, datetime

    from obsalt.store.postgres import PostgresSearchDocuments

    apply_schema(pg_conn)
    result = PostgresSearchDocuments(pg_conn).query(
        "local",
        "refund",
        filters={
            "start": datetime(2020, 1, 1, tzinfo=UTC),
            "end": datetime(2030, 1, 1, tzinfo=UTC),
        },
    )
    assert "items" in result


def test_claim_outbox_round_robins_across_orgs(pg_conn: PgConn) -> None:
    # Regression: the claim SQL mixed ROW_NUMBER() with FOR UPDATE, which
    # Postgres rejects (FeatureNotSupported) and crash-looped the worker.
    from obsalt.plugin.types import RawEnvelope, TombstoneHints
    from obsalt.store.postgres import PostgresInbox

    apply_schema(pg_conn)
    inbox = PostgresInbox(pg_conn)
    try:
        for i in range(6):
            org = "acme" if i % 2 == 0 else "beta"
            envelope = RawEnvelope(
                envelope_id=f"e-claim-{i}",
                org_id=org,
                provider="example",
                connection_id="c1",
                object_key=f"org/{org}/raw/example/claim-{i}/sha",
                delivery_key=f"d-claim-{i}",
                content_sha256="x",
                source_call_id=f"claim-{i}",
            )
            inbox.accept(
                envelope,
                tombstone_hints=TombstoneHints(source_call_id=f"claim-{i}"),
            )
        claimed = inbox.claim_outbox(limit=4)
        assert len(claimed) == 4
        orgs = [envelope.org_id for envelope in claimed]
        assert orgs.count("acme") == 2 and orgs.count("beta") == 2
        # Only the two unclaimed rows remain; leased rows are not re-claimed.
        again = inbox.claim_outbox(limit=4)
        assert len(again) == 2
        assert {envelope.org_id for envelope in again} == {"acme", "beta"}
        assert inbox.claim_outbox(limit=4) == []
        pg_conn.rollback()
    finally:
        pg_conn.rollback()
