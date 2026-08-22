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
    drain_tier2(state)
    from obsalt.otel.forward_queue import drain_forward_queue
    from obsalt.webhooks.outbound import drain_outbound

    drain_forward_queue(state)
    drain_outbound(state)


def drain_once(state: Any, *, limit: int = 32) -> int:
    processed = process_outbox(state, limit=limit)
    processed += finalize_due_traces(state)
    drain_tier2(state)
    from obsalt.otel.forward_queue import drain_forward_queue
    from obsalt.webhooks.outbound import drain_outbound

    drain_forward_queue(state)
    drain_outbound(state)
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
                from obsalt.metrics import decode_failures_total

                decode_failures_total.labels(plugin=envelope.provider).inc()
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

    hints = TombstoneHints(source_call_id=envelope.source_call_id, event_time=envelope.received_at)
    if inbox.is_tombstoned(envelope.org_id, hints):
        inbox.tombstone(envelope.org_id, hints)
        return None

    if envelope.body is None:
        envelope = envelope.model_copy(update={"body": objects.get(envelope.object_key)})

    from obsalt.ingest.receive import decoded_envelope_body

    if envelope.provider == "otlp" or envelope.event_kind is ObservationalEventKind.OTLP_BATCH:
        return persist_otlp_envelope(state, envelope)

    plugins = {plugin.name: plugin for plugin in state.plugins}
    loaded_plugin = plugins.get(envelope.provider)
    if loaded_plugin is None:
        from obsalt.metrics import decode_failures_total

        decode_failures_total.labels(plugin=envelope.provider).inc()
        inbox.mark_failed(envelope.envelope_id, "plugin not installed")
        return None

    plugin = loaded_plugin.plugin
    decoded = decoded_envelope_body(envelope)
    if decoded is not envelope.body:
        envelope = envelope.model_copy(update={"body": decoded})
    from obsalt.plugin.host import invoke_with_deadline

    deadline = float(
        getattr(getattr(state, "settings", None), "plugin_deadline_seconds", 10.0) or 10.0
    )
    events = invoke_with_deadline(lambda: list(plugin.decode(envelope)), timeout_seconds=deadline)
    source_call_id = envelope.source_call_id or _source_call_id(events) or envelope.envelope_id
    extracted = _tombstone_from_events(events, source_call_id)
    if extracted.event_time is None:
        extracted = extracted.model_copy(update={"event_time": envelope.received_at})
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
    encoding = (envelope.headers or {}).get("content-encoding")
    req = parse_otlp_request(
        ct,
        raw,
        encoding,
        expanded_bytes=getattr(getattr(state, "settings", None), "expanded_body_limit", None),
    )
    spans = request_to_spans(req)
    registry = MapperRegistry(state.plugins)
    from obsalt.plugin.host import invoke_with_deadline

    deadline = float(
        getattr(getattr(state, "settings", None), "plugin_deadline_seconds", 10.0) or 10.0
    )
    events = invoke_with_deadline(lambda: list(registry.decode(spans)), timeout_seconds=deadline)
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

    if not assembler.ready(
        record, now=utcnow(), grace_seconds=grace, max_call_duration_seconds=max_dur
    ):
        state.inbox.mark_assembled(envelope.envelope_id)
        envelope.state = EnvelopeState.ASSEMBLED
        return None

    use_events = record.events
    rooted = record.rooted
    if not rooted:
        use_events = unrooted_events(use_events)
    from obsalt.otel.attributes import leftover_attributes

    leftover = leftover_attributes(spans)
    revision = _assemble_and_promote(
        state,
        envelope,
        use_events,
        source=source,
        source_call_id=source_call_id or record.trace_id,
        declaration=declaration,
        decoder_version=decoder_version,
        rooted=rooted,
        caller_token=getattr(record, "caller_token", None),
        unmapped_attributes=leftover,
    )
    assembler.mark_finalized(record, unrooted=not rooted)
    record.call_id = revision.call_id if revision is not None else None
    extracted = _tombstone_from_events(use_events, source_call_id)
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
    caller_token: str | None = None,
    unmapped_attributes: dict[str, str] | None = None,
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
            caller_token=caller_token,
            unmapped_attributes=unmapped_attributes,
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
    frontier_fn = getattr(state.pointers, "frontier", None)
    stored_frontier = (
        frontier_fn(revision.org_id, revision.call_id) if callable(frontier_fn) else frozenset()
    )
    covers = True
    if stored_frontier:
        covers = stored_frontier <= _revision_fact_ids(revision)
    if (
        revision.conflicts
        or active != revision.revision
        or (active == revision.revision and not covers)
    ):
        from obsalt.metrics import promotion_failures_total

        promotion_failures_total.inc()
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
        _emit_analysis_hooks(state, revision)
        _emit_slo(state, revision)
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
        store = getattr(state, "generation_store", None)
        if store is not None and hasattr(store, "publish"):
            store.publish("fleet", generation, expected=getattr(state, "rollup_generation", None))
        state.rollup_generation = generation
    updater = getattr(state.pointers, "update_summary", None)
    if callable(updater):
        try:
            updater(revision)
        except Exception:
            log.warning("active_calls summary update failed for %s", revision.call_id)
    try:
        _refresh_hangup_clusters(state, revision.org_id)
    except Exception:
        log.warning("hangup cluster refresh failed for %s", revision.org_id)
    try:
        _enqueue_tier2(state, revision)
    except Exception:
        log.warning("tier-2 scheduling failed for %s", revision.call_id)


