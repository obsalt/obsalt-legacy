"""Command-line entry point. Production commands talk to the compose stack."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import uvicorn

from obsalt._version import __version__
from obsalt.api import create_app
from obsalt.config import ENV_EXAMPLE, Settings
from obsalt.ops.doctor import format_report, plugin_rows, run_doctor
from obsalt.plugin.host import plugin_by_name
from obsalt.runtime import in_memory_state, production_state

_EPILOG = """Typical local path:
  docker compose up -d
  obsalt init --write-env
  obsalt doctor
  obsalt serve          # terminal 1 — API + console
  obsalt worker         # terminal 2 — decode / assemble / analyze
  obsalt seed           # optional: fill the console from vendored fixtures

Docs: https://github.com/coder-with-a-bushido/obsalt/tree/main/docs
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsalt",
        description="Self-hosted call analytics for AI voice agents",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="store_true")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Run the HTTP API against the compose stack")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument(
        "--in-memory",
        action="store_true",
        help="Forbidden in production. Tests only: inject memory doubles.",
    )
    serve.set_defaults(func=cmd_serve)
    init = sub.add_parser("init", help="Write .env.example (and optionally .env)")
    init.add_argument("--dir", default=".")
    init.add_argument(
        "--write-env",
        action="store_true",
        help="Also write .env when it is missing. Never overwrites an existing .env.",
    )
    init.set_defaults(func=cmd_init)
    doctor = sub.add_parser("doctor", help="Check plugins and probe the durable stack")
    doctor.add_argument(
        "--skip-network",
        action="store_true",
        help="Print plugins and config warnings without connecting to stores",
    )
    doctor.add_argument("--json", action="store_true", help="Machine-readable report")
    doctor.set_defaults(func=cmd_doctor)
    plugins = sub.add_parser("plugins", help="List installed source plugins")
    plugins.add_argument("--json", action="store_true")
    plugins.set_defaults(func=cmd_plugins)
    parse = sub.add_parser("parse", help="Decode a payload with an installed plugin")
    parse.add_argument("path")
    parse.add_argument("--provider", required=True)
    parse.set_defaults(func=cmd_parse)
    record = sub.add_parser(
        "record-golden",
        help="Write fixtures/expected from a raw payload (review the diff)",
    )
    record.add_argument("path")
    record.add_argument("--provider", required=True)
    record.add_argument("--out", default=None)
    record.set_defaults(func=cmd_record_golden)
    eval_case = sub.add_parser(
        "record-eval-case",
        help="Write a redacted regression fixture from a confirmed production failure",
    )
    eval_case.add_argument("call_id")
    eval_case.add_argument("--org", required=True)
    eval_case.add_argument("--out", default=None)
    eval_case.set_defaults(func=cmd_record_eval_case)
    drift = sub.add_parser(
        "schema-drift", help="Compare a vendored plugin schema to a refetched copy"
    )
    drift.add_argument("--fixtures", default=None)
    drift.add_argument(
        "--all", action="store_true", help="Scan every first-party plugin fixtures/ directory"
    )
    drift.add_argument(
        "--remote", default=None, help="Optional JSON file of the refetched vendor schema"
    )
    drift.set_defaults(func=cmd_schema_drift)
    retain = sub.add_parser("retain", help="Sweep expired raw, transcript, and aggregate retention")
    retain.set_defaults(func=cmd_retain)
    worker = sub.add_parser(
        "worker", help="Drain the outbox. Decode never runs on the webhook path."
    )
    worker.add_argument("--poll", type=float, default=1.0, help="Idle sleep seconds")
    worker.add_argument("--once", action="store_true", help="Process one batch and exit")
    worker.set_defaults(func=cmd_worker)
    export = sub.add_parser(
        "export", help="Export active-call revisions to a Parquet/JSONL manifest"
    )
    export.add_argument("--org", required=True)
    export.add_argument("--dest", required=True)
    export.set_defaults(func=cmd_export)
    seed = sub.add_parser(
        "seed",
        help="Replay vendored provider fixtures into a running serve (dev only)",
    )
    seed.add_argument(
        "--base",
        default=None,
        help="obsalt serve origin (default http://localhost:8080)",
    )
    seed.add_argument("--key", default=None, help="X-API-Key (default OBSALT_BOOTSTRAP_API_KEY)")
    seed.add_argument("--count", type=int, default=8, help="Clones per provider template")
    seed.add_argument(
        "--providers",
        default=None,
        help="Comma-separated plugin names (default: all first-party)",
    )
    seed.add_argument("--window-days", type=int, default=7)
    seed.add_argument(
        "--include-example",
        action="store_true",
        help="Also post the example plugin fixture",
    )
    seed.add_argument("--json", action="store_true")
    seed.add_argument(
        "--dry-run",
        action="store_true",
        help="Clone and schema-validate without HTTP",
    )
    seed.set_defaults(func=cmd_seed)
    version = sub.add_parser("version")
    version.set_defaults(func=lambda _a: print(__version__) or 0)
    return parser


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    dest = root / ".env.example"
    dest.write_text(ENV_EXAMPLE)
    print(f"wrote {dest}")
    if getattr(args, "write_env", False):
        env_path = root / ".env"
        if env_path.exists():
            print(f"left existing {env_path} untouched")
        else:
            env_path.write_text(ENV_EXAMPLE)
            print(f"wrote {env_path} — replace every change-me and dev-key before a real deploy")
    else:
        print("Copy to .env, or re-run with --write-env if .env is missing.")
    print("Install a provider plugin (obsalt-vapi, obsalt-retell, …). Core ships no providers.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(probe=not getattr(args, "skip_network", False))
    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), indent=2))
    else:
        format_report(report, sys.stdout)
    return report.exit_code()


