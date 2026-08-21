"""Deprecated aliases. Use obsalt.tracing.conventions."""

from obsalt.tracing.conventions import *  # noqa: F403
from obsalt.tracing.conventions import (  # noqa: F401
    AGENT_ID as ATTR_AGENT_ID,
    CALL_ID as ATTR_CALL_ID,
    GENAI_OPERATION as ATTR_GENAI_OPERATION,
    GENAI_PROVIDER as ATTR_GENAI_PROVIDER,
    GENAI_REQUEST_MODEL as ATTR_GENAI_REQUEST_MODEL,
    GENAI_TOOL_NAME as ATTR_TOOL_NAME,
    TOOL_RETRY_COUNT as ATTR_TOOL_RETRY,
    TURN_INDEX as ATTR_TURN_INDEX,
    TURN_SPEAKER as ATTR_TURN_SPEAKER,
)