def _revision_fact_ids(revision: CallRevision) -> set[str]:
    stored = getattr(revision, "accepted_fact_ids", None)
    if stored:
        return {item for item in stored if item}
    ids = {item.fact_id for item in revision.stage_measurements}
    ids.update(item.fact_id for item in revision.aggregate_measurements)
    ids.update(item.id for item in revision.tools)
    return {item for item in ids if item}


def _refresh_hangup_clusters(state: Any, org_id: str) -> None:
    store = getattr(state, "hangup_clusters", None)
    if store is None or not hasattr(store, "refresh"):
        return
    from obsalt.query import active_calls
    from obsalt.search.hybrid import LocalEmbedder

    embedder = getattr(state, "embedder", None) or LocalEmbedder()
    store.refresh(
        org_id,
        active_calls(state, org_id),
        getattr(state, "rollup_generation", ""),
        embedder=embedder,
    )


def _enqueue_tier2(state: Any, revision: CallRevision) -> None:
    """Queue expensive analysis after promotion. Never run it on the webhook path."""
    queue = getattr(state, "tier2_queue", None)
    if queue is None:
        state.tier2_queue = []
        queue = state.tier2_queue
    queue.append((revision.org_id, revision.call_id, revision.revision))


def drain_tier2(state: Any) -> int:
    queue = list(getattr(state, "tier2_queue", None) or [])
    if not queue:
        return 0
    state.tier2_queue.clear()
    processed = 0
    for org_id, call_id, revision_id in queue:
        revision = state.sink.get(org_id, call_id, revision_id)
        if revision is None:
            continue
        try:
            _run_queued_tier2(state, revision)
            processed += 1
        except Exception:
            log.warning("tier-2 execution failed for %s", call_id)
    return processed


