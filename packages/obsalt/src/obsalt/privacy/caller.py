"""Per-org keyed HMAC caller tokens retained only for privacy operations (§12.3)."""

from __future__ import annotations

import hashlib
import hmac

# Privacy-only pepper. Distinct from bootstrap secrets so caller-token identity
# does not depend on Settings.
DEFAULT_PEPPER = "obsalt-privacy"


def caller_token(org_id: str, caller: str, master_key: bytes | str) -> str:
    key = master_key.encode("utf-8") if isinstance(master_key, str) else master_key
    material = f"{org_id}\0{caller.strip()}".encode()
    return hmac.new(key, material, hashlib.sha256).hexdigest()
