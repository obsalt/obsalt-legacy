"""Server-rendered correlation UI. Not a fleet dashboard — Grafana still owns that.

Transcripts live in the evidence pane. Span attributes stay reviewer-safe.
"""

from __future__ import annotations

import html
from typing import Any

from obsalt._version import __version__
from obsalt.tracing.timeline import CallView

_CSS = """
:root {
  --bg: #111111;
  --panel: #1a1a1a;
  --ink: #f3f1eb;
  --muted: #9a958a;
  --line: #2c2c2c;
  --ok: #c4d39a;
  --warn: #d4b562;
  --bad: #d67a6f;
  --call: #e8e2d6;
  --stt: #8fbfb4;
  --llm: #c9b07a;
  --tts: #d08a5a;
  --tool: #8aa4c9;
  --eval: #c48aa8;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.45 "IBM Plex Sans", "Source Sans 3", "Segoe UI", sans-serif; }
a { color: var(--ok); }
header.app {
  display: flex; justify-content: space-between; align-items: baseline;
  padding: 18px 24px 12px; border-bottom: 1px solid var(--line);
}
header.app strong { letter-spacing: 0.08em; font-size: 12px; text-transform: uppercase; }
header.app span { color: var(--muted); font-size: 12px; }
.wrap { padding: 16px 24px 48px; display: grid; gap: 16px; }
.meta { display: flex; flex-wrap: wrap; gap: 8px 18px; color: var(--muted); font-size: 12px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase;
  border: 1px solid var(--line); padding: 3px 8px; border-radius: 999px; color: var(--muted);
}
.chip.ok { color: var(--ok); border-color: #3d4a2c; }
.chip.bad { color: var(--warn); border-color: #5a4a1c; }
.grid { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 16px; }
@media (max-width: 960px) { .grid { grid-template-columns: 1fr; } }
.panel { background: var(--panel); border: 1px solid var(--line); padding: 14px 16px; }
.panel h2 { margin: 0 0 10px; font-size: 12px; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--muted); font-weight: 600; }
.note { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
.span {
  display: grid; grid-template-columns: 220px 1fr 72px; gap: 8px; align-items: center;
  margin: 3px 0; font-size: 12px;
}
.span .name { font-family: ui-monospace, "IBM Plex Mono", monospace; color: var(--call); }
.span.child .name { padding-left: 14px; color: var(--muted); }
.span.gchild .name { padding-left: 28px; }
.bar { height: 8px; background: #2a2a2a; position: relative; }
.bar > i { display: block; height: 100%; background: var(--call); }
.span[data-kind="stt"] .bar > i { background: var(--stt); }
.span[data-kind="llm"] .bar > i { background: var(--llm); }
.span[data-kind="tts"] .bar > i, .span[data-kind="playout"] .bar > i { background: var(--tts); }
.span[data-kind="tool"] .bar > i { background: var(--tool); }
.span[data-kind="eval"] .bar > i { background: var(--eval); }
.span.error .name { color: var(--bad); }
.ms { text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
.turn { padding: 10px 0; border-top: 1px solid var(--line); }
.turn:first-of-type { border-top: 0; }
.turn .who { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
.turn p { margin: 4px 0 0; }
.turn.active { background: #222018; margin: 0 -16px; padding: 10px 16px; }
.gaps { margin: 0; padding-left: 18px; color: var(--muted); font-size: 12px; }
.gaps li { margin: 4px 0; }
.gaps .structural { color: var(--muted); }
.gaps .missing { color: var(--warn); }
audio { width: 100%; margin-top: 8px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }
.list a { display: block; padding: 10px 0; border-bottom: 1px solid var(--line); text-decoration: none; color: var(--ink); }
.list a:hover { color: var(--ok); }
.fail { color: var(--bad); }
.gate { max-width: 420px; margin: 48px auto; padding: 24px; background: var(--panel); border: 1px solid var(--line); }
.gate input { width: 100%; margin: 8px 0 12px; padding: 8px; background: var(--bg); color: var(--ink); border: 1px solid var(--line); }
.gate button { background: var(--ink); color: var(--bg); border: 0; padding: 8px 14px; cursor: pointer; }
"""


def render_index_html(*, org_id: str, calls: list[dict[str, Any]], require_auth: bool) -> str:
    rows = []
    for item in calls:
        cid = html.escape(str(item.get("id", "")))
        agent = html.escape(str(item.get("agent_id") or ""))
        provider = html.escape(str(item.get("provider") or ""))
        complete = item.get("coverage", {}).get("completeness")
        pct = f"{complete:.0%}" if isinstance(complete, float) else "—"
        hangup = html.escape(str(item.get("hangup_reason") or "—"))
        rows.append(
            f'<a href="/v1/calls/{cid}/ui"><strong>{cid}</strong>'
            f'<div class="note">{provider} · {agent} · hangup {hangup} · coverage {html.escape(pct)}</div></a>'
        )
    body = "".join(rows) or '<p class="note">No calls in this tenant yet.</p>'
    return _page(
        title="obsalt calls",
        body=f"""
<header class="app"><strong>obsalt</strong><span>{html.escape(org_id)} · evidence + span tree</span></header>
<div class="wrap">
  <p class="note">Per-call join view. Fleet waterfalls and SLOs stay in Grafana Tempo / Prometheus.</p>
  <div class="panel"><h2>Calls</h2><div class="list">{body}</div></div>
</div>
""",
        require_auth=require_auth,
    )


