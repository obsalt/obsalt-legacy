"""CAS generation store and write-then-verify promotion protocol."""

from __future__ import annotations

import pytest
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.domain.events import CallObserved
from obsalt.runtime import MemoryGenerationStore
from obsalt.worker.process import MemoryRevisionSink, RevisionSink, process_normalized_events

from tests.helpers import fidelity_declaration


def test_generation_store_cas_is_strict() -> None:
    store = MemoryGenerationStore()
    assert store.get("fleet") == "gen-0"
    assert store.publish("fleet", "gen-1", expected="gen-0") is True
    assert store.publish("fleet", "gen-2", expected="gen-0") is False
    assert store.get("fleet") == "gen-1"


class InvisibleSink(MemoryRevisionSink):
    def write(self, revision) -> None:  # type: ignore[no-untyped-def]
        return

    def verify_visible(self, revision):  # type: ignore[no-untyped-def]
        return RevisionSink.verify_visible(self, revision)


def test_invisible_write_blocks_promotion() -> None:
    pointers = MemoryPointerStore()
    sink = InvisibleSink()
    with pytest.raises(RuntimeError, match="query-visible"):
        process_normalized_events(
            [CallObserved(source_call_id="c1")],
            org_id="acme",
            source="example",
            source_call_id="c1",
            envelope_id="e1",
            declaration=fidelity_declaration(),
            pointers=pointers,
            sink=sink,
            decoder_version="t/1",
        )
    assert pointers.get("acme", "c1") is None
