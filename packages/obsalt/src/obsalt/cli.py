from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from obsalt._version import __version__
from obsalt.config import Settings
from obsalt.plugin.host import PluginHost
from obsalt.plugin.protocol import RawEnvelope
from obsalt.runtime import Runtime
from obsalt.workers.decode import decode_envelope

_INIT_TOML = """# obsalt.toml
[server]
host = "0.0.0.0"
port = 8080

[storage]
postgres_dsn = "postgresql://obsalt:obsalt@127.0.0.1:5432/obsalt"
clickhouse_url = "http://127.0.0.1:8123"
redis_url = "redis://127.0.0.1:6379/0"
s3_endpoint = "http://127.0.0.1:9000"

[retention]
raw_days = 30
evidence_days = 90
aggregate_days = 400
"""

_INIT_ENV = """OBSALT_MASTER_KEY=
OBSALT_POSTGRES_DSN=postgresql://obsalt:obsalt@127.0.0.1:5432/obsalt
OBSALT_CLICKHOUSE_URL=http://127.0.0.1:8123
OBSALT_REDIS_URL=redis://127.0.0.1:6379/0
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obsalt", description="obsalt — voice call analytics")
    parser.add_argument("--version", "-V", action="store_true")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the HTTP API")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    demo = sub.add_parser("demo", help="Ephemeral full stack (not for production)")
    demo.set_defaults(func=cmd_demo)

    init = sub.add_parser("init")
    init.add_argument("--force", action="store_true")
    init.add_argument("--dir", default=".")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor")
    doctor.set_defaults(func=cmd_doctor)

    plugins = sub.add_parser("plugins")
    plugins.set_defaults(func=cmd_plugins)

    worker = sub.add_parser("worker", help="Lease outbox work and decode/assemble")
    worker.set_defaults(func=cmd_worker)

    parse = sub.add_parser("parse")
    parse.add_argument("path")
    parse.add_argument("--provider", required=True)
    parse.add_argument("--org", default="demo")
    parse.set_defaults(func=cmd_parse)

    version = sub.add_parser("version")
    version.set_defaults(func=cmd_version)
    return parser


def cmd_version(_args: argparse.Namespace | None = None) -> int:
    print(__version__)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name, body in {"obsalt.toml": _INIT_TOML, ".env.example": _INIT_ENV}.items():
        dest = root / name
        if dest.exists() and not args.force:
            print(f"skipped {dest}")
            continue
        dest.write_text(body, encoding="utf-8")
        print(f"wrote {dest}")
    print("Supported path: docker compose up")
    print("There is no Postgres-only production mode.")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    settings = Settings()
    host = PluginHost.load()
    print(f"obsalt doctor ({__version__})")
    print(f"  plugins   {', '.join(host.plugins) or '(none installed)'}")
    for err in host.errors:
        print(f"  FAIL      {err}")
    print(f"  postgres  {settings.postgres_dsn}")
    print(f"  clickhouse {settings.clickhouse_url}")
    print("  auth      require_auth=false has been deleted; every key is org-bound")
    print("  plugins   trusted operator-installed code; not a sandbox")
    return 1 if host.errors else 0


def cmd_plugins(_args: argparse.Namespace) -> int:
    host = PluginHost.load()
    print(json.dumps({"items": host.inventory(), "errors": host.errors}, indent=2))
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    raw = Path(args.path).read_bytes() if args.path != "-" else sys.stdin.buffer.read()
    host = PluginHost.load()
    plugin = host.webhook(args.provider)
    envelope = RawEnvelope(
        envelope_id="parse",
        org_id=args.org,
        provider=args.provider,
        connection_id="parse",
        object_key="parse",
        body=raw,
        delivery_key="parse",
        received_at="1970-01-01T00:00:00+00:00",
    )
    revision = decode_envelope(envelope, plugin=plugin, declaration=host.get(args.provider).fidelity)
    print(revision.model_dump_json(indent=2))
    return 0


def cmd_demo(_args: argparse.Namespace) -> int:
    print("obsalt demo — NOT FOR PRODUCTION, data is not durable")
    print("Starting in-process stack. Use `docker compose up` for the supported path.")
    os.environ.setdefault("OBSALT_DEMO", "true")
    settings = Settings(demo=True)
    runtime = Runtime.create(settings)
    creds = runtime.bootstrap_dev_org("demo")
    print(f"  api_key     {creds['api_key']}")
    print(f"  ingest_key  {creds['ingest_key']}")
    print("  banner      NOT FOR PRODUCTION")
    uvicorn.run(_app(runtime, settings), host=settings.host, port=settings.port)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    settings = Settings()
    host = args.host or settings.host
    port = args.port if args.port is not None else settings.port
    print(f"obsalt {__version__}")
    print(f"  listen  http://{host}:{port}")
    print("  store   postgres + clickhouse + object storage (compose is the supported path)")
    try:
        runtime = Runtime.create_durable(settings)
    except Exception as exc:
        print(f"  durable store unavailable: {exc}")
        print("  Use `docker compose up` or `obsalt demo` (not for production).")
        print("  There is no Postgres-only or SQLite production mode.")
        return 1
    uvicorn.run(_app(runtime, settings), host=host, port=port)
    return 0


def cmd_worker(_args: argparse.Namespace) -> int:
    settings = Settings()
    try:
        runtime = Runtime.create_durable(settings)
        print("obsalt worker — durable spine")
    except Exception as exc:
        print(f"durable store unavailable ({exc}); worker refusing to run against memory")
        return 1
    import time

    while True:
        processed = runtime.drain_outbox()
        time.sleep(0.25 if processed == 0 else 0)


def _app(runtime: Runtime, settings: Settings):
    from obsalt.api.app import create_app

    return create_app(runtime, settings)


def _normalize(argv: Sequence[str] | None) -> list[str]:
    args = list(argv) if argv is not None else sys.argv[1:]
    if args and args[0] in {"-h", "--help", "--version", "-V"}:
        return args
    commands = {"serve", "demo", "init", "doctor", "plugins", "parse", "version", "worker"}
    if not args or args[0].startswith("-"):
        return ["serve", *args]
    if args[0] not in commands:
        return ["serve", *args]
    return args


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize(argv))
    if getattr(args, "version", False) and args.command is None:
        return cmd_version(args)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    return int(func(args))


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))
