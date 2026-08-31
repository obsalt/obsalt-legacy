"""Replay vendored provider fixtures into a running serve over HTTP.

Seed is a black-box client: signed webhooks and OTLP, never a ClickHouse insert.
Decode still runs after ack. Not for production.
"""

from __future__ import annotations

import copy
import json
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any, cast

import httpx
from jsonschema import Draft202012Validator, ValidationError

from obsalt.config import Settings
from obsalt.crypto.primitives import hmac_hex
from obsalt.util import parse_datetime, utcnow

SEED_SECRET = "seed-secret"
ALLOWED_ENVIRONMENTS = frozenset({"dev", "test", "testing"})
DEFAULT_BASE = "http://localhost:8080"
DEFAULT_COUNT = 8
DEFAULT_WINDOW_DAYS = 7
POLL_TIMEOUT_SECONDS = 90.0

FIRST_PARTY = (
    "vapi",
    "retell",
    "elevenlabs",
    "cartesia",
    "pipecat",
    "livekit",
    "openai_realtime",
    "gemini_live",
)
WEBHOOK_PROVIDERS = frozenset({"vapi", "retell", "elevenlabs", "cartesia", "example"})
OTLP_PROVIDERS = frozenset({"pipecat", "livekit", "openai_realtime", "gemini_live"})

_MODULE = {
    "vapi": "obsalt_vapi",
    "retell": "obsalt_retell",
    "elevenlabs": "obsalt_elevenlabs",
    "cartesia": "obsalt_cartesia",
    "example": "obsalt_example",
    "pipecat": "obsalt_pipecat",
    "livekit": "obsalt_livekit",
    "openai_realtime": "obsalt_openai_realtime",
    "gemini_live": "obsalt_gemini_live",
}
_WEBHOOK_TEMPLATE = {
    "vapi": "raw/end_of_call.json",
    "retell": "raw/call_ended.json",
    "elevenlabs": "raw/post_call_transcription.json",
    "cartesia": "raw/call_ended.json",
    "example": "raw/call_ended.json",
}
_OTLP_TEMPLATE = {
    "pipecat": "otlp/stock_trace.json",
    "livekit": "otlp/room_trace.json",
    "openai_realtime": "otlp/s2s_trace.json",
    "gemini_live": "otlp/s2s_trace.json",
}

# Documented hangup enums only — must exist in the vendored data/*.json lists.
VAPI_HANGUPS = (
    "customer-ended-call",
    "assistant-ended-call",
    "silence-timed-out",
    "exceeded-max-duration",
    "pipeline-error-openai-llm-failed",
)
RETELL_HANGUPS = (
    "user_hangup",
    "agent_hangup",
    "voicemail_reached",
    "inactivity",
    "error_llm_websocket_runtime",
)


class SeedError(Exception):
    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class SeedEnvelope:
    provider: str
    kind: str  # "webhook" | "otlp"
    body: bytes
    source_call_id: str


@dataclass
class SeedReport:
    posted: int = 0
    calls: int = 0
    sources: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    start: str = ""
    end: str = ""
    console_url: str = ""
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "posted": self.posted,
            "calls": self.calls,
            "sources": self.sources,
            "skipped": self.skipped,
            "errors": self.errors,
            "start": self.start,
            "end": self.end,
            "console_url": self.console_url,
            "dry_run": self.dry_run,
        }


def environment_allowed(settings: Settings | None = None) -> bool:
    env = (settings or Settings()).environment.lower()
    return env in ALLOWED_ENVIRONMENTS


def selected_providers(requested: Sequence[str] | None, *, include_example: bool) -> list[str]:
    if requested:
        names = [item.strip() for item in requested if item.strip()]
    else:
        names = list(FIRST_PARTY)
        if include_example:
            names.append("example")
    unknown = [name for name in names if name not in _MODULE]
    if unknown:
        raise SeedError(f"unknown provider(s): {', '.join(unknown)}", exit_code=2)
    return names


