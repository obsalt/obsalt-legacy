"""Voice-specific OpenTelemetry semantic conventions.

OTel GenAI conventions cover chat/embeddings/tools. Voice agents also need
STT, TTS, turn-taking, hangup, and time-to-first-audio. We emit official
`gen_ai.*` attributes where they apply and `voice.*` for the rest.
"""

from __future__ import annotations

ATTR_PROVIDER = "voice.provider"
ATTR_CALL_ID = "voice.call.id"
ATTR_PROVIDER_CALL_ID = "voice.provider.call_id"
ATTR_AGENT_ID = "voice.agent.id"
ATTR_AGENT_NAME = "voice.agent.name"
ATTR_DIRECTION = "voice.direction"
ATTR_HANGUP_REASON = "voice.hangup.reason"
ATTR_HANGUP_PARTY = "voice.hangup.party"
ATTR_HANGUP_PROVIDER_REASON = "voice.hangup.provider_reason"
ATTR_LOSS_SCORE = "voice.hangup.loss_score"
ATTR_TURN_INDEX = "voice.turn.index"
ATTR_TURN_SPEAKER = "voice.turn.speaker"
ATTR_COMPONENT = "voice.component"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_STATUS = "voice.tool.status"
ATTR_TOOL_RETRY = "voice.tool.retry_count"
ATTR_HALLUCINATION_KIND = "voice.hallucination.kind"
ATTR_EVAL_RUBRIC = "voice.eval.rubric"
ATTR_EVAL_SCORE = "voice.eval.score"
ATTR_GENAI_OPERATION = "gen_ai.operation.name"
ATTR_GENAI_PROVIDER = "gen_ai.provider.name"
ATTR_GENAI_REQUEST_MODEL = "gen_ai.request.model"

SPAN_CALL = "voice.call"
SPAN_TURN = "voice.turn"
SPAN_STT = "voice.stt"
SPAN_LLM = "voice.llm"
SPAN_TTS = "voice.tts"
SPAN_TOOL_PREFIX = "execute_tool"

METRIC_STT = "voice.latency.stt"
METRIC_LLM = "voice.latency.llm"
METRIC_TTS = "voice.latency.tts"
METRIC_E2E = "voice.latency.e2e"
METRIC_TTFA = "voice.latency.ttfa"
METRIC_CALL_DURATION = "voice.call.duration"
METRIC_HANGUP = "voice.hangup.count"
METRIC_TOOL_DURATION = "voice.tool.duration"
METRIC_TOOL_COUNT = "voice.tool.invocations"
METRIC_HALLUCINATION = "voice.hallucination.count"
METRIC_EVAL = "voice.eval.score"

# Voice SLA buckets in seconds (OTel histograms are seconds).
LATENCY_BUCKETS = (0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0)
DURATION_BUCKETS = (1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)
