"""Per-org keyed HMAC caller tokens retained only for privacy operations (§12.3)."""

from __future__ import annotations

import hashlib
import hmac

from obsalt.crypto.primitives import constant_time_eq

# Privacy-only pepper. Distinct from bootstrap secrets so caller-token identity
# does not depend on Settings.
DEFAULT_PEPPER = "obsalt-privacy"


def caller_token(org_id: str, caller: str, master_key: bytes | str) -> str:
    key = master_key.encode("utf-8") if isinstance(master_key, str) else master_key
    material = f"{org_id}\0{caller.strip()}".encode()
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def match_caller(
    org_id: str, caller: str, stored_token: str | None, master_key: bytes | str
) -> bool:
    if not stored_token:
        return False
    return constant_time_eq(caller_token(org_id, caller, master_key), stored_token)