def cmd_plugins(args: argparse.Namespace) -> int:
    rows = plugin_rows()
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
        return 0 if rows else 1
    if not rows:
        print("No plugins installed. Core ships none.")
        print("Try: uv pip install obsalt-vapi   or   uv sync --all-packages")
        return 1
    width = max(len(row["name"]) for row in rows)
    for row in rows:
        caps = ", ".join(row["capabilities"]) or "(no capabilities)"
        print(f"{row['name']:<{width}}  {row['display_name']}  {caps}")
    return 0


def _require_plugin(name: str) -> Any:
    try:
        return plugin_by_name(name).plugin
    except KeyError:
        installed = ", ".join(row["name"] for row in plugin_rows()) or "(none)"
        print(f"plugin {name!r} is not installed. Installed: {installed}", file=sys.stderr)
        print(
            "Core ships no providers. pip install obsalt-vapi / obsalt-retell / …", file=sys.stderr
        )
        raise SystemExit(2) from None


def cmd_parse(args: argparse.Namespace) -> int:
    from obsalt.plugin.types import RawEnvelope
    from obsalt.util import new_id, utcnow

    raw = Path(args.path).read_bytes() if args.path != "-" else sys.stdin.buffer.read()
    plugin = _require_plugin(args.provider)
    envelope = RawEnvelope(
        envelope_id=new_id(),
        org_id="parse",
        provider=args.provider,
        connection_id="parse",
        object_key="parse",
        delivery_key="parse",
        content_sha256="parse",
        body=raw,
        received_at=utcnow(),
    )
    events = [e.model_dump(mode="json") for e in plugin.decode(envelope)]
    print(json.dumps(events, indent=2, default=str))
    return 0


def cmd_record_golden(args: argparse.Namespace) -> int:
    from obsalt_testkit import decode_raw_fixture, stable_event_dump

    raw_path = Path(args.path)
    plugin = _require_plugin(args.provider)
    events = [stable_event_dump(event) for event in decode_raw_fixture(plugin, raw_path)]
    dest = Path(args.out) if args.out else raw_path.parent.parent / "expected" / raw_path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(events, indent=2) + "\n")
    print(f"wrote {dest}")
    print("Review the golden diff before committing.")
    return 0


