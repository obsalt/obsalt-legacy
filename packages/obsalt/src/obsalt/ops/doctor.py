"""Lightweight connectivity and install checks. Not a substitute for GET /ready."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TextIO

from obsalt._version import __version__
from obsalt.config import Settings
from obsalt.plugin.host import LoadedPlugin, discover_plugins

_COMPOSE_HINT = "Start the durable stack with: docker compose up -d"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    required: bool
    detail: str
    hint: str = ""


@dataclass
class DoctorReport:
    version: str
    plugins: list[dict[str, Any]]
    checks: list[Check]
    insecure_defaults: bool
    notes: list[str] = field(default_factory=list)

    @property
    def required_ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    def exit_code(self) -> int:
        if not self.required_ok:
            return 2
        if not self.plugins:
            return 1
        return 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "plugins": self.plugins,
            "insecure_defaults": self.insecure_defaults,
            "checks": [
                {
                    "name": check.name,
                    "ok": check.ok,
                    "required": check.required,
                    "detail": check.detail,
                    "hint": check.hint,
                }
                for check in self.checks
            ],
            "notes": self.notes,
            "ok": self.required_ok,
        }


def plugin_rows(plugins: list[LoadedPlugin] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for plugin in plugins if plugins is not None else discover_plugins():
        rows.append(
            {
                "name": plugin.name,
                "display_name": plugin.display_name,
                "capabilities": sorted(capability.value for capability in plugin.capabilities),
                "source_format": plugin.fidelity.source_format,
            }
        )
    return rows


def probe_postgres(settings: Settings) -> None:
    from psycopg import Connection
    from psycopg.rows import dict_row

    conn = Connection.connect(settings.postgres_dsn, row_factory=dict_row, connect_timeout=2)
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


def probe_clickhouse(settings: Settings) -> None:
    from obsalt.store.clickhouse import ClickHouseSink

    ClickHouseSink(settings).ping()


def probe_redis(settings: Settings) -> None:
    from redis import Redis

    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        client.ping()
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
        else:
            client.connection_pool.disconnect()


def probe_object_store(settings: Settings) -> None:
    from obsalt.store.objects import S3ObjectStore

    S3ObjectStore(settings).ping()


def _run_probe(name: str, required: bool, fn: Any, settings: Settings, hint: str) -> Check:
    try:
        fn(settings)
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        return Check(name=name, ok=False, required=required, detail=detail, hint=hint)
    return Check(name=name, ok=True, required=required, detail="ok")


def run_doctor(
    settings: Settings | None = None,
    *,
    probe: bool = True,
    plugins: list[LoadedPlugin] | None = None,
) -> DoctorReport:
    settings = settings or Settings()
    loaded = plugins if plugins is not None else discover_plugins()
    rows = plugin_rows(loaded)
    notes: list[str] = []
    if settings.insecure_defaults():
        notes.append(
            "Default master key, session secret, or bootstrap API key is in use. "
            "Fine on localhost. Change them before the process is reachable from a "
            "network you do not trust."
        )
    if not rows:
        notes.append(
            "No plugins loaded. Core ships none. Install obsalt-vapi, obsalt-retell, "
            "or another package that registers on the obsalt.plugins entry point."
        )
    notes.append("require_auth=false does not exist. Every key is hashed and org-bound.")
    notes.append(f"raw retention days: {settings.raw_retention_days}")
    notes.append("Supported path: docker compose up -d && obsalt serve && obsalt worker")

    checks: list[Check] = []
    if probe:
        checks.append(_run_probe("postgres", True, probe_postgres, settings, _COMPOSE_HINT))
        checks.append(_run_probe("clickhouse", True, probe_clickhouse, settings, _COMPOSE_HINT))
        checks.append(_run_probe("object_store", True, probe_object_store, settings, _COMPOSE_HINT))
        checks.append(
            _run_probe(
                "redis",
                False,
                probe_redis,
                settings,
                "Redis is a lease accelerator. If it is down, workers still claim from Postgres.",
            )
        )
    else:
        for name, required in (
            ("postgres", True),
            ("clickhouse", True),
            ("object_store", True),
            ("redis", False),
        ):
            checks.append(
                Check(name=name, ok=True, required=required, detail="skipped (no network probe)")
            )

    return DoctorReport(
        version=__version__,
        plugins=rows,
        checks=checks,
        insecure_defaults=settings.insecure_defaults(),
        notes=notes,
    )


def format_report(report: DoctorReport, file: TextIO) -> None:
    file.write(f"obsalt {report.version}\n")
    file.write("\nplugins\n")
    if report.plugins:
        width = max(len(row["name"]) for row in report.plugins)
        for row in report.plugins:
            caps = ", ".join(row["capabilities"]) or "(no capabilities)"
            file.write(f"  {row['name']:<{width}}  {caps}\n")
    else:
        file.write("  (none installed)\n")

    file.write("\nstores\n")
    for check in report.checks:
        status = "ok" if check.ok else "FAIL"
        suffix = "" if check.required else " (optional)"
        file.write(f"  {check.name:<14} {status}{suffix}")
        if check.detail and check.detail not in {"ok"}:
            file.write(f"  {check.detail}")
        file.write("\n")
        if not check.ok and check.hint:
            file.write(f"                 hint: {check.hint}\n")

    if report.notes:
        file.write("\nnotes\n")
        for note in report.notes:
            file.write(f"  {note}\n")
