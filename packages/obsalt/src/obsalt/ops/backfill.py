"""Provider REST backfill. Identity is (connection, upstream_entity_id, content_hash) (§6.4)."""

from __future__ import annotations

from typing import Any

from obsalt.domain.enums import Capability, EnvelopeState, ObservationalEventKind
from obsalt.ingest.receive import object_key_for
from obsalt.plugin.types import BackfillCursor, ConnectionConfig, RawEnvelope, TombstoneHints
from obsalt.util import sha256_bytes, utcnow
from obsalt.worker.drain import drain_once


def run_backfill(
    state: Any,
    *,
    org_id: str,
    provider: str | None = None,
    connection_id: str | None = None,
    limit_pages: int = 8,
) -> dict[str, Any]:
    queued = 0
    truncated = False
    connections = _connections(state, org_id, provider=provider, connection_id=connection_id)
    for cfg, plugin in connections:
        if Capability.REST_BACKFILL not in getattr(plugin, "capabilities", ()):
            continue
        cursor = BackfillCursor()
        pages = 0
        while pages < limit_pages:
            page = plugin.scan(cfg, cursor)
            truncated = truncated or bool(page.truncated_by_retention)
            for item in page.items:
                envelope = plugin.hydrate(cfg, item)
                hints = TombstoneHints(source_call_id=item.upstream_entity_id)
                if state.inbox.is_tombstoned(org_id, hints):
                    continue
                body = envelope.body or b"{}"
                digest = envelope.content_sha256 or sha256_bytes(body)
                delivery = f"{cfg.connection_id}:{item.upstream_entity_id}:{item.content_hash or digest}"
                key = object_key_for(org_id, cfg.provider, delivery, digest)
                state.objects.put(key, body, content_type="application/json")
                stored = RawEnvelope(
                    envelope_id=envelope.envelope_id,
                    org_id=org_id,
                    provider=cfg.provider,
                    connection_id=cfg.connection_id,
                    object_key=key,
                    delivery_key=delivery,
                    content_sha256=digest,
                    state=EnvelopeState.QUEUED,
                    event_kind=ObservationalEventKind.SNAPSHOT,
                    source_call_id=item.upstream_entity_id,
                    received_at=utcnow(),
                    body=body,
                )
                accepted, _created = state.inbox.accept(stored, tombstone_hints=hints)
                if accepted.state is not EnvelopeState.TOMBSTONED:
                    queued += 1
            pages += 1
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    processed = drain_once(state)
    return {
        "status": "queued",
        "envelopes": queued,
        "processed": processed,
        "truncated_by_retention": truncated,
        "note": "Backfill is bounded by provider retention. Replay re-decodes retained raw data.",
    }


def _connections(
    state: Any,
    org_id: str,
    *,
    provider: str | None,
    connection_id: str | None,
) -> list[tuple[ConnectionConfig, Any]]:
    out: list[tuple[ConnectionConfig, Any]] = []
    plugins = {item.name: item.plugin for item in state.plugins}
    for cfg in getattr(state.resolver, "connections", {}).values():
        if cfg.org_id != org_id:
            continue
        if provider and cfg.provider != provider:
            continue
        if connection_id and cfg.connection_id != connection_id:
            continue
        plugin = plugins.get(cfg.provider)
        if plugin is None:
            continue
        out.append((cfg, plugin))
    return out
