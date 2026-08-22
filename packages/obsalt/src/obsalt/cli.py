from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import uvicorn

from obsalt._version import __version__
from obsalt.api import AppState, create_app
from obsalt.assemble.promote import MemoryPointerStore
from obsalt.config import Settings
from obsalt.domain.enums import KeyScope
from obsalt.plugin.host import discover_plugins
from obsalt.plugin.types import ConnectionConfig
from obsalt.testing.fakes import MemoryInbox, MemoryObjectStore, MemoryResolver
from obsalt.worker.process import MemoryRevisionSink

_INIT_ENV = """# Secrets. Do not commit.
OBSALT_MASTER_KEY=change-me-master-key-not-for-production
OBSALT_SESSION_SECRET=change-me-session
OBSALT_POSTGRES_DSN=postgresql://obsalt:obsalt@localhost:5432/obsalt
OBSALT_CLICKHOUSE_URL=http://localhost:8123
OBSALT_REDIS_URL=redis://localhost:6379/0
OBSALT_S3_ENDPOINT=http://localhost:9000
OBSALT_S3_ACCESS_KEY=obsalt
OBSALT_S3_SECRET_KEY=obsalt-secret
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obsalt", description="Self-hosted call analytics for AI voice agents")
    parser.add_argument("-V", "--version", action="store_true")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Run the HTTP API")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
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
    if settings.insecure_defaults():
        print("WARNING: default master/session keys are in use")
    print("require_auth=false has been removed. Every key is hashed and org-bound.")
    print(f"raw retention days: {settings.raw_retention_days}")
    return 0


def cmd_demo(_args: argparse.Namespace) -> int:
    here = Path(__file__).resolve()
    compose = None
    for parent in here.parents:
        candidate = parent / "docker-compose.yml"
        if candidate.is_file():
            compose = candidate
            break
    if compose is None:
        compose = Path("docker-compose.yml")
    if not compose.exists():
        compose = Path.cwd() / "docker-compose.yml"
    print("NOT FOR PRODUCTION. Data is not durable beyond this compose project.")
    return subprocess.call(["docker", "compose", "-f", str(compose), "up"])


def cmd_parse(args: argparse.Namespace) -> int:
    from obsalt.plugin.host import plugin_by_name
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


def cmd_serve(args: argparse.Namespace) -> int:
    settings = Settings()
    host = args.host or settings.host
    port = args.port or settings.port
    app = create_app(settings, _dev_state(settings))
    uvicorn.run(app, host=host, port=port)
    return 0


def _dev_state(settings: Settings) -> AppState:
    """In-process wiring used when compose services are not injected.

    Production docker-compose replaces these with Postgres, ClickHouse, MinIO, and Redis.
    This is not a supported production backend.
    """
    plugins = discover_plugins()
    resolver = MemoryResolver()
    # Bootstrap a demo connection per installed webhook plugin so local ingest works.
    for plugin in plugins:
        if "webhook_source" not in {c.value for c in plugin.capabilities}:
            continue
        secrets = {name: "dev-secret" for name in plugin.manifest.secret_fields} or {"hmac_secret": "dev-secret"}
        cfg = ConnectionConfig(
            org_id="dev",
            provider=plugin.name,
            connection_id=f"{plugin.name}-dev",
            ingest_key_hash="",
            secrets=secrets,
            settings={"auth_mode": "legacy_secret"} if plugin.name == "vapi" else {},
        )
        resolver.add(cfg, "dev")
        if plugin.name == "vapi":
            cfg.secrets["legacy_secret"] = "dev-secret"
        if plugin.name == "retell":
            cfg.secrets["api_key"] = "dev-secret"
    state = AppState(
        settings=settings,
        plugins=plugins,
        resolver=resolver,
        objects=MemoryObjectStore(),
        inbox=MemoryInbox(),
        pointers=MemoryPointerStore(),
        sink=MemoryRevisionSink(),
        keys={"dev-key": ("dev", frozenset(KeyScope))},
    )
    return state


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
