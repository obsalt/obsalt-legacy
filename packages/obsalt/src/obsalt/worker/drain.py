"""Outbox drain: claim, decode, redact, assemble, persist, promote, index.

Call process_after_ack as a FastAPI BackgroundTask AFTER the webhook ack is built.
Do not run this on the webhook request path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from obsalt.domain.enums import EnvelopeState, ObservationalEventKind
from obsalt.domain.events import CallObserved, NormalizedEvent
from obsalt.domain.models import CallRevision
from obsalt.plugin.types import RawEnvelope, TombstoneHints
from obsalt.store.leases import claim_work
from obsalt.util import utcnow
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
    finalize_due_traces(state)
    from obsalt.otel.forward_queue import drain_forward_queue

    drain_forward_queue(state)


def drain_once(state: Any, *, limit: int = 32) -> int:
    processed = process_outbox(state, limit=limit)
    processed += finalize_due_traces(state)
    from obsalt.otel.forward_queue import drain_forward_queue

    drain_forward_queue(state)
    return processed


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
    existing = inbox.get_by_id(envelope.envelope_id)
    if existing is not None and existing.state is EnvelopeState.ASSEMBLED:
        return None

    hints = TombstoneHints(source_call_id=envelope.source_call_id)
    if inbox.is_tombstoned(envelope.org_id, hints):
        inbox.tombstone(envelope.org_id, hints)
        return None

    if envelope.body is None:
        envelope = envelope.model_copy(update={"body": objects.get(envelope.object_key)})

    if envelope.provider == "otlp" or envelope.event_kind is ObservationalEventKind.OTLP_BATCH:
        return persist_otlp_envelope(state, envelope)

    plugins = {plugin.name: plugin for plugin in state.plugins}
    loaded_plugin = plugins.get(envelope.provider)
    if loaded_plugin is None:
        inbox.mark_failed(envelope.envelope_id, "plugin not installed")
        return None

    plugin = loaded_plugin.plugin
    events = list(plugin.decode(envelope))
    source_call_id = envelope.source_call_id or _source_call_id(events) or envelope.envelope_id
    extracted = TombstoneHints(source_call_id=source_call_id)
    if inbox.is_tombstoned(envelope.org_id, extracted):
        inbox.tombstone(envelope.org_id, extracted)
        return None

    decoder_version = getattr(plugin, "decoder_version", loaded_plugin.name + "/1")
    revision = _assemble_and_promote(
        state,
        envelope,
        events,
        source=envelope.provider,
        source_call_id=source_call_id,
        declaration=loaded_plugin.fidelity,
        decoder_version=decoder_version,
    )
    return _after_promote(state, envelope, revision, extracted)


def persist_otlp_envelope(state: Any, envelope: RawEnvelope) -> CallRevision | None:
    from obsalt.otel.forward_queue import enqueue_raw_batch
    from obsalt.otel.mappers import MapperRegistry
    from obsalt.otel.receiver import parse_otlp_request, request_to_spans
    from obsalt.otel.trace_assembly import MemoryTraceAssembler, unrooted_events

    raw = envelope.body or b""
    content_type = (envelope.headers or {}).get("content-type", "application/json")
    ct = content_type.split(";")[0].strip()
    req = parse_otlp_request(ct, raw, None)
    spans = request_to_spans(req)
    registry = MapperRegistry(state.plugins)
    events = registry.decode(spans)
    mapper = registry.pick(spans[0]) if spans else None
    if mapper is None:
        state.inbox.mark_failed(envelope.envelope_id, "no otlp mapper claimed this batch")
        return None
    declaration = getattr(mapper, "fidelity", None)
    if declaration is None:
        state.inbox.mark_failed(envelope.envelope_id, "mapper has no fidelity declaration")
        return None
    source = getattr(mapper, "name", "otlp")
    decoder_version = getattr(mapper, "decoder_version", f"{source}/1")
    source_call_id = envelope.source_call_id or _source_call_id(events)

    assembler = getattr(state, "traces", None) or MemoryTraceAssembler()
    if getattr(state, "traces", None) is None:
        state.traces = assembler
    record = assembler.ingest(envelope.org_id, spans, list(events), mapper_name=source)
    settings = getattr(state, "settings", None)
    grace = float(getattr(settings, "trace_grace_seconds", 0) or 0)
    max_dur = float(getattr(settings, "max_call_duration_seconds", 4 * 60 * 60) or 4 * 60 * 60)

    enqueue_raw_batch(
        getattr(state, "forward_queue", None),
        org_id=envelope.org_id,
        object_key=envelope.object_key,
        content_type=ct,
        raw=raw,
    )

    if not assembler.ready(record, now=utcnow(), grace_seconds=grace, max_call_duration_seconds=max_dur):
        state.inbox.mark_assembled(envelope.envelope_id)
        envelope.state = EnvelopeState.ASSEMBLED
        return None

    use_events = record.events
    rooted = record.rooted
    if not rooted:
        use_events = unrooted_events(use_events)
    revision = _assemble_and_promote(
        state,
        envelope,
        use_events,
        source=source,
        source_call_id=source_call_id or record.trace_id,
        declaration=declaration,
        decoder_version=decoder_version,
        rooted=rooted,
    )
    assembler.mark_finalized(record, unrooted=not rooted)
    record.call_id = revision.call_id if revision is not None else None
    extracted = TombstoneHints(source_call_id=source_call_id)
    return _after_promote(state, envelope, revision, extracted)


def finalize_due_traces(state: Any) -> int:
    assembler = getattr(state, "traces", None)
    if assembler is None:
        return 0
    settings = getattr(state, "settings", None)
    grace = float(getattr(settings, "trace_grace_seconds", 0) or 0)
    max_dur = float(getattr(settings, "max_call_duration_seconds", 4 * 60 * 60) or 4 * 60 * 60)
    due = assembler.due(now=utcnow(), grace_seconds=grace, max_call_duration_seconds=max_dur)
    finalized = 0
    for record in due:
        from obsalt.otel.mappers import MapperRegistry
        from obsalt.otel.trace_assembly import unrooted_events

        registry = MapperRegistry(state.plugins)
        mapper = None
        wanted = getattr(record, "mapper_name", None)
        if wanted:
            for plugin in state.plugins:
                if plugin.name == wanted:
                    mapper = plugin.plugin
                    break
        if mapper is None:
            mapper = registry.mappers[0] if registry.mappers else None
        if mapper is None:
            continue
        declaration = getattr(mapper, "fidelity", None)
        if declaration is None:
            continue
        use_events = record.events if record.rooted else unrooted_events(record.events)
        from obsalt.plugin.types import RawEnvelope
        from obsalt.util import new_id

        envelope = RawEnvelope(
            envelope_id=new_id(),
            org_id=record.org_id,
            provider=getattr(mapper, "name", "otlp"),
            connection_id="otlp-finalize",
            object_key="",
            delivery_key=f"finalize:{record.trace_id}",
            content_sha256="",
            body=b"",
        )
        revision = process_normalized_events(
            use_events,
            org_id=record.org_id,
            source=getattr(mapper, "name", "otlp"),
            source_call_id=_source_call_id(use_events) or record.trace_id,
            envelope_id=envelope.envelope_id,
            declaration=declaration,
            pointers=state.pointers,
            sink=state.sink,
            decoder_version=getattr(mapper, "decoder_version", "otlp/1"),
            objects=state.objects,
            rooted=record.rooted,
        )
        assembler.mark_finalized(record, unrooted=not record.rooted)
        record.call_id = revision.call_id
        _index_and_rollup(state, revision)
        finalized += 1
    return finalized


def _assemble_and_promote(
    state: Any,
    envelope: RawEnvelope,
    events: list[NormalizedEvent],
    *,
    source: str,
    source_call_id: str,
    declaration: Any,
    decoder_version: str,
    rooted: bool = True,
) -> CallRevision:
    inbox = state.inbox
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
            source=source,
            source_call_id=source_call_id,
            envelope_id=envelope.envelope_id,
            declaration=declaration,
            pointers=state.pointers,
            sink=state.sink,
            decoder_version=decoder_version,
            objects=state.objects,
            rooted=rooted,
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
    if callable(record_run) and run_id:
        record_run(
            org_id=envelope.org_id,
            envelope_id=envelope.envelope_id,
            decoder_version=decoder_version,
            status="completed",
            run_id=run_id,
        )
    return revision


def _after_promote(
    state: Any,
    envelope: RawEnvelope,
    revision: CallRevision | None,
    extracted: TombstoneHints,
) -> CallRevision | None:
    inbox = state.inbox
    if revision is None:
        return None
    if inbox.is_tombstoned(envelope.org_id, extracted):
        deleter = getattr(state.sink, "delete_call", None)
        if callable(deleter):
            deleter(revision.org_id, revision.call_id)
        inbox.tombstone(envelope.org_id, extracted)
        return None

    active = state.pointers.get(revision.org_id, revision.call_id)
    if revision.conflicts or active != revision.revision:
        inbox.mark_failed(
            envelope.envelope_id,
            "fact conflicts block promotion"
            if revision.conflicts
            else "active revision does not cover this envelope's fact frontier",
        )
        return revision

    _index_and_rollup(state, revision)
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
    return revision


def _index_and_rollup(state: Any, revision: CallRevision) -> None:
    if state.pointers.get(revision.org_id, revision.call_id) != revision.revision:
        return
    indexer = getattr(state, "search", None)
    if indexer is not None and hasattr(indexer, "upsert_revision"):
        indexer.upsert_revision(revision)
    elif indexer is not None and hasattr(indexer, "index"):
        indexer.index(revision)
    else:
        index_revision = getattr(state.inbox, "index_revision", None)
        if callable(index_revision):
            index_revision(revision)
    rollups = getattr(state, "rollups", None)
    if rollups is not None and hasattr(rollups, "contribute"):
        generation = rollups.contribute(revision)
        state.rollup_generation = generation


def _source_call_id(events: Iterable[NormalizedEvent]) -> str | None:
    for event in events:
        if isinstance(event, CallObserved):
            return event.source_call_id
    return None


# Drop-in name for the webhook BackgroundTask once api.py imports this module.
drain_inbox = drain_once