def build_corpus(
    providers: Sequence[str],
    *,
    count: int,
    window_days: int,
    now: datetime | None = None,
) -> list[SeedEnvelope]:
    if count < 1:
        raise SeedError("--count must be >= 1", exit_code=2)
    if window_days < 1:
        raise SeedError("--window-days must be >= 1", exit_code=2)
    clock = now or utcnow()
    out: list[SeedEnvelope] = []
    for name in providers:
        try:
            if name in WEBHOOK_PROVIDERS:
                out.extend(_webhook_clones(name, count, window_days, clock))
            elif name in OTLP_PROVIDERS:
                out.extend(_otlp_clones(name, count, window_days, clock))
            else:
                raise SeedError(f"unknown provider {name!r}", exit_code=2)
        except ModuleNotFoundError:
            continue
    return out


def signed_headers(provider: str, raw: bytes, secret: str = SEED_SECRET) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if provider == "vapi":
        headers["authorization"] = f"Bearer {secret}"
        return headers
    if provider == "retell":
        ts = str(int(time.time() * 1000))
        digest = hmac_hex(secret, raw + ts.encode("utf-8"))
        headers["x-retell-signature"] = f"v={ts},d={digest}"
        return headers
    if provider == "elevenlabs":
        ts = str(int(time.time()))
        digest = hmac_hex(secret, f"{ts}.".encode() + raw)
        headers["elevenlabs-signature"] = f"t={ts},v0={digest}"
        return headers
    if provider == "cartesia":
        headers["x-webhook-secret"] = secret
        return headers
    if provider == "example":
        headers["x-obsalt-example-signature"] = hmac_hex(secret, raw)
        return headers
    raise SeedError(f"no signer for {provider}", exit_code=2)


def wrap_otlp(spans: list[dict[str, Any]]) -> dict[str, Any]:
    otlp_spans = []
    for span in spans:
        item: dict[str, Any] = {
            "traceId": span["trace_id"],
            "spanId": span["span_id"],
            "name": span["name"],
            "startTimeUnixNano": str(span["start_unix_nano"]),
            "endTimeUnixNano": str(span["end_unix_nano"]),
            "attributes": [
                _otlp_kv(key, value) for key, value in (span.get("attributes") or {}).items()
            ],
        }
        parent = span.get("parent_span_id")
        if parent:
            item["parentSpanId"] = parent
        otlp_spans.append(item)
    return {"resourceSpans": [{"scopeSpans": [{"spans": otlp_spans}]}]}


def connection_body(provider: str) -> dict[str, Any]:
    settings: dict[str, Any] = {"seed": True}
    if provider == "vapi":
        secrets_map = {"legacy_secret": SEED_SECRET}
        settings["auth_mode"] = "legacy_secret"
    elif provider == "retell":
        secrets_map = {"api_key": SEED_SECRET}
    elif provider == "elevenlabs":
        secrets_map = {"webhook_secret": SEED_SECRET}
    elif provider == "cartesia":
        secrets_map = {"webhook_secret": SEED_SECRET}
    elif provider == "example":
        secrets_map = {"hmac_secret": SEED_SECRET}
    else:
        raise SeedError(f"{provider} is not a webhook source", exit_code=2)
    return {"provider": provider, "secrets": secrets_map, "settings": settings}


