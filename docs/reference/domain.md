# Domain model

**Separate measurement from timeline.** Hosted providers often ship
durations without timestamps. A duration is still a fact. A waterfall
bar is not.

## Placement and fidelity

```python
class TimelineFidelity(StrEnum):
    STAGE_LEVEL   = "stage_level"    # at least one real stage interval
    TURN_LEVEL    = "turn_level"     # real turn boundaries, stage values unplaced
    MESSAGE_LEVEL = "message_level"  # coarse message anchors
    CALL_LEVEL    = "call_level"     # aggregates only
    NONE          = "none"

class MeasurementPlacement(StrEnum):
    INTERVAL          = "interval"           # real start and end
    ANCHORED_DURATION = "anchored_duration"  # real anchor plus measured duration
    UNPLACED          = "unplaced"           # duration only
    COARSE_ANCHOR     = "coarse_anchor"      # low-resolution timestamp

class PipelineArchitecture(StrEnum):
    CASCADE           = "cascade"            # discrete STT -> LLM -> TTS
    SPEECH_TO_SPEECH  = "speech_to_speech"   # no stage decomposition exists
    HYBRID            = "hybrid"

class Provenance(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    OBSALT_DERIVED    = "obsalt_derived"
```

A `StageMeasurement` carries `stage`, `metric`, `value_ms`, optional
`turn_index`, `placement`, optional `started_at` / `ended_at`,
`provenance`, `source_path`, and `derivation`.

An `AggregateMeasurement` carries a `statistic` (mean, p50, p95, …) and
never enters sample rollups.

`SignalCoverage` is a separate fact: `present`, `absent`, `redacted`,
`decode_failed`, or `unsupported`, with a reason.

## Call aggregate

```
Call
├── identity      org, call_id (uuid5), source, source_call_id, agent_id, agent_version
├── telephony     direction, from/to (redacted), sip/provider ids
├── lifecycle     started_at, ended_at, duration_ms, status, pipeline_architecture,
│                 timeline_fidelity, cost
├── outcome       Hangup { reason, party, provider_code, provenance }
├── Turn[]        index, speaker, text_ref, started_at?, ended_at?, interrupted?, confidence?
├── StageMeasurement[]
├── AggregateMeasurement[]
├── ToolInvocation[]
├── Grounding     system_prompt_ref, knowledge_refs[], tool_result_refs[], user_text_ref
├── Evidence      transcript_ref, recording_ref
├── SignalCoverage[]
└── Provenance    map<field_path, Provenance + source_path>
```

Notes:

- **`*_ref` not inline text.** Transcripts, prompts, tool payloads, and
  recordings are content-addressed references into the evidence store.
  Keeps the hot tables narrow and gives redaction and retention a
  single object to act on.
- **Analysis is associated, not embedded.** `AnalysisExecution` and
  `AnalysisResult` rows are keyed by call revision, analyzer/rubric
  version, and prompt/model version. The call revision remains
  immutable while users can see that it failed one rubric version and
  passed a later one.

`call_id` is `uuid5(OBSALT_NAMESPACE, f"{org_id}:{source}:{source_call_id}")`.

## Normalized events

Decoders emit small facts. This is what removes in-place `merge_calls`.

```
CallObserved
TurnObserved
StageObserved
AggregateObserved
ToolObserved
OutcomeObserved
GroundingObserved
EvidenceObserved
InterruptionObserved
SnapshotBoundaryObserved
FactRetracted
CallFinalized
```

Core stamps every event with `(org_id, call_key, fact_id, envelope_id,
decoder_version, processing_run_id, event_occurred_at, envelope_sequence)`.

Merging independent facts is associative, commutative, and idempotent;
any delivery order produces the same candidate revision. Conflicts
block automatic promotion until the plugin's documented resolution
policy or an operator resolves them.

Property tests on the assembler assert that any permutation and
duplicate delivery of an event stream folds to the same candidate
revision.

## Hangup taxonomy

Provider-agnostic hangup reasons. Keep this list stable:
`user_hangup`, `agent_hangup`, `transfer`, `voicemail`, `inactivity`,
`silence_timeout`, `max_duration`, `busy`, `no_answer`, `dial_failed`,
`error_stt`, `error_llm`, `error_tts`, `error_tool`, `error_telephony`,
`error_unknown`, `spam`, `concurrency`, `cancelled`, `completed`,
`unknown`.

## Next

Why these types exist: [Architecture](../architecture.md).
Definitions: [Glossary](glossary.md).
