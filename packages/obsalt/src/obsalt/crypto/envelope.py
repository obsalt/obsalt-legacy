"""Envelope encryption for recoverable provider/judge/embedder secrets.

Secrets are never returned after creation. Decryption is narrow and audited.
"""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


class EnvelopeError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet(key: str | None = None) -> Fernet:
    raw = key or os.environ.get("OBSALT_MASTER_KEY", "")
    if not raw:
        raise EnvelopeError("OBSALT_MASTER_KEY is required to encrypt secrets")
    if len(raw) == 44:
        return Fernet(raw.encode("utf-8"))
    # allow raw urlsafe base64 generation via Fernet.generate_key()
    try:
        return Fernet(raw.encode("utf-8"))
    except ValueError as exc:
        raise EnvelopeError("OBSALT_MASTER_KEY must be a Fernet key") from exc


def encrypt_secret(plaintext: str, *, key: str | None = None) -> bytes:
    return _fernet(key).encrypt(plaintext.encode("utf-8"))


def decrypt_secret(token: bytes, *, key: str | None = None) -> str:
    try:
        return _fernet(key).decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise EnvelopeError("secret cannot be decrypted") from exc


def generate_master_key() -> str:
    return Fernet.generate_key().decode("ascii")
