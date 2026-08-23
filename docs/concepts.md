# Voice agents

This page is for anyone who has not built a voice agent yet. It explains
the pieces you will see in obsalt, then points you at the install.

If you already know STT, TTS, turns, and webhooks, skip to
[What obsalt does](product.md) or [Getting started](getting-started.md).

---

## What a voice agent is

A **voice agent** is software that talks to a person on a live phone
call or in a browser. The person speaks. The agent listens, decides
what to say, and speaks back. Many agents also call **tools** — APIs
that look up an order, book an appointment, or issue a refund.

That is different from a chatbot. A chatbot exchanges text. A voice
agent has to hear audio, produce audio, and do it fast enough that the
call still feels like a conversation.

obsalt does **not** build the agent. You already have Vapi, Retell,
Pipecat, LiveKit, or something similar. obsalt records what happened on
live calls so you can debug them and measure quality.

---

## How a typical call is built

Most production agents use a **cascade**: three stages in a row.

1. **Speech-to-text (STT).** A transcriber turns the caller’s audio
   into words. Deepgram, AssemblyAI, and similar services do this.
2. **Language model (LLM).** A model reads those words (plus your
   prompt, knowledge, and tool results) and decides the next reply.
   It may call a tool before it speaks.
3. **Text-to-speech (TTS).** A voice model turns the reply into audio
   and plays it to the caller.

One back-and-forth — the caller says something, the agent answers — is
a **turn**. A call is a sequence of turns.

Some newer APIs skip the cascade. **OpenAI Realtime** and **Gemini
Live** are **speech-to-speech**: one model hears audio and produces
audio. There is no separate STT / LLM / TTS split you can time. obsalt
still records the call; it will not invent cascade stages that do not
exist.

```
Caller audio
    │
    ▼
 ┌─────┐    ┌─────┐    ┌─────┐
 │ STT │───▶│ LLM │───▶│ TTS │───▶ Agent audio
 └─────┘    └─────┘    └─────┘
               │
               ▼
            Tools (lookup, book, refund, …)
```

---

## Two ways people run agents

| You use… | Who runs STT / LLM / TTS | How data reaches obsalt |
| --- | --- | --- |
| **Vapi, Retell, ElevenLabs, Cartesia** | The hosted platform | They POST a **signed webhook** (an HTTP request with the call payload) to obsalt |
| **Pipecat, LiveKit, OpenAI Realtime, Gemini Live** | Your process | Your process emits **OTLP traces** (OpenTelemetry spans) to obsalt |

A **webhook** is “their server calls yours when something happens.” For
hosted platforms, that something is usually end-of-call, a transcript,
or a status update.

**OTLP** is the OpenTelemetry protocol. Custom agents already emit
spans for timing. obsalt receives those spans, builds a call record,
and can forward the same traces to Tempo or Grafana.

Pick **one** path per call. Pointing both a webhook and OTLP at the
same conversation creates two partial records that do not join.

---

## What goes wrong on live calls

These are the problems obsalt is built to show.

| Problem | What it looks like |
| --- | --- |
| **Latency** | The agent feels slow. Time may be in STT, the model, TTS, a tool, or the phone network. |
| **Hangups** | The caller leaves. Reasons include silence, an error, voicemail, or the user hanging up. |
| **Hallucination** | The agent invents a price, an order id, or “I’ve booked that” when the tool failed. |
| **Tools** | A function call fails, retries, or stalls the turn. |
| **Quality** | The agent was rude, skipped a disclosure, or over-promised — things you write as a rubric. |
| **Search** | You need “calls where someone asked about a refund” and a CSV grep is not enough. |

Hosted platforms often send **durations without clocks**: “STT took
180 ms” with no start and end time. obsalt shows that as a **chip**
(a number), not a **waterfall** (bars on a timeline). A waterfall is
only drawn when the source sent real start and end timestamps. That is
not a bug. See [The console](console.md).

---

## Words you will see in obsalt

Full list: [Glossary](reference/glossary.md).

| Term | Meaning |
| --- | --- |
| **Agent** | The voice bot you configured — “support”, “booking”, and so on. |
| **Call** | One live conversation. A row in the console. |
| **Turn** | One caller utterance plus the agent’s reply. |
| **Chip** | A duration we have, but cannot place on a timeline. |
| **Waterfall** | Stage bars drawn only from real start and end timestamps. |
| **Hangup** | Why the call ended, in a stable list (`user_hangup`, `silence_timeout`, …). |
| **Grounding** | Prompt, knowledge, tool results, and caller text that hallucination checks are allowed to trust. |
| **Ingest key** | Secret in the webhook URL. Shown once when you create a connection. |
| **Revision** | An immutable snapshot of a call. Late events create a new one. |
| **Provenance** | Where a number came from, or why it is missing. |
| **Plugin** | A separately installed package that knows one source (`obsalt-vapi`, `obsalt-pipecat`, …). Core ships none. |

---

## What you need to try obsalt

1. Python 3.11+ and Docker (for Postgres, ClickHouse, and object storage).
2. Either a hosted-platform account **or** an agent process that can
   emit OTLP.
3. A few real calls. Fixtures in this repo are for tests. The console
   fills from live traffic.

Then:

1. [Getting started](getting-started.md) — run the service and sign in.
2. [Connect a hosted platform](connect-hosted.md) or
   [Connect your own agent](connect-custom.md).
3. [The console](console.md) — read the call that landed.
