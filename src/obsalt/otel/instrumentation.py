"""Backward-compatible OTel entrypoints. Prefer obsalt.tracing."""

from obsalt.tracing.emitter import emit_call_trace
from obsalt.tracing.setup import setup_tracing

__all__ = ["emit_call_trace", "setup_tracing"]