def run_seed(
    *,
    client: httpx.Client | None,
    api_key: str,
    count: int = DEFAULT_COUNT,
    providers: Sequence[str] | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    include_example: bool = False,
    dry_run: bool = False,
    poll_timeout: float = POLL_TIMEOUT_SECONDS,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    base_url: str = DEFAULT_BASE,
) -> SeedReport:
    settings = Settings()
    if not environment_allowed(settings):
        raise SeedError(
            "obsalt seed refuses OBSALT_ENVIRONMENT=production. Use a dev stack.",
            exit_code=2,
        )
    clock = now or utcnow()
    names = selected_providers(providers, include_example=include_example)
    local_ok = _locally_installed(names)
    missing_pkg = [name for name in names if name not in local_ok]
    names = [name for name in names if name in local_ok]
    envelopes = build_corpus(names, count=count, window_days=window_days, now=clock)
    end = clock
    start = clock - timedelta(days=window_days)
    report = SeedReport(
        start=_iso(start),
        end=_iso(end),
        skipped=[f"{name}: plugin package not installed locally" for name in missing_pkg],
        dry_run=dry_run,
        console_url=f"{base_url.rstrip('/')}/v1/ui?start={_iso(start)}&end={_iso(end)}",
    )
    for env in envelopes:
        report.sources[env.provider] = report.sources.get(env.provider, 0) + 1
    if dry_run:
        report.posted = len(envelopes)
        return report
    if client is None:
        raise SeedError("HTTP client is required unless --dry-run", exit_code=2)
    _require_health(client, base_url)
    installed = _server_plugins(client, api_key)
    ingest_keys = _ensure_seed_connections(
        client, api_key, [n for n in names if n in WEBHOOK_PROVIDERS and n in installed]
    )
    for name in names:
        if name not in installed:
            report.skipped.append(f"{name}: not installed on serve")
            report.sources.pop(name, None)
    expected = 0
    for envelope in envelopes:
        if envelope.provider not in installed:
            continue
        try:
            _post_envelope(client, api_key, envelope, ingest_keys)
        except SeedError as exc:
            report.errors.append(str(exc))
            continue
        expected += 1
        report.posted += 1
    if report.errors:
        raise SeedError("\n".join(report.errors), exit_code=2)
    if expected == 0:
        raise SeedError(
            "nothing to post. Install provider plugins (`uv sync --all-packages`) "
            "and confirm `obsalt plugins` matches serve.",
            exit_code=2,
        )
    report.calls = _wait_calls(
        client, api_key, report.start, report.end, expected, poll_timeout, sleep
    )
    return report


def format_report(report: SeedReport) -> str:
    if report.dry_run:
        lines = [
            "NOT FOR PRODUCTION. Dry run — no HTTP.",
            f"Would post {report.posted} envelopes: "
            + ", ".join(f"{k}={v}" for k, v in sorted(report.sources.items())),
        ]
    else:
        lines = [
            "NOT FOR PRODUCTION. "
            f"Posted {report.posted} envelopes across {len(report.sources)} sources; "
            f"{report.calls} calls now queryable.",
            f"Console: {report.console_url}",
            "Re-run to add more. Wipe with POST /v1/privacy/deletion-requests.",
        ]
    for skipped in report.skipped:
        lines.append(f"skipped: {skipped}")
    return "\n".join(lines)


def _locally_installed(names: Sequence[str]) -> set[str]:
    ok: set[str] = set()
    for name in names:
        try:
            files(_MODULE[name])
        except ModuleNotFoundError:
            continue
        ok.add(name)
    return ok


def _read_fixture(module: str, relative: str) -> bytes:
    trav = files(module).joinpath("fixtures")
    for part in relative.split("/"):
        trav = trav.joinpath(part)
    return trav.read_bytes()


def _vendor_schema(module: str) -> dict[str, Any] | None:
    schema_dir = files(module).joinpath("fixtures", "schema")
    if not schema_dir.is_dir():
        return None
    for item in schema_dir.iterdir():
        if item.name.endswith(".json") and not item.name.startswith("PIN"):
            schema: dict[str, Any] = json.loads(item.read_text(encoding="utf-8"))
            return schema
    return None


def _validate(module: str, payload: dict[str, Any]) -> None:
    schema = _vendor_schema(module)
    if schema is None:
        return
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        raise SeedError(f"{module} clone failed vendor schema: {exc.message}", exit_code=2) from exc


def _webhook_clones(name: str, count: int, window_days: int, now: datetime) -> list[SeedEnvelope]:
    module = _MODULE[name]
    raw = json.loads(_read_fixture(module, _WEBHOOK_TEMPLATE[name]))
    out: list[SeedEnvelope] = []
    window = timedelta(days=window_days)
    for index in range(count):
        target = now - window * (index + 1) / max(count, 1)
        cloned, call_id = _clone_webhook(name, raw, index, target)
        _validate(module, cloned)
        out.append(
            SeedEnvelope(
                provider=name,
                kind="webhook",
                body=json.dumps(cloned, separators=(",", ":")).encode(),
                source_call_id=call_id,
            )
        )
    return out


