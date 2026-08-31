"""Durable stores. Postgres + ClickHouse + object storage; not a pluggable backend.

Ports live in ``obsalt.store.ports``. Memory types implement those ports and are
test doubles, not a second engine.
"""

from obsalt.store.clickhouse import ClickHouseSink, as_clickhouse_datetime
from obsalt.store.leases import RedisLeaseAccelerator, claim_work
from obsalt.store.objects import S3ObjectStore
from obsalt.store.ports import (
    ConnectionResolver,
    Inbox,
    ObjectStore,
    RevisionPointerStore,
    RevisionSink,
)
from obsalt.store.postgres import (
    PostgresAuditLog,
    PostgresDeletionStore,
    PostgresInbox,
    PostgresKeyDirectory,
    PostgresPointerStore,
    PostgresResolver,
    PostgresRubricStore,
    PostgresSearchDocuments,
    apply_schema,
    connect,
    ping,
)

__all__ = [
    "ConnectionResolver",
    "Inbox",
    "ObjectStore",
    "RevisionPointerStore",
    "RevisionSink",
    "ClickHouseSink",
    "as_clickhouse_datetime",
    "PostgresAuditLog",
    "PostgresDeletionStore",
    "PostgresInbox",
    "PostgresKeyDirectory",
    "PostgresPointerStore",
    "PostgresResolver",
    "PostgresRubricStore",
    "PostgresSearchDocuments",
    "RedisLeaseAccelerator",
    "S3ObjectStore",
    "apply_schema",
    "claim_work",
    "connect",
    "ping",
]