def _run_queued_tier2(state: Any, revision: CallRevision) -> None:
    from obsalt.analysis.hallucination import extract_candidate_claims
    from obsalt.analysis.tier2 import decide_tier2
    from obsalt.domain.enums import AnalysisState
    from obsalt.runtime import add_org_spend, org_spend_usd

    settings = getattr(state, "settings", None)
    rate = float(getattr(settings, "baseline_sample_rate", 0.0) or 0.0)
    budget = float(getattr(settings, "llm_monthly_budget_usd", 0.0) or 0.0)
    spend = org_spend_usd(state, revision.org_id)
    budget_usd = budget if budget > 0 else float("inf")
    rubrics = [
        r
        for r in getattr(state, "rubrics", {}).values()
        if getattr(r, "org_id", None) == revision.org_id
    ]
    existing = list(
        getattr(state.sink, "analysis", {}).get(
            (revision.org_id, revision.call_id, revision.revision), []
        )
    )

    claims = []
    for row in existing:
        payload = getattr(row, "payload", {}) or {}
        if payload.get("candidates"):
            claims = payload["candidates"]
            break
    if not claims:
        claims = extract_candidate_claims(revision)
    decisions = []
    if claims:
        decisions.append(
            decide_tier2(
                revision,
                hallucination_candidates=claims,
                baseline_sample_rate=rate,
                budget_usd=budget_usd,
                spend_usd=spend,
                analyzer_id="hallucination",
            )
        )
    for rubric in rubrics:
        decisions.append(
            decide_tier2(
                revision,
                rubric=rubric,
                baseline_sample_rate=rate,
                budget_usd=budget_usd,
                spend_usd=spend,
            )
        )
    replacing = {item.analyzer_id for item in decisions}
    results = [
        row
        for row in existing
        if getattr(getattr(row, "execution", None), "analyzer_id", None) not in replacing
    ]
    writer = getattr(state.sink, "write_analysis", None)
    for execution in decisions:
        if execution.state is AnalysisState.PENDING:
            result = _run_tier2_blocking(
                revision,
                rubric=next(
                    (r for r in rubrics if str(r.version) == str(execution.rubric_version)), None
                ),
                baseline_sample_rate=rate,
                budget_usd=budget_usd,
                spend_usd=spend,
                analyzer_id=execution.analyzer_id,
                hallucination_candidates=claims or None,
                judge=getattr(state, "judge", None),
            )
            cost = float((result.payload or {}).get("cost_usd") or 0.0)
            if cost:
                spend = add_org_spend(state, revision.org_id, cost)
            results.append(result)
            if not result.payload.get("passed", True):
                from obsalt.webhooks.outbound import emit_standard_event

                emit_standard_event(
                    state, revision, "eval.failed", {"analyzer_id": execution.analyzer_id}
                )
        else:
            from obsalt.domain.models import AnalysisResult

            results.append(
                AnalysisResult(execution=execution, payload={"selection": execution.state.value})
            )
    if writer is not None:
        writer(revision.org_id, revision.call_id, revision.revision, results)


def _run_tier2_blocking(revision: CallRevision, **kwargs: Any) -> Any:
    import asyncio
    import threading

    from obsalt.analysis.tier2 import run_tier2

    async def _go() -> Any:
        return await run_tier2(revision, **kwargs)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_go())
    box: dict[str, Any] = {}

    def _thread() -> None:
        box["result"] = asyncio.run(_go())

    worker = threading.Thread(target=_thread)
    worker.start()
    worker.join()
    return box["result"]


def _emit_slo(state: Any, revision: CallRevision) -> None:
    from obsalt.webhooks.outbound import maybe_emit_slo

    maybe_emit_slo(state, revision)


def _emit_analysis_hooks(state: Any, revision: CallRevision) -> None:
    from obsalt.webhooks.outbound import emit_standard_event

    rows = getattr(state.sink, "analysis", {}).get(
        (revision.org_id, revision.call_id, revision.revision), []
    )
    for row in rows:
        payload = getattr(row, "payload", {}) or {}
        analyzer = getattr(getattr(row, "execution", None), "analyzer_id", "")
        if analyzer == "flags":
            for flag in payload.get("flags") or []:
                if isinstance(flag, dict) and flag.get("kind"):
                    emit_standard_event(state, revision, "flag.raised", {"kind": str(flag["kind"])})


def _source_call_id(events: Iterable[NormalizedEvent]) -> str | None:
    for event in events:
        if isinstance(event, CallObserved):
            return event.source_call_id
    return None


def _tombstone_from_events(
    events: Iterable[NormalizedEvent], source_call_id: str | None
) -> TombstoneHints:
    event_time = None
    for event in events:
        if isinstance(event, CallObserved):
            event_time = event.started_at or event.ended_at or event.event_occurred_at
            break
        occurred = getattr(event, "event_occurred_at", None)
        if occurred is not None:
            event_time = occurred
            break
    return TombstoneHints(source_call_id=source_call_id, event_time=event_time)


# Drop-in name for the webhook BackgroundTask once api.py imports this module.
drain_inbox = drain_once
