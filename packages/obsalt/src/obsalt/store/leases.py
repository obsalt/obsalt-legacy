"""Redis lease accelerator. Postgres outbox remains the source of truth."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from redis import Redis
from redis.exceptions import RedisError

from obsalt.plugin.types import RawEnvelope

log = logging.getLogger("obsalt.leases")

QUEUE_KEY = "obsalt:outbox"
LEASE_PREFIX = "obsalt:lease:"


class LeaseAccelerator(Protocol):
    def notify(self, envelope_id: str) -> None: ...
    def pop_ready(self, limit: int = 32) -> list[str]: ...
    def acquire(self, envelope_id: str, owner: str, ttl: int) -> bool: ...
    def release(self, envelope_id: str) -> None: ...


class RedisLeaseAccelerator:
    def __init__(self, client: Redis) -> None:  # type: ignore[type-arg]
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> RedisLeaseAccelerator:
        return cls(Redis.from_url(url, decode_responses=True))

    def ping(self) -> None:
        self._client.ping()

    def notify(self, envelope_id: str) -> None:
        try:
            self._client.lpush(QUEUE_KEY, envelope_id)
        except RedisError:
            log.warning("redis notify failed; postgres outbox remains authoritative")

    def pop_ready(self, limit: int = 32) -> list[str]:
        try:
            out: list[str] = []
            for _ in range(max(limit, 0)):
                item = self._client.lpop(QUEUE_KEY)
                if item is None:
                    break
                out.append(str(item))
            return out
        except RedisError:
            log.warning("redis pop failed; falling back to postgres claim")
            return []

    def acquire(self, envelope_id: str, owner: str, ttl: int) -> bool:
        try:
            self._client.set(f"{LEASE_PREFIX}{envelope_id}", owner, nx=True, ex=ttl)
            return True
        except RedisError:
            # Redis must never veto work; Postgres already claimed the row.
            return True

    def release(self, envelope_id: str) -> None:
        try:
            self._client.delete(f"{LEASE_PREFIX}{envelope_id}")
        except RedisError:
            return


def claim_work(
    inbox: Any,
    leases: LeaseAccelerator | None,
    limit: int,
    *,
    owner: str = "worker",
    ttl: int = 30,
) -> list[RawEnvelope]:
    """Claim outbox rows from Postgres. Redis is a hint; if it is down, still claim."""
    if leases is not None:
        try:
            leases.pop_ready(limit)
        except Exception:
            log.warning("lease accelerator unavailable; claiming from postgres outbox")
    envelopes: list[RawEnvelope] = inbox.claim_outbox(limit)
    if leases is not None:
        for envelope in envelopes:
            try:
                leases.acquire(envelope.envelope_id, owner, ttl)
            except Exception:
                log.warning("lease acquire failed; continuing with postgres-claimed work")
                break
    return envelopes
