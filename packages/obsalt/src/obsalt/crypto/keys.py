from __future__ import annotations

import hashlib
import secrets

from obsalt.domain.identity import sha256_text


def new_ingest_key() -> str:
    return secrets.token_urlsafe(32)


def new_api_key() -> tuple[str, str, str]:
    """Returns (plaintext, prefix, hash). Plaintext is shown once."""
    token = secrets.token_urlsafe(32)
    prefix = token[:8]
    return token, prefix, hash_secret(token)


def hash_secret(value: str) -> str:
    return sha256_text(value)


def hash_ingest_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