def cmd_record_eval_case(args: argparse.Namespace) -> int:
    from obsalt.analysis.eval_case import build_eval_case
    from obsalt.query import analysis_for

    settings = Settings()
    try:
        state = production_state(settings)
    except Exception as exc:
        print("Could not connect to the durable stack.", file=sys.stderr)
        print("Supported path: docker compose up -d && obsalt record-eval-case", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2
    rev = state.sink.get(args.org, args.call_id, state.pointers.get(args.org, args.call_id) or "")
    if rev is None or rev.org_id != args.org:
        print("call not found", file=sys.stderr)
        return 1
    case = build_eval_case(rev, analysis_for(state, args.org, rev.call_id, rev.revision))
    dest = Path(args.out) if args.out else Path(f"eval-case-{args.call_id[-8:]}.json")
    dest.write_text(json.dumps(case, indent=2) + "\n")
    print(f"wrote {dest}")
    print("Review the fixture before committing. Not a caller simulator.")
    return 0


def cmd_schema_drift(args: argparse.Namespace) -> int:
    from obsalt.ops.schema_drift import compare_all_vendored, compare_vendored

    remote = json.loads(Path(args.remote).read_text()) if args.remote else None
    if args.all or args.fixtures is None:
        report = compare_all_vendored(Path("packages"), remote)
        print(json.dumps(report, indent=2))
        return 1 if report.get("diverged") else 0
    report = compare_vendored(Path(args.fixtures), remote)
    print(json.dumps(report, indent=2))
    return 1 if report.get("diverged") else 0


def cmd_worker(args: argparse.Namespace) -> int:
    from obsalt.worker.drain import drain_once

    settings = Settings()
    try:
        state = production_state(settings)
    except Exception as exc:
        print("Could not connect to Postgres / ClickHouse / object storage.", file=sys.stderr)
        print("Supported path: docker compose up -d && obsalt worker", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2
    if args.once:
        print(drain_once(state))
        return 0
    while True:
        processed = drain_once(state)
        if processed == 0:
            time.sleep(max(0.05, float(args.poll)))


def cmd_retain(_args: argparse.Namespace) -> int:
    from obsalt.ops.retention import sweep

    settings = Settings()
    try:
        state = production_state(settings)
    except Exception as exc:
        print("Could not connect to the durable stack.", file=sys.stderr)
        print("Supported path: docker compose up -d && obsalt retain", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2
    print(json.dumps(sweep(state), indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from obsalt.ops.parquet import export_revisions
    from obsalt.query import active_calls

    settings = Settings()
    try:
        state = production_state(settings)
    except Exception as exc:
        print("Could not connect to the durable stack.", file=sys.stderr)
        print("Supported path: docker compose up -d && obsalt export", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2
    dest = Path(args.dest)
    report = export_revisions(
        active_calls(state, args.org),
        dest,
        as_of_generation=state.rollup_generation,
    )
    print(json.dumps(report, indent=2))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    import httpx

    from obsalt.ops.seed import (
        DEFAULT_BASE,
        SeedError,
        format_report,
        run_seed,
    )

    settings = Settings()
    providers = None
    if args.providers:
        providers = [part.strip() for part in str(args.providers).split(",") if part.strip()]
    base = (args.base or DEFAULT_BASE).rstrip("/")
    key = args.key or settings.bootstrap_api_key
    try:
        if args.dry_run:
            report = run_seed(
                client=None,
                api_key=key,
                count=args.count,
                providers=providers,
                window_days=args.window_days,
                include_example=args.include_example,
                dry_run=True,
                base_url=base,
            )
        else:
            with httpx.Client(base_url=base, timeout=30.0) as client:
                report = run_seed(
                    client=client,
                    api_key=key,
                    count=args.count,
                    providers=providers,
                    window_days=args.window_days,
                    include_example=args.include_example,
                    dry_run=False,
                    base_url=base,
                )
    except SeedError as exc:
        print(str(exc), file=sys.stderr)
        return int(exc.exit_code)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(format_report(report))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    settings = Settings()
    host = args.host or settings.host
    port = args.port or settings.port
    if args.in_memory:
        print("WARNING: --in-memory is a test double, not a production backend.", file=sys.stderr)
        state = in_memory_state(settings)
    else:
        try:
            state = production_state(settings)
        except Exception as exc:
            print("Could not connect to Postgres / ClickHouse / object storage.", file=sys.stderr)
            print("Supported path: docker compose up -d && obsalt serve", file=sys.stderr)
            print("There is no SQLite or Postgres-only production mode.", file=sys.stderr)
            print(exc, file=sys.stderr)
            return 2
    app = create_app(settings, state)
    uvicorn.run(app, host=host, port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if not args.command:
        parser.print_help()
        return 2
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
