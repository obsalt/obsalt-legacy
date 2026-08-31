"""Memory and durable adapters implement the same store ports."""

from __future__ import annotations

from obsalt.analysis.cluster import ClickHouseHangupClusterStore, MemoryHangupClusterStore
from obsalt.analysis.contributions import ClickHouseRollupStore, MemoryRollupStore
from obsalt.analysis.runners import MemoryEvalRunnerStore
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.runtime import (
    MemoryDeletionStore,
    MemoryGenerationStore,
    MemoryKeyDirectory,
    MemoryOrgSpend,
    MemoryReviewStore,
    MemoryRubricStore,
    MemoryWebhookStore,
)
from obsalt.search.index import MemorySearchIndex
from obsalt.store.clickhouse import ClickHouseSink
from obsalt.store.objects import S3ObjectStore
from obsalt.store.ports import (
    DeletionStore,
    EvalRunnerStore,
    GenerationStore,
    HangupClusterStore,
    Inbox,
    KeyDirectory,
    ObjectStore,
    OrgSpendStore,
    ReviewStore,
    RevisionPointerStore,
    RevisionSink,
    RollupStore,
    RubricStore,
    SearchIndex,
    WebhookStore,
)
from obsalt.store.postgres import (
    PostgresDeletionStore,
    PostgresEvalRunnerStore,
    PostgresGenerationStore,
    PostgresInbox,
    PostgresKeyDirectory,
    PostgresOrgSpend,
    PostgresPointerStore,
    PostgresReviewStore,
    PostgresRubricStore,
    PostgresSearchDocuments,
    PostgresWebhookStore,
)
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore
from obsalt.worker.process import MemoryRevisionSink


def _port_methods(port: type) -> set[str]:
    return {name for name in port.__dict__ if not name.startswith("_")}


def _implements(adapter: type, port: type) -> None:
    missing = _port_methods(port) - set(dir(adapter))
    assert not missing, f"{adapter.__name__} missing {sorted(missing)} from {port.__name__}"


def test_object_store_port() -> None:
    _implements(MemoryObjectStore, ObjectStore)
    _implements(S3ObjectStore, ObjectStore)


def test_inbox_port() -> None:
    _implements(MemoryInbox, Inbox)
    _implements(PostgresInbox, Inbox)


def test_pointer_and_sink_ports() -> None:
    _implements(MemoryPointerStore, RevisionPointerStore)
    _implements(PostgresPointerStore, RevisionPointerStore)
    _implements(MemoryRevisionSink, RevisionSink)
    _implements(ClickHouseSink, RevisionSink)


def test_catalog_ports() -> None:
    _implements(MemorySearchIndex, SearchIndex)
    _implements(PostgresSearchDocuments, SearchIndex)
    _implements(MemoryRollupStore, RollupStore)
    _implements(ClickHouseRollupStore, RollupStore)
    _implements(MemoryHangupClusterStore, HangupClusterStore)
    _implements(ClickHouseHangupClusterStore, HangupClusterStore)
    _implements(MemoryGenerationStore, GenerationStore)
    _implements(PostgresGenerationStore, GenerationStore)
    _implements(MemoryKeyDirectory, KeyDirectory)
    _implements(PostgresKeyDirectory, KeyDirectory)
    _implements(MemoryRubricStore, RubricStore)
    _implements(PostgresRubricStore, RubricStore)
    _implements(MemoryEvalRunnerStore, EvalRunnerStore)
    _implements(PostgresEvalRunnerStore, EvalRunnerStore)
    _implements(MemoryWebhookStore, WebhookStore)
    _implements(PostgresWebhookStore, WebhookStore)
    _implements(MemoryReviewStore, ReviewStore)
    _implements(PostgresReviewStore, ReviewStore)
    _implements(MemoryDeletionStore, DeletionStore)
    _implements(PostgresDeletionStore, DeletionStore)
    _implements(MemoryOrgSpend, OrgSpendStore)
    _implements(PostgresOrgSpend, OrgSpendStore)