def render_call_html(view: CallView, *, org_id: str, require_auth: bool) -> str:
    join = view.join
    call_id = html.escape(str(join.get("call.id", "")))
    evidence = view.evidence
    coverage = view.coverage
    chips = []
    for name, present in coverage.signals.items():
        klass = "ok" if present else "bad"
        chips.append(f'<span class="chip {klass}">{html.escape(name)}</span>')
    gaps = []
    for gap in coverage.gaps:
        gaps.append(
            f'<li class="{html.escape(gap.kind)}"><strong>{html.escape(gap.id)}</strong> '
            f'({html.escape(gap.kind)}) — {html.escape(gap.reason)}</li>'
        )
    waterfall = _waterfall(view.trace.get("spans") or [], view.trace.get("source") == "live")
    transcript = _transcript(evidence.get("turns") or [])
    recording = evidence.get("recording_url")
    audio = (
        f'<audio controls src="{html.escape(str(recording))}"></audio>'
        if recording
        else '<p class="note">No recording_url on this call.</p>'
    )
    tools = _tools(evidence.get("tools") or [])
    evals = _evals(evidence.get("evals") or [], evidence.get("hallucinations") or [])
    hangup = evidence.get("hangup") or {}
    hangup_line = "—"
    if hangup:
        hangup_line = f"{hangup.get('reason')} / {hangup.get('party')} · loss {hangup.get('loss_score')}"
    source = html.escape(str(view.trace.get("source", "")))
    return _page(
        title=f"obsalt {call_id}",
        body=f"""
<header class="app">
  <strong><a href="/v1/ui">obsalt</a></strong>
  <span>{html.escape(org_id)} · join key call.id</span>
</header>
<div class="wrap">
  <div class="meta">
    <span>call.id <code>{call_id}</code></span>
    <span>provider_id <code>{html.escape(str(join.get("call.provider_id", "")))}</code></span>
    <span>agent {html.escape(str(evidence.get("agent_id") or join.get("agent.id") or ""))}</span>
    <span>trace {source}</span>
    <span>hangup {html.escape(str(hangup_line))}</span>
    <span><a href="/v1/calls/{call_id}/view">JSON</a></span>
  </div>
  <div class="chips">{''.join(chips)}</div>
  <div class="grid">
    <div class="panel" id="waterfall">
      <h2>Where time went</h2>
      <p class="note">Conversation span tree. Attributes are timings and join keys — not transcripts.</p>
      {waterfall}
    </div>
    <div class="panel" id="transcript">
      <h2>What was said</h2>
      <p class="note">Evidence store. Same call.id as the spans on the left.</p>
      {transcript}
      {audio}
    </div>
  </div>
  <div class="grid">
    <div class="panel"><h2>Tools</h2>{tools}</div>
    <div class="panel"><h2>Evals</h2>{evals}</div>
  </div>
  <div class="panel">
    <h2>Coverage gaps</h2>
    <p class="note">Asymmetry is explicit. Structural = this path cannot produce the signal. Missing = this call did not.</p>
    <ul class="gaps">{''.join(gaps) or '<li>No gaps listed.</li>'}</ul>
  </div>
</div>
""",
        require_auth=require_auth,
    )


def render_login_html(*, next_path: str, require_auth: bool) -> str:
    return _page(
        title="obsalt",
        body=f"""
<div class="gate">
  <strong>obsalt</strong>
  <p class="note">This tenant requires an API key. It is kept in sessionStorage and sent as X-API-Key.</p>
  <input id="key" type="password" autocomplete="off" placeholder="API key"/>
  <button type="button" id="go">Open</button>
</div>
<script>
const next = {next_path!r};
document.getElementById("go").onclick = () => {{
  sessionStorage.setItem("obsalt_api_key", document.getElementById("key").value);
  location.href = next;
}};
</script>
""",
        require_auth=require_auth,
        gated=True,
    )