def _otlp_clones(name: str, count: int, window_days: int, now: datetime) -> list[SeedEnvelope]:
    module = _MODULE[name]
    spans = json.loads(_read_fixture(module, _OTLP_TEMPLATE[name]))
    if not isinstance(spans, list):
        raise SeedError(f"{name} OTLP fixture must be a span list", exit_code=2)
    out: list[SeedEnvelope] = []
    window = timedelta(days=window_days)
    for index in range(count):
        target = now - window * (index + 1) / max(count, 1)
        cloned, call_id = _clone_otlp(name, spans, index, target)
        body = json.dumps(wrap_otlp(cloned), separators=(",", ":")).encode()
        out.append(SeedEnvelope(provider=name, kind="otlp", body=body, source_call_id=call_id))
    return out


def _clone_webhook(
    name: str, raw: dict[str, Any], index: int, target: datetime
) -> tuple[dict[str, Any], str]:
    payload = copy.deepcopy(raw)
    token = secrets.token_hex(4)
    call_id = f"seed-{name}-{index:04d}-{token}"
    if name == "vapi":
        original = parse_datetime(_dig(payload, ("message", "startedAt"))) or target
        delta = target - original
        _set(payload, ("message", "call", "id"), call_id)
        _shift_iso(payload, ("message", "startedAt"), delta)
        _shift_iso(payload, ("message", "endedAt"), delta)
        _shift_iso(payload, ("message", "call", "startedAt"), delta)
        _shift_iso(payload, ("message", "call", "endedAt"), delta)
        messages = _dig(payload, ("message", "artifact", "messages"))
        if isinstance(messages, list):
            for row in messages:
                if isinstance(row, dict) and isinstance(row.get("time"), (int, float)):
                    row["time"] = _shift_epoch(row["time"], delta)
        reason = VAPI_HANGUPS[index % len(VAPI_HANGUPS)]
        _set(payload, ("message", "endedReason"), reason)
        _set(payload, ("message", "call", "endedReason"), reason)
    elif name == "retell":
        original = parse_datetime(_dig(payload, ("call", "start_timestamp"))) or target
        delta = target - original
        _set(payload, ("call", "call_id"), call_id)
        _shift_num(payload, ("call", "start_timestamp"), delta)
        _shift_num(payload, ("call", "end_timestamp"), delta)
        _set(payload, ("call", "disconnection_reason"), RETELL_HANGUPS[index % len(RETELL_HANGUPS)])
    elif name == "elevenlabs":
        original = (
            parse_datetime(_dig(payload, ("data", "metadata", "start_time_unix_secs"))) or target
        )
        delta = target - original
        _set(payload, ("data", "conversation_id"), call_id)
        _shift_num(payload, ("event_timestamp",), delta)
        _shift_num(payload, ("data", "metadata", "start_time_unix_secs"), delta)
    elif name == "cartesia":
        original = parse_datetime(_dig(payload, ("started_at",))) or target
        delta = target - original
        _set(payload, ("id",), call_id)
        _set(payload, ("call_id",), call_id)
        _shift_iso(payload, ("started_at",), delta)
        _shift_iso(payload, ("ended_at",), delta)
        turns = payload.get("turns")
        if isinstance(turns, list):
            for row in turns:
                if not isinstance(row, dict):
                    continue
                if "started_at" in row:
                    dt = parse_datetime(row["started_at"])
                    if dt is not None:
                        row["started_at"] = _iso(dt + delta)
                if "ended_at" in row:
                    dt = parse_datetime(row["ended_at"])
                    if dt is not None:
                        row["ended_at"] = _iso(dt + delta)
    elif name == "example":
        first_turn = payload.get("turns")
        start_s = None
        if isinstance(first_turn, list) and first_turn and isinstance(first_turn[0], dict):
            start_s = parse_datetime(first_turn[0].get("started_at"))
        delta = target - (start_s or target)
        _set(payload, ("call_id",), call_id)
        _set(payload, ("delivery_id",), f"{call_id}-del")
        if isinstance(first_turn, list):
            for row in first_turn:
                if not isinstance(row, dict):
                    continue
                for key in ("started_at", "ended_at"):
                    dt = parse_datetime(row.get(key))
                    if dt is not None:
                        row[key] = _iso(dt + delta)
    else:
        raise SeedError(f"no webhook cloner for {name}", exit_code=2)
    return payload, call_id


