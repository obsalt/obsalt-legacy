"""obsalt — observability for voice AI agents.

Two ways in:

- ``VoiceCallTracer`` — call this from your agent process to emit OpenTelemetry spans.
- The HTTP server (``obsalt`` CLI) — point Vapi / Retell / Bland webhooks at it, or
  POST a ``CallRecorder`` snapshot to ``/v1/ingest/native``.

Traces go to your OpenTelemetry backend. Transcripts and analysis live in obsalt's
evidence store. There is no obsalt dashboard.
"""

from obsalt.domain.enums import HangupParty, HangupReason, Provider, Speaker, ToolStatus
from obsalt.domain.models import CanonicalCall, Hangup, ToolInvocation, Turn
from obsalt.sdk import CallRecorder
from obsalt.tracing.tracer import VoiceCallTracer

__all__ = [
    "CallRecorder",
    "CanonicalCall",
    "Hangup",
    "HangupParty",
    "HangupReason",
    "Provider",
    "Speaker",
    "ToolInvocation",
    "ToolStatus",
    "Turn",
    "VoiceCallTracer",
]

__version__ = "0.1.0"
