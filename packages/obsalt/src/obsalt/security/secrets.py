"""Envelope encryption for recoverable provider/judge/embedder/webhook secrets."""

from __future__ import annotations

import os
from hashlib import sha256

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def hash_key(plaintext: str, *, salt: bytes | None = None) -> str:
    material = (salt or b"obsalt-api-key") + plaintext.encode("utf-8")
    return sha256(material).hexdigest()


def encrypt_secret(plaintext: str, master_key: bytes) -> bytes:
    aes = AESGCM(_key32(master_key))
    nonce = os.urandom(12)
    return nonce + aes.encrypt(nonce, plaintext.encode("utf-8"), None)


def decrypt_secret(blob: bytes, master_key: bytes) -> str:
    aes = AESGCM(_key32(master_key))
    nonce, ct = blob[:12], blob[12:]
    return aes.decrypt(nonce, ct, None).decode("utf-8")


def _key32(master_key: bytes) -> bytes:
    if len(master_key) == 32:
        return master_key
    return sha256(master_key).digest()