def _clone_otlp(
    name: str, spans: list[dict[str, Any]], index: int, target: datetime
) -> tuple[list[dict[str, Any]], str]:
    cloned = copy.deepcopy(spans)
    nanos = [int(row.get("start_unix_nano") or 0) for row in cloned]
    origin_ns = min((n for n in nanos if n), default=0)
    target_ns = int(target.timestamp() * 1e9)
    delta_ns = target_ns - origin_ns
    call_id = f"seed-{name}-{index:04d}-{secrets.token_hex(4)}"
    trace_id = secrets.token_hex(16)
    span_ids = {str(row.get("span_id")): secrets.token_hex(8) for row in cloned}
    for row in cloned:
        row["trace_id"] = trace_id
        old = str(row.get("span_id"))
        row["span_id"] = span_ids.get(old, secrets.token_hex(8))
        parent = row.get("parent_span_id")
        if parent:
            row["parent_span_id"] = span_ids.get(str(parent), parent)
        row["start_unix_nano"] = int(row.get("start_unix_nano") or 0) + delta_ns
        row["end_unix_nano"] = int(row.get("end_unix_nano") or 0) + delta_ns
        attrs = row.get("attributes")
        if isinstance(attrs, dict):
            for key in (
                "gen_ai.conversation.id",
                "lk.room.name",
                "elevenlabs.conversation_id",
                "call.id",
                "call.provider_id",
                "obsalt.call_id",
            ):
                if key in attrs:
                    attrs[key] = call_id
    return cloned, call_id


def _otlp_kv(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        inner: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int) and not isinstance(value, bool):
        inner = {"intValue": str(value)}
    elif isinstance(value, float):
        inner = {"doubleValue": value}
    else:
        inner = {"stringValue": str(value)}
    return {"key": key, "value": inner}


def _require_health(client: httpx.Client, base_url: str) -> None:
    try:
        response = client.get("/health")
    except httpx.RequestError as exc:
        raise SeedError(
            f"Could not connect to obsalt serve at {base_url}.\n"
            "Supported path: docker compose up -d && obsalt seed\n"
            f"{exc}",
            exit_code=2,
        ) from exc
    if response.status_code != 200:
        raise SeedError(
            f"GET /health returned {response.status_code}. Start serve: docker compose up -d",
            exit_code=2,
        )


def _server_plugins(client: httpx.Client, api_key: str) -> set[str]:
    response = client.get("/v1/plugins", headers=_auth(api_key))
    if response.status_code != 200:
        raise SeedError(
            f"GET /v1/plugins returned {response.status_code}: {response.text[:300]}",
            exit_code=2,
        )
    items = response.json().get("items") or []
    return {str(item.get("name")) for item in items if item.get("name")}


def _ensure_seed_connections(
    client: httpx.Client, api_key: str, providers: Sequence[str]
) -> dict[str, str]:
    listed = client.get("/v1/connections", headers=_auth(api_key))
    if listed.status_code != 200:
        raise SeedError(
            f"GET /v1/connections returned {listed.status_code}: {listed.text[:300]}",
            exit_code=2,
        )
    for item in listed.json().get("items") or []:
        settings = item.get("settings") or {}
        if settings.get("seed") is True or settings.get("seed") == "true":
            deleted = client.delete(
                f"/v1/connections/{item['connection_id']}", headers=_auth(api_key)
            )
            if deleted.status_code not in {200, 404}:
                raise SeedError(
                    f"DELETE connection {item['connection_id']} returned {deleted.status_code}",
                    exit_code=2,
                )
    keys: dict[str, str] = {}
    for name in providers:
        created = client.post(
            "/v1/connections",
            headers={**_auth(api_key), "content-type": "application/json"},
            json=connection_body(name),
        )
        if created.status_code != 200:
            raise SeedError(
                f"POST /v1/connections ({name}) returned {created.status_code}: "
                f"{created.text[:300]}",
                exit_code=2,
            )
        body = created.json()
        ingest_key = body.get("ingest_key")
        if not ingest_key:
            raise SeedError(f"connection for {name} did not return ingest_key", exit_code=2)
        keys[name] = str(ingest_key)
    return keys


