"""Inbound receive paths. Decode never runs on the webhook or OTLP request."""

from obsalt.ingest.headers import DIAGNOSTIC_HEADER_ALLOWLIST, RawHeaders
from obsalt.ingest.otlp import OtlpReceiveResult, otlp_delivery_key, receive_otlp_batch
from obsalt.ingest.receive import ReceiveResult, object_key_for, receive_webhook

__all__ = [
    "DIAGNOSTIC_HEADER_ALLOWLIST",
    "OtlpReceiveResult",
    "RawHeaders",
    "ReceiveResult",
    "object_key_for",
    "otlp_delivery_key",
    "receive_otlp_batch",
    "receive_webhook",
]
