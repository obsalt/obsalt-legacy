"""obsalt — observability for voice AI agents.

Two ways in. Pick one:

- **Hosted platform** (Vapi, Retell, Bland) — run ``obsalt serve`` and point the
  vendor webhook at it. You do not use ``VoiceCall`` / ``VoiceCallTracer``.
- **Your agent** (Pipecat, LiveKit, custom) — wrap the session with ``VoiceCall``.
  That emits OpenTelemetry spans from your process and can POST evidence to the
  server. ``VoiceCallTracer`` is the low-level span API; ``CallRecorder`` is the
  snapshot builder if you do not want in-process OTel.

Traces go to your OpenTelemetry backend (OTLP is a protocol, not an obsalt
server). Transcripts and analysis live in obsalt's evidence store. There is no
obsalt dashboard.
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
from obsalt.domain.models import CanonicalCall, Hangup, NativeSnapshot, ToolInvocation, Turn
from obsalt.sdk import CallRecorder
from obsalt.session import VoiceCall
from obsalt.tracing.setup import setup_tracing
from obsalt.tracing.tracer import VoiceCallTracer

__all__ = [
    "CallRecorder",
    "CanonicalCall",
    "Hangup",
    "HangupParty",
    "HangupReason",
    "NativeSnapshot",
    "ObsaltClient",
    "Provider",
    "Settings",
    "Speaker",
    "ToolInvocation",
    "ToolStatus",
    "Turn",
    "VoiceCall",
    "VoiceCallTracer",
    "parse_provider",
    "setup_tracing",
    "__version__",
]
