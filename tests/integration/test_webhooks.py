from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from obsalt.egress import EgressDenied, validate_destination
from obsalt.webhooks.outbound import mint_whsec, parse_whsec, sign


def test_standard_webhooks_sign_headers() -> None:
    secret = parse_whsec(mint_whsec())
    body = b'{"type":"call.finalized","revision":"r1"}'
    headers = sign(secret, body, timestamp=1_700_000_000, msg_id="msg_test")
    assert headers["webhook-id"] == "msg_test"
    assert headers["webhook-timestamp"] == "1700000000"
    assert headers["webhook-signature"].startswith("v1,")
    to_sign = b"msg_test.1700000000." + body
    expected = hmac.new(secret, to_sign, hashlib.sha256).digest()
    assert headers["webhook-signature"] == "v1," + base64.b64encode(expected).decode("ascii")


def test_outbound_destination_https_only() -> None:
    with pytest.raises(EgressDenied):
        validate_destination("http://example.com/hooks")
