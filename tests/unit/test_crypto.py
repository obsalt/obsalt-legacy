from __future__ import annotations

import time

from obsalt.crypto.primitives import (
    constant_time_eq,
    enforce_window,
    hmac_hex,
    parse_kv_header,
)
from obsalt.domain.enums import VerifyOutcome


def test_hmac_hex_and_constant_time() -> None:
    digest = hmac_hex("secret", b"body")
    assert constant_time_eq(digest, hmac_hex("secret", b"body"))
    assert not constant_time_eq(digest, hmac_hex("other", b"body"))


def test_parse_kv_header_retell_and_elevenlabs_shapes() -> None:
    retell = parse_kv_header("v=1710000000000,d=abc")
    assert retell["v"] == "1710000000000"
    assert retell["d"] == "abc"
    eleven = parse_kv_header("t=1710000000,v0=deadbeef")
    assert eleven["t"] == "1710000000"
    assert eleven["v0"] == "deadbeef"


def test_enforce_window_both_sides() -> None:
    now = time.time()
    assert enforce_window(now, now=now, tolerance_seconds=300) is None
    stale = enforce_window(now - 400, now=now, tolerance_seconds=300)
    assert stale is not None and stale.outcome is VerifyOutcome.STALE
    future = enforce_window(now + 400, now=now, tolerance_seconds=300)
    assert future is not None and future.outcome is VerifyOutcome.STALE
