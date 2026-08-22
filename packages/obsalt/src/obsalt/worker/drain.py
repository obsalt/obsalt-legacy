"""Outbox drain: claim, decode, redact, assemble, persist, promote, index.

Call process_after_ack as a FastAPI BackgroundTask AFTER the webhook ack is built.
Do not run this on the webhook request path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from obsalt.domain.enums import EnvelopeState
from obsalt.domain.events import CallObserved, NormalizedEvent
from obsalt.domain.models import CallRevision
from obsalt.plugin.types import RawEnvelope, TombstoneHints
from obsalt.store.leases import claim_work
from obsalt.worker.process import process_normalized_events

log = logging.getLogger("obsalt.worker.drain")


def process_after_ack(state: Any, envelope: RawEnvelope | None = None) -> None:
    """FastAPI BackgroundTask target. Invoke after the provider acknowledgement is built."""
    if envelope is None:
        drain_once(state)
        return
    try:
        persist_envelope(state, envelope)
    except Exception as exc:  # noqa: BLE001 — isolate background work
        log.exception("envelope %s failed", envelope.envelope_id)
        state.inbox.mark_failed(envelope.envelope_id, str(exc))


def drain_once(state: Any, *, limit: int = 32) -> int:
    return process_outbox(state, limit=limit)


def process_outbox(state: Any, limit: int = 32) -> int:
    inbox = state.inbox
    leases = getattr(state, "leases", None)
    owner = str(getattr(state, "worker_id", "worker"))
    claimed = claim_work(inbox, leases, limit, owner=owner)
    processed = 0
    for envelope in claimed:
        try:
            if persist_envelope(state, envelope) is not None:
                processed += 1
        except Exception as exc:  # noqa: BLE001 — isolate one envelope
            log.exception("envelope %s failed", envelope.envelope_id)
            try:
                inbox.mark_failed(envelope.envelope_id, str(exc))
            except Exception:
                log.exception("mark_failed failed for %s", envelope.envelope_id)
    return processed


def persist_envelope(state: Any, envelope: RawEnvelope) -> CallRevision | None:
    inbox = state.inbox
    objects = state.objects
    pointers = state.pointers
    sink = state.sink
    plugins = {plugin.name: plugin for plugin in state.plugins}

    existing = inbox.get_by_id(envelope.envelope_id)
    if existing is not None and existing.state is EnvelopeState.ASSEMBLED:
        return None

    hints = TombstoneHints(source_call_id=envelope.source_call_id)
    if inbox.is_tombstoned(envelope.org_id, hints):
        inbox.tombstone(envelope.org_id, hints)
        return None

    loaded_plugin = plugins.get(envelope.provider)
    if loaded_plugin is None:
        inbox.mark_failed(envelope.envelope_id, "plugin not installed")
        return None

    if envelope.body is None:
        envelope = envelope.model_copy(update={"body": objects.get(envelope.object_key)})

    plugin = loaded_plugin.plugin
    events = list(plugin.decode(envelope))
    source_call_id = envelope.source_call_id or _source_call_id(events) or envelope.envelope_id
    extracted = TombstoneHints(source_call_id=source_call_id)
    if inbox.is_tombstoned(envelope.org_id, extracted):
        inbox.tombstone(envelope.org_id, extracted)
        return None

    decoder_version = getattr(plugin, "decoder_version", loaded_plugin.name + "/1")
    record_run = getattr(inbox, "record_run", None)
    run_id = None
    if callable(record_run):
        run_id = record_run(
            org_id=envelope.org_id,
            envelope_id=envelope.envelope_id,
            decoder_version=decoder_version,
            status="running",
        )

    try:
        revision = process_normalized_events(
            events,
            org_id=envelope.org_id,
            source=envelope.provider,
            source_call_id=source_call_id,
            envelope_id=envelope.envelope_id,
            declaration=loaded_plugin.fidelity,
            pointers=pointers,
            sink=sink,
            decoder_version=decoder_version,
        )
    except Exception:
        if callable(record_run) and run_id:
            record_run(
                org_id=envelope.org_id,
                envelope_id=envelope.envelope_id,
                decoder_version=decoder_version,
                status="failed",
                run_id=run_id,
                error="persist failed",
            )
        raise

    if inbox.is_tombstoned(envelope.org_id, extracted):
        deleter = getattr(sink, "delete_call", None)
        if callable(deleter):
            deleter(revision.org_id, revision.call_id)
        inbox.tombstone(envelope.org_id, extracted)
        return None

    if pointers.get(revision.org_id, revision.call_id) == revision.revision:
        indexer = getattr(state, "search", None)
        if indexer is not None and hasattr(indexer, "upsert_revision"):
            indexer.upsert_revision(revision)
        else:
            index_revision = getattr(inbox, "index_revision", None)
            if callable(index_revision):
                index_revision(revision)

    inbox.mark_assembled(envelope.envelope_id)
    envelope.state = EnvelopeState.ASSEMBLED
    try:
        from obsalt.webhooks.outbound import emit_call_finalized

        emit_call_finalized(state, revision)
    except Exception:
        log.warning("outbound webhook emit failed for %s", envelope.envelope_id)
    leases = getattr(state, "leases", None)
    if leases is not None:
        try:
            leases.release(envelope.envelope_id)
        except Exception:
            log.warning("lease release failed for %s", envelope.envelope_id)
    if callable(record_run) and run_id:
        record_run(
            org_id=envelope.org_id,
            envelope_id=envelope.envelope_id,
            decoder_version=decoder_version,
            status="completed",
            run_id=run_id,
        )
    return revision


def _source_call_id(events: Iterable[NormalizedEvent]) -> str | None:
    for event in events:
        if isinstance(event, CallObserved):
            return event.source_call_id
    return None


# Drop-in name for the webhook BackgroundTask once api.py imports this module.
drain_inbox = drain_once