def _waterfall(spans: list[dict[str, Any]], live: bool) -> str:
    if not spans:
        return '<p class="note">No span tree could be built from evidence.</p>'
    max_ms = max(( (s.get("start_ms") or 0) + (s.get("duration_ms") or 0) for s in spans), default=1) or 1
    depth = {"call": 0}
    rows = []
    if live:
        rows.append('<p class="note">Live OTLP spans were exported; this tree is rebuilt from evidence so transcript and waterfall share one page.</p>')
    for span in spans:
        pid = span.get("parent_id")
        level = 0 if not pid else depth.get(pid, 0) + 1
        depth[span["id"]] = level
        kind = "call"
        name = span.get("name") or ""
        if name.startswith("stt"):
            kind = "stt"
        elif name.startswith("llm.tool"):
            kind = "tool"
        elif name.startswith("llm"):
            kind = "llm"
        elif name.startswith("tts") or name.startswith("audio"):
            kind = "tts"
        elif name.startswith("evaluation") or name.startswith("transcript"):
            kind = "eval"
        start = float(span.get("start_ms") or 0)
        dur = float(span.get("duration_ms") or 0)
        left = max(0.0, 100.0 * start / max_ms)
        width = max(0.6, 100.0 * dur / max_ms) if dur else 0.6
        klass = "span"
        if level == 1:
            klass += " child"
        elif level >= 2:
            klass += " gchild"
        if span.get("status") == "error":
            klass += " error"
        label = html.escape(name)
        ms = f"{dur:.0f}ms" if dur else "—"
        turn = span.get("turn_index")
        turn_attr = f' data-turn="{int(turn)}"' if turn is not None else ""
        rows.append(
            f'<div class="{klass}" data-kind="{kind}"{turn_attr}>'
            f'<div class="name">{label}</div>'
            f'<div class="bar"><i style="margin-left:{left:.2f}%;width:{width:.2f}%"></i></div>'
            f'<div class="ms">{ms}</div></div>'
        )
    return "".join(rows)


def _transcript(turns: list[dict[str, Any]]) -> str:
    if not turns:
        return '<p class="note">No turns stored.</p>'
    blocks = []
    for turn in turns:
        who = html.escape(str(turn.get("speaker") or "unknown"))
        text = html.escape(str(turn.get("text") or ""))
        idx = int(turn.get("index") or 0)
        blocks.append(
            f'<div class="turn" data-turn="{idx}"><div class="who">{who} · turn {idx}</div><p>{text}</p></div>'
        )
    return "".join(blocks) + """
<script>
document.querySelectorAll(".turn, .span").forEach((el) => {
  el.addEventListener("click", () => {
    const t = el.getAttribute("data-turn");
    document.querySelectorAll(".turn").forEach((n) => n.classList.toggle("active", n.getAttribute("data-turn") === t));
  });
});
</script>
"""


def _tools(tools: list[dict[str, Any]]) -> str:
    if not tools:
        return '<p class="note">No tool invocations.</p>'
    rows = [
        "<tr><th>Name</th><th>Status</th><th>ms</th><th>Shape</th></tr>"
    ]
    for tool in tools:
        status = html.escape(str(tool.get("status") or ""))
        klass = "fail" if status in {"error", "timeout"} else ""
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(tool.get('name') or ''))}</td>"
            f'<td class="{klass}">{status}</td>'
            f"<td>{html.escape(str(tool.get('duration_ms') if tool.get('duration_ms') is not None else '—'))}</td>"
            f"<td><code>{html.escape(str(tool.get('payload_shape') or ''))}</code></td>"
            "</tr>"
        )
    return "<table>" + "".join(rows) + "</table>"


def _evals(evals: list[dict[str, Any]], hallucinations: list[dict[str, Any]]) -> str:
    lines = []
    for item in evals:
        mark = "PASS" if item.get("passed") else "FAIL"
        klass = "" if item.get("passed") else "fail"
        lines.append(
            f'<div><span class="{klass}">{mark}</span> {html.escape(str(item.get("rubric_name") or item.get("rubric_id")))}'
            f' · {html.escape(str(item.get("score")))}</div>'
        )
    for flag in hallucinations:
        lines.append(
            f'<div class="fail">HALLUCINATION {html.escape(str(flag.get("kind")))} · turn {html.escape(str(flag.get("turn_index")))}</div>'
        )
    return "".join(lines) or '<p class="note">No evals on this call.</p>'


def _page(*, title: str, body: str, require_auth: bool, gated: bool = False) -> str:
    boot = ""
    if require_auth and not gated:
        boot = """
<script>
(function () {
  const key = sessionStorage.getItem("obsalt_api_key");
  if (!key) return;
  document.querySelectorAll("a[href^='/v1/']").forEach((a) => {
    a.addEventListener("click", (ev) => {
      if (a.getAttribute("href").endsWith("/view")) {
        ev.preventDefault();
        fetch(a.getAttribute("href"), { headers: { "X-API-Key": key } })
          .then((r) => r.json())
          .then((j) => {
            const w = window.open("", "_blank");
            w.document.write("<pre>" + JSON.stringify(j, null, 2).replace(/</g, "&lt;") + "</pre>");
          });
      }
    });
  });
})();
</script>
"""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
<footer class="wrap note">obsalt {html.escape(__version__)} · traces stay PII-free · evidence is tenant-scoped</footer>
{boot}
</body>
</html>
"""
