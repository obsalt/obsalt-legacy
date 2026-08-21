from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import uvicorn

from obsalt._version import __version__
from obsalt.adapters.registry import AdapterRegistry, detect_provider
from obsalt.config import Settings, resolve_config_path
from obsalt.domain.enums import parse_provider
from obsalt.pipeline import IngestPipeline

_INIT_TOML = """# obsalt.toml — copy-local config. Secrets can live here or in .env / OBSALT_* vars.
# Env vars always win. Run `obsalt doctor` after you edit this file.

[server]
host = "0.0.0.0"
port = 8080

[auth]
# org:secret pairs. The secret is what you send as X-API-Key.
api_keys = "acme:change-me"
# Set true before exposing the port. Local demos can leave this false.
require_auth = false

[export]
# OTLP HTTP base (obsalt appends /v1/traces and /v1/metrics).
# grafana/otel-lgtm listens on 4318. Omit to store evidence without exporting traces.
otlp_endpoint = "http://localhost:4318"
environment = "dev"
service_name = "obsalt"

[webhooks]
# Empty = signature check skipped (local only).
vapi_secret = ""
retell_secret = ""
bland_secret = ""
"""

_INIT_ENV = """# .env — loaded automatically from the working directory.
# Preferred place for secrets. Do not commit this file.

OBSALT_API_KEYS=acme:change-me
OBSALT_REQUIRE_AUTH=false
OBSALT_OTLP_ENDPOINT=http://localhost:4318
# OBSALT_VAPI_SECRET=
# OBSALT_RETELL_SECRET=
# OBSALT_BLAND_SECRET=
# OBSALT_ENVIRONMENT=dev
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsalt",
        description=(
            "Observability for voice AI agents — ingest server, "
            "webhook parser, and config tools."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  obsalt init\n"
            "  obsalt doctor\n"
            "  obsalt serve --port 8080\n"
            "  obsalt parse tests/fixtures/vapi_end_of_call.json\n"
            "  obsalt --port 8080          # same as serve (compat)\n"
        ),
    )
    parser.add_argument(
        "--version",
        "-V",
        action="store_true",
        help="Print the package version and exit",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the ingest & evidence HTTP API")
    serve.add_argument("-c", "--config", help="Path to obsalt.toml (or set OBSALT_CONFIG)")
    serve.add_argument("--host", default=None, help="Bind address (default: settings / 0.0.0.0)")
    serve.add_argument("--port", type=int, default=None, help="Bind port (default: 8080)")
    serve.add_argument("--reload", action="store_true", help="Reload on code changes (local only)")
    serve.set_defaults(func=cmd_serve)

    init = sub.add_parser("init", help="Write obsalt.toml and .env.example")
    init.add_argument("--force", action="store_true", help="Overwrite existing files")
    init.add_argument("--dir", default=".", help="Directory to write into (default: cwd)")
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="Validate config, keys, and OTLP reachability")
    doctor.add_argument("-c", "--config", help="Path to obsalt.toml")
    doctor.set_defaults(func=cmd_doctor)

    parse = sub.add_parser("parse", help="Normalize a provider webhook without running the server")
    parse.add_argument("path", help="JSON file, or - for stdin")
    parse.add_argument(
        "--provider",
        default=None,
        help="vapi | retell | bland | openai-realtime | native (auto-detected)",
    )
    parse.add_argument(
        "--org",
        default=None,
        help="Tenant id for the canonical call id (default: first configured org)",
    )
    parse.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the full CanonicalCall as JSON",
    )
    parse.add_argument("-c", "--config", help="Path to obsalt.toml")
    parse.set_defaults(func=cmd_parse)

    version = sub.add_parser("version", help="Print the package version")
    version.set_defaults(func=cmd_version)
    return parser


def _apply_config_path(path: str | None) -> None:
    if path:
        os.environ["OBSALT_CONFIG"] = path


def cmd_version(_args: argparse.Namespace | None = None) -> int:
    print(__version__)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    wrote: list[str] = []
    skipped: list[str] = []
    mapping = {
        "obsalt.toml": _INIT_TOML,
        ".env.example": _INIT_ENV,
    }
    for name, body in mapping.items():
        dest = root / name
        if dest.exists() and not args.force:
            skipped.append(str(dest))
            continue
        dest.write_text(body, encoding="utf-8")
        wrote.append(str(dest))
    for path in wrote:
        print(f"wrote   {path}")
    for path in skipped:
        print(f"skipped {path} (exists; pass --force to overwrite)")
    print()
    print("Local OpenTelemetry backend (Grafana LGTM):")
    print("  docker run --rm -p 3000:3000 -p 4317:4317 -p 4318:4318 grafana/otel-lgtm")
    print()
    print("Then:")
    print("  obsalt doctor")
    print("  obsalt serve")
    print("Docs: https://github.com/coder-with-a-bushido/obsalt/blob/main/docs/getting-started.md")
    return 0


def _check_otlp(endpoint: str, timeout: float = 1.5) -> tuple[str, str]:
    if not endpoint:
        return "warn", "not set — traces stay in-process and are discarded"
    url = endpoint.rstrip("/") + "/v1/traces"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=timeout) as response:  # noqa: S310 — operator-supplied OTLP URL
            return "ok", f"reachable ({url}, HTTP {response.status})"
    except HTTPError as exc:
        return "ok", f"reachable ({url}, HTTP {exc.code})"
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        return "warn", f"not reachable at {url} ({reason})"
    except Exception as exc:  # noqa: BLE001 — doctor should never crash
        return "warn", f"not reachable at {url} ({exc})"


def cmd_doctor(args: argparse.Namespace) -> int:
    _apply_config_path(getattr(args, "config", None))
    settings = Settings()
    config_path = resolve_config_path()
    lines: list[tuple[str, str, str]] = []
    fatal = False

    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    lines.append(("ok", "python", py))

    lines.append(("ok", "package", f"obsalt {__version__}"))

    if getattr(args, "config", None) and not Path(args.config).expanduser().is_file():
        lines.append(("fail", "config", f"{args.config} not found"))
        fatal = True
    elif config_path:
        lines.append(("ok", "config", str(config_path)))
    else:
        lines.append(("info", "config", "no obsalt.toml (using env / defaults)"))

    env_file = Path(".env")
    if env_file.is_file():
        lines.append(("ok", "dotenv", str(env_file.resolve())))
    else:
        lines.append(("info", "dotenv", "no .env in cwd"))

    keys = settings.parsed_api_keys()
    orgs = settings.org_ids()
    if not keys:
        lines.append(
            (
                "fail",
                "keys",
                "OBSALT_API_KEYS parsed to nothing — expected org:secret[,org2:secret2]",
            )
        )
        fatal = True
    else:
        lines.append(("ok", "keys", f"{len(orgs)} org(s): {', '.join(orgs)}"))

    for chunk in settings.malformed_api_key_chunks():
        lines.append(("fail", "keys", f"ignored malformed chunk {chunk!r}"))
        fatal = True
    for dupe in settings.duplicate_secret_orgs():
        lines.append(("fail", "keys", dupe))
        fatal = True

    if settings.require_auth:
        lines.append(("ok", "auth", "OBSALT_REQUIRE_AUTH=true — unauthenticated requests rejected"))
    else:
        lines.append(
            (
                "warn",
                "auth",
                "OBSALT_REQUIRE_AUTH is false — anyone who can reach the port can ingest",
            )
        )

    if "demo-secret" in keys:
        lines.append(("warn", "auth", "default secret demo-secret is still configured"))

    for name, present in (
        ("vapi HMAC", settings.vapi_secret),
        ("retell HMAC", settings.retell_secret),
        ("bland HMAC", settings.bland_secret),
    ):
        if present:
            lines.append(("ok", "webhook", f"{name} configured"))
        else:
            lines.append(("info", "webhook", f"{name} empty — signature check skipped"))

    status, detail = _check_otlp(settings.otlp_endpoint)
    otlp_msg = (
        detail
        if settings.otlp_endpoint
        else "not set — evidence is stored; traces are not exported"
    )
    lines.append((status, "otlp", otlp_msg))

    lines.append(("info", "store", "in-memory — a process restart loses calls"))
    lines.append(("info", "env", settings.environment))

    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL", "info": " ·  "}
    print(f"obsalt doctor  ({__version__})")
    print()
    for level, name, detail in lines:
        print(f"  {marks[level]}  {name:<10} {detail}")
    print()
    if fatal:
        print("Fix the FAIL lines, then re-run obsalt doctor.")
        return 1
    if any(level == "warn" for level, _, _ in lines):
        print("Ready for local use. Treat WARN lines before production.")
        return 0
    print("Looks good. obsalt serve")
    return 0


def _load_payload(path: str) -> dict[str, Any]:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data


def _pretty_call(call: Any, result: Any) -> str:
    hangup = ""
    if call.hangup:
        hangup = (
            f"{call.hangup.reason.value} ({call.hangup.party.value})  "
            f"loss={call.hangup.loss_score}"
        )
    tools = ", ".join(f"{t.name}:{t.status.value}" for t in call.tools) or "(none)"
    hallu = ", ".join(h.kind.value for h in call.hallucinations) or "(none)"
    evals = ", ".join(
        f"{e.rubric_name} {'PASS' if e.passed else 'FAIL'} {e.score:.2f}" for e in call.evals
    ) or "(none)"
    rows = [
        ("status", result.status),
        ("obsalt_id", call.id),
        ("provider", call.provider.value),
        ("provider_id", call.provider_call_id),
        ("agent", call.agent_id),
        ("duration_ms", call.duration_ms),
        ("turns", len(call.turns)),
        ("tools", tools),
        ("hangup", hangup or "(none)"),
        ("hallucinations", f"{len(call.hallucinations)}  {hallu}"),
        ("evals", evals),
        ("recording", call.recording_url or "(none)"),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"  {key:<{width}}  {value}" for key, value in rows)


def cmd_parse(args: argparse.Namespace) -> int:
    _apply_config_path(getattr(args, "config", None))
    settings = Settings()
    try:
        payload = _load_payload(args.path)
    except FileNotFoundError:
        print(f"obsalt: file not found: {args.path}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"obsalt: invalid JSON: {exc}", file=sys.stderr)
        return 1

    try:
        if args.provider:
            provider = parse_provider(args.provider)
        else:
            provider = detect_provider(payload)
            if provider is None:
                print(
                    "obsalt: could not detect provider. "
                    "Pass --provider vapi|retell|bland|openai-realtime|native",
                    file=sys.stderr,
                )
                return 1
    except ValueError as exc:
        print(f"obsalt: {exc}", file=sys.stderr)
        return 1

    org = args.org or (settings.org_ids()[0] if settings.org_ids() else "demo")
    pipeline = IngestPipeline()
    try:
        result = pipeline.ingest(provider, payload, org_id=org)
    except ValueError as exc:
        print(f"obsalt: {exc}", file=sys.stderr)
        return 1
    call = pipeline.store.get_call(org, result.call_id)
    if call is None:
        parsed = AdapterRegistry().parse(provider, payload, org_id=org)
        if parsed is None:
            print("obsalt: payload is missing a call id", file=sys.stderr)
            return 1
        call = parsed.call
    if args.as_json:
        print(json.dumps(call.model_dump(mode="json"), indent=2, default=str))
        return 0
    detected = "" if args.provider else f"  (detected {provider.value})"
    print(f"obsalt parse  {provider.value}{detected}")
    print(_pretty_call(call, result))
    return 0


def print_banner(settings: Settings, host: str, port: int) -> None:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    base = f"http://{display_host}:{port}"
    otlp = settings.otlp_endpoint or "not set (evidence only; no trace export)"
    orgs = ", ".join(settings.org_ids()) or "demo"
    auth = (
        "required"
        if settings.require_auth
        else "open — set OBSALT_REQUIRE_AUTH=true before production"
    )
    lines = [
        f"obsalt {__version__}  ingest & evidence API",
        f"  listen     {base}",
        f"  openapi    {base}/docs",
        f"  health     {base}/health",
        f"  otlp       {otlp}",
        f"  auth       {auth}",
        f"  orgs       {orgs}",
        f"  env        {settings.environment}",
        "  store      in-memory (calls vanish on restart)",
    ]
    print("\n".join(lines), flush=True)


def cmd_serve(args: argparse.Namespace) -> int:
    _apply_config_path(getattr(args, "config", None))
    settings = Settings()
    host = args.host or settings.host
    port = args.port if args.port is not None else settings.port
    print_banner(settings, host, port)
    uvicorn.run(
        "obsalt.api:create_app",
        factory=True,
        host=host,
        port=port,
        reload=bool(getattr(args, "reload", False)),
    )
    return 0


def _normalize_argv(argv: Sequence[str] | None) -> list[str]:
    args = list(argv) if argv is not None else sys.argv[1:]
    if args and args[0] in {"-h", "--help"}:
        return args
    if args and args[0] in {"--version", "-V"}:
        return args
    commands = {"serve", "init", "doctor", "parse", "version"}
    if not args or args[0].startswith("-"):
        return ["serve", *args]
    if args[0] not in commands:
        return ["serve", *args]
    return args


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(argv))
    if getattr(args, "version", False) and args.command is None:
        return cmd_version(args)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    return int(func(args))


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