def _post_envelope(
    client: httpx.Client,
    api_key: str,
    envelope: SeedEnvelope,
    ingest_keys: dict[str, str],
) -> None:
    if envelope.kind == "webhook":
        ingest_key = ingest_keys.get(envelope.provider)
        if not ingest_key:
            raise SeedError(f"no ingest_key for {envelope.provider}", exit_code=2)
        headers = signed_headers(envelope.provider, envelope.body)
        response = client.post(
            f"/v1/ingest/{envelope.provider}/{ingest_key}",
            content=envelope.body,
            headers=headers,
        )
        if response.status_code != 200:
            raise SeedError(
                f"POST /v1/ingest/{envelope.provider}/… returned {response.status_code}: "
                f"{response.text[:400]}",
                exit_code=2,
            )
        return
    response = client.post(
        "/v1/traces",
        content=envelope.body,
        headers={**_auth(api_key), "content-type": "application/json"},
    )
    if response.status_code != 200:
        raise SeedError(
            f"POST /v1/traces ({envelope.provider}) returned {response.status_code}: "
            f"{response.text[:400]}",
            exit_code=2,
        )


def _ready_snapshot(client: httpx.Client) -> str:
    try:
        response = client.get("/ready")
    except httpx.RequestError as exc:
        return f"GET /ready failed: {exc}"
    if response.status_code != 200:
        return f"GET /ready returned {response.status_code}"
    body = response.json() or {}
    return (
        f"outbox_depth={body.get('outbox_depth')} "
        f"dlq_depth={body.get('dlq_depth')} "
        f"inbox_age_seconds={body.get('inbox_age_seconds')}"
    )


def _wait_calls(
    client: httpx.Client,
    api_key: str,
    start: str,
    end: str,
    expected: int,
    timeout: float,
    sleep: Callable[[float], None],
) -> int:
    deadline = time.monotonic() + timeout
    seen = 0
    need = min(max(expected, 1), 100)
    while time.monotonic() < deadline:
        response = client.get(
            "/v1/calls",
            params={"start": start, "end": end, "limit": need},
            headers=_auth(api_key),
        )
        if response.status_code == 200:
            seen = len(response.json().get("items") or [])
            if seen >= need:
                return seen
        elif response.status_code >= 500:
            raise SeedError(
                f"GET /v1/calls returned {response.status_code}: {response.text[:400]}. "
                f"{_ready_snapshot(client)}",
                exit_code=2,
            )
        sleep(0.05)
    raise SeedError(
        f"posted {expected} envelopes but GET /v1/calls returned {seen}. "
        f"{_ready_snapshot(client)}. "
        "Decode runs in `obsalt worker` after ack (compose runs one). "
        "If dlq_depth is rising, check `docker compose logs worker`.",
        exit_code=2,
    )


