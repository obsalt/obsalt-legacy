from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import uvicorn

from obsalt._version import __version__
from obsalt.api import create_app
from obsalt.config import Settings
from obsalt.plugin.host import discover_plugins, plugin_by_name
from obsalt.runtime import in_memory_state, production_state

_INIT_ENV = """# Secrets. Do not commit.
OBSALT_MASTER_KEY=change-me-master-key-not-for-production
OBSALT_SESSION_SECRET=change-me-session
OBSALT_BOOTSTRAP_API_KEY=dev-key
OBSALT_BOOTSTRAP_ORG_ID=local
OBSALT_POSTGRES_DSN=postgresql://obsalt:obsalt@localhost:5432/obsalt
OBSALT_CLICKHOUSE_URL=http://localhost:8123
OBSALT_REDIS_URL=redis://localhost:6379/0
OBSALT_S3_ENDPOINT=http://localhost:9010
OBSALT_S3_ACCESS_KEY=obsalt
OBSALT_S3_SECRET_KEY=obsalt-secret
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obsalt", description="Self-hosted call analytics for AI voice agents")
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
    init = sub.add_parser("init", help="Write .env.example")
    init.add_argument("--dir", default=".")
    init.set_defaults(func=cmd_init)
    doctor = sub.add_parser("doctor", help="Check config")
    doctor.set_defaults(func=cmd_doctor)
    demo = sub.add_parser("demo", help="Launch the ephemeral full stack (docker compose). Not for production.")
    demo.set_defaults(func=cmd_demo)
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
    drift = sub.add_parser("schema-drift", help="Compare a vendored plugin schema to a refetched copy")
    drift.add_argument("--fixtures", default=None)
    drift.add_argument("--all", action="store_true", help="Scan every first-party plugin fixtures/ directory")
    drift.add_argument("--remote", default=None, help="Optional JSON file of the refetched vendor schema")
    drift.set_defaults(func=cmd_schema_drift)
    retain = sub.add_parser("retain", help="Sweep expired raw, transcript, and aggregate retention")
    retain.set_defaults(func=cmd_retain)
    worker = sub.add_parser("worker", help="Drain the outbox. Decode never runs on the webhook path.")
    worker.add_argument("--poll", type=float, default=1.0, help="Idle sleep seconds")
    worker.add_argument("--once", action="store_true", help="Process one batch and exit")
    worker.set_defaults(func=cmd_worker)
    export = sub.add_parser("export", help="Export active-call revisions to a Parquet/JSONL manifest")
    export.add_argument("--org", required=True)
    export.add_argument("--dest", required=True)
    export.set_defaults(func=cmd_export)
    version = sub.add_parser("version")
    version.set_defaults(func=lambda _a: (print(__version__) or 0))
    return parser


def cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.dir) / ".env.example"
    dest.write_text(_INIT_ENV)
    print(f"wrote {dest}")
    print("Install a provider plugin (obsalt-vapi, obsalt-retell, …). Core ships no providers.")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    settings = Settings()
    plugins = discover_plugins()
    print(f"obsalt {__version__}")
    print(f"plugins: {', '.join(p.name for p in plugins) or '(none installed)'}")
    print(f"postgres: {settings.postgres_dsn}")
    print(f"clickhouse: {settings.clickhouse_url}")
    print(f"object store: {settings.s3_endpoint}/{settings.s3_bucket}")
    if settings.insecure_defaults():
        print("WARNING: default master/session keys are in use")
    print("require_auth=false has been removed. Every key is hashed and org-bound.")
    if settings.bootstrap_api_key == "dev-key":
        print("WARNING: OBSALT_BOOTSTRAP_API_KEY is the default dev-key")
    print(f"raw retention days: {settings.raw_retention_days}")
    print("Supported path: docker compose up -d && obsalt serve")
    return 0


def _compose_file() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "docker-compose.yml"
        if candidate.is_file():
            return candidate
    return Path("docker-compose.yml")


def cmd_demo(_args: argparse.Namespace) -> int:
    compose = _compose_file()
    if not compose.exists():
        print("docker-compose.yml not found", file=sys.stderr)
        return 2
    print("NOT FOR PRODUCTION. Data is not durable beyond this compose project.")
    rc = subprocess.call(["docker", "compose", "-f", str(compose), "up", "-d"])
    if rc != 0:
        return rc
    settings = Settings()
    for _ in range(30):
        try:
            state = production_state(settings)
            break
        except Exception:
            time.sleep(1)
    else:
        print("compose services did not become ready", file=sys.stderr)
        return 2
    host = settings.host
    port = settings.port
    app = create_app(settings, state)
    uvicorn.run(app, host=host, port=port)
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    from obsalt.plugin.types import RawEnvelope
    from obsalt.util import new_id, utcnow

    raw = Path(args.path).read_bytes() if args.path != "-" else sys.stdin.buffer.read()
    plugin = plugin_by_name(args.provider).plugin
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
    plugin = plugin_by_name(args.provider).plugin
    events = [stable_event_dump(event) for event in decode_raw_fixture(plugin, raw_path)]
    dest = Path(args.out) if args.out else raw_path.parent.parent / "expected" / raw_path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(events, indent=2) + "\n")
    print(f"wrote {dest}")
    print("Review the golden diff before committing.")
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
    except Exception:
        state = in_memory_state(settings)
    print(json.dumps(sweep(state), indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from obsalt.ops.parquet import export_revisions
    from obsalt.query import active_calls

    settings = Settings()
    try:
        state = production_state(settings)
    except Exception:
        state = in_memory_state(settings)
    dest = Path(args.dest)
    report = export_revisions(
        active_calls(state, args.org),
        dest,
        as_of_generation=state.rollup_generation,
    )
    print(json.dumps(report, indent=2))
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
