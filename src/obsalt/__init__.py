"""obsalt — observability for voice AI agents.

Two ways in:

- ``VoiceCallTracer`` / ``setup_tracing`` — call these from your agent process to
  emit OpenTelemetry spans.
- The HTTP server (``obsalt serve``) — point Vapi / Retell / Bland webhooks at it,
  or POST a ``CallRecorder`` snapshot with ``ObsaltClient``.

Traces go to your OpenTelemetry backend. Transcripts and analysis live in obsalt's
evidence store. There is no obsalt dashboard.
"""

from obsalt._version import __version__
from obsalt.client import ObsaltClient
from obsalt.config import Settings
from obsalt.domain.enums import (
    HangupParty,
    HangupReason,
    Provider,
    Speaker,
    ToolStatus,
    parse_provider,
)
from obsalt.domain.models import CanonicalCall, Hangup, ToolInvocation, Turn
from obsalt.sdk import CallRecorder
from obsalt.tracing.setup import setup_tracing
from obsalt.tracing.tracer import VoiceCallTracer

__all__ = [
    "CallRecorder",
    "CanonicalCall",
    "Hangup",
    "HangupParty",
    "HangupReason",
    "ObsaltClient",
    "Provider",
    "Settings",
    "Speaker",
    "ToolInvocation",
    "ToolStatus",
    "Turn",
    "VoiceCallTracer",
    "parse_provider",
    "setup_tracing",
    "__version__",
]