def _auth(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    cur = payload
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur: Any = payload
    for part in path[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return
        cur = nxt
    if isinstance(cur, dict) and path[-1] in cur:
        cur[path[-1]] = value


def _shift_iso(payload: dict[str, Any], path: tuple[str, ...], delta: timedelta) -> None:
    current = _dig(payload, path)
    parsed = parse_datetime(current)
    if parsed is None:
        return
    _set(payload, path, _iso(parsed + delta))


def _shift_num(payload: dict[str, Any], path: tuple[str, ...], delta: timedelta) -> None:
    current = _dig(payload, path)
    if isinstance(current, (int, float)):
        _set(payload, path, _shift_epoch(current, delta))


def _shift_epoch(value: int | float, delta: timedelta) -> int:
    seconds = delta.total_seconds()
    number = float(value)
    if number > 1e16:
        return int(number + seconds * 1e9)
    if number > 1e12:
        return int(number + seconds * 1000)
    return int(number + seconds)


def ingest_seed_corpus(
    state: Any,
    *,
    org_id: str,
    count: int = 1,
    window_days: int = DEFAULT_WINDOW_DAYS,
    include_example: bool = False,
    now: datetime | None = None,
) -> SeedReport:
    """Queue vendored fixtures through receive, not HTTP-to-self. Decode stays a worker."""

    if not environment_allowed(getattr(state, "settings", None)):
        raise SeedError(
            "obsalt seed refuses OBSALT_ENVIRONMENT=production. Use a dev stack.",
            exit_code=2,
        )
    from obsalt.ingest.headers import RawHeaders
    from obsalt.ingest.otlp import receive_otlp_batch
    from obsalt.ingest.receive import ReceiveLimits, receive_webhook
    from obsalt.otel.receiver import parse_otlp_request, request_to_spans
    from obsalt.plugin.contract import WebhookSource
    from obsalt.plugin.host import plugin_by_name
    from obsalt.runtime import create_connection

    clock = now or utcnow()
    installed = {plugin.name for plugin in getattr(state, "plugins", [])}
    names = [
        name
        for name in selected_providers(None, include_example=include_example)
        if name in installed
    ]
    envelopes = build_corpus(names, count=count, window_days=window_days, now=clock)
    start = clock - timedelta(days=window_days)
    report = SeedReport(
        start=_iso(start),
        end=_iso(clock),
        skipped=[
            f"{name}: not installed on serve" for name in FIRST_PARTY if name not in installed
        ],
        console_url="/v1/ui",
    )
    ingest_keys: dict[str, str] = {}
    for name in names:
        if name not in WEBHOOK_PROVIDERS:
            continue
        body = connection_body(name)
        created = create_connection(
            state,
            org_id=org_id,
            provider=name,
            secrets=dict(body.get("secrets") or {}),
            settings=dict(body.get("settings") or {}),
        )
        ingest_keys[name] = created["ingest_key"]
    limits = ReceiveLimits(
        compressed_bytes=state.settings.compressed_body_limit,
        expanded_bytes=state.settings.expanded_body_limit,
    )
    span_index = getattr(state, "span_identities", None)
    for envelope in envelopes:
        report.sources[envelope.provider] = report.sources.get(envelope.provider, 0) + 1
        if envelope.kind == "webhook":
            ingest_key = ingest_keys.get(envelope.provider)
            if not ingest_key:
                report.skipped.append(f"{envelope.provider}: no ingest_key")
                continue
            try:
                loaded = plugin_by_name(envelope.provider, state.plugins)
            except KeyError:
                report.skipped.append(f"{envelope.provider}: plugin not installed")
                continue
            headers = signed_headers(envelope.provider, envelope.body)
            pairs = [
                (key.encode("latin-1"), value.encode("latin-1")) for key, value in headers.items()
            ]
            result = receive_webhook(
                provider=envelope.provider,
                ingest_key=ingest_key,
                raw=envelope.body,
                headers=RawHeaders(pairs),
                resolver=state.resolver,
                plugin=cast(WebhookSource, loaded.plugin),
                objects=state.objects,
                inbox=state.inbox,
                limits=limits,
                leases=getattr(state, "leases", None),
            )
            if result.rejected or result.envelope is None:
                report.errors.append(f"{envelope.provider}: {result.rejected or 'rejected'}")
                continue
            report.posted += 1
            continue
        try:
            req = parse_otlp_request(
                "application/json",
                envelope.body,
                None,
                expanded_bytes=state.settings.expanded_body_limit,
            )
        except Exception as exc:  # noqa: BLE001 — surface parse errors per envelope
            report.errors.append(f"{envelope.provider}: {exc}")
            continue
        spans = request_to_spans(req)
        otlp_result = receive_otlp_batch(
            org_id=org_id,
            raw=envelope.body,
            content_type="application/json",
            objects=state.objects,
            inbox=state.inbox,
            limits=limits,
            spans=spans,
            span_index=span_index,
            leases=getattr(state, "leases", None),
            backpressure_limit=state.settings.outbox_backpressure_limit,
        )
        if otlp_result.envelope is None or otlp_result.rejected:
            report.errors.append(f"{envelope.provider}: {otlp_result.rejected or 'rejected'}")
            continue
        report.posted += 1
    return report
