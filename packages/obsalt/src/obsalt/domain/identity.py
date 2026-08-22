"""Deterministic call and fact identities.

``call_id`` is uuid5(org, source, source_call_id). A content hash identifies
content; it never establishes which snapshot is newer.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any
from uuid import UUID

OBSALT_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def call_id_for(org_id: str, source: str, source_call_id: str) -> str:
    return str(uuid.uuid5(OBSALT_NAMESPACE, f"{org_id}:{source}:{source_call_id}"))


def call_key_for(org_id: str, source: str, source_call_id: str) -> str:
    return f"{org_id}:{source}:{source_call_id}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def fact_id_for(kind: str, *parts: str) -> str:
    """Stable identity for a source fact. Same inputs always yield the same id."""
    payload = "|".join((kind, *parts))
    return sha256_text(payload)


def content_hash(value: Any) -> str:
    if isinstance(value, bytes):
        return sha256_bytes(value)
    if isinstance(value, str):
        return sha256_text(value)
    return sha256_text(canonical_json(value))
