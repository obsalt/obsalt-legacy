"""Doctor reports plugins and store probes without falling back to memory stores."""

from __future__ import annotations

import io
import json
from argparse import Namespace

from obsalt.config import Settings
from obsalt.ops.doctor import Check, DoctorReport, format_report, run_doctor


def test_run_doctor_skip_network_lists_plugins() -> None:
    from obsalt.plugin.host import LoadedPlugin
    from obsalt_example.plugin import ExamplePlugin

    report = run_doctor(Settings(), probe=False, plugins=[LoadedPlugin(ExamplePlugin())])
    assert report.required_ok
    assert report.plugins[0]["name"] == "example"
    assert all(check.detail.startswith("skipped") for check in report.checks)
    assert report.exit_code() == 0


def test_run_doctor_required_store_failure(monkeypatch) -> None:
    def _boom(_settings: Settings) -> None:
        raise RuntimeError("postgres refused")

    monkeypatch.setattr("obsalt.ops.doctor.probe_postgres", _boom)
    monkeypatch.setattr("obsalt.ops.doctor.probe_clickhouse", lambda _s: None)
    monkeypatch.setattr("obsalt.ops.doctor.probe_object_store", lambda _s: None)
    monkeypatch.setattr("obsalt.ops.doctor.probe_redis", lambda _s: None)
    report = run_doctor(Settings(), probe=True)
    assert report.required_ok is False
    assert report.exit_code() == 2
    failed = next(check for check in report.checks if check.name == "postgres")
    assert failed.ok is False
    assert "refused" in failed.detail


def test_run_doctor_optional_redis_failure_is_not_fatal(monkeypatch) -> None:
    monkeypatch.setattr("obsalt.ops.doctor.probe_postgres", lambda _s: None)
    monkeypatch.setattr("obsalt.ops.doctor.probe_clickhouse", lambda _s: None)
    monkeypatch.setattr("obsalt.ops.doctor.probe_object_store", lambda _s: None)

    def _boom(_settings: Settings) -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr("obsalt.ops.doctor.probe_redis", _boom)
    report = run_doctor(Settings(), probe=True)
    assert report.required_ok is True
    redis = next(check for check in report.checks if check.name == "redis")
    assert redis.ok is False
    assert redis.required is False


def test_doctor_cli_json(monkeypatch, capsys) -> None:
    from obsalt.cli import cmd_doctor

    monkeypatch.setattr(
        "obsalt.cli.run_doctor",
        lambda probe=True: DoctorReport(
            version="2.0.0",
            plugins=[
                {
                    "name": "example",
                    "display_name": "Example",
                    "capabilities": [],
                    "source_format": "x",
                }
            ],
            checks=[Check("postgres", True, True, "ok")],
            insecure_defaults=True,
        ),
    )
    assert cmd_doctor(Namespace(skip_network=True, json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["plugins"][0]["name"] == "example"


def test_format_report_mentions_hint() -> None:
    report = DoctorReport(
        version="2.0.0",
        plugins=[],
        checks=[Check("postgres", False, True, "connection refused", "docker compose up -d")],
        insecure_defaults=True,
        notes=["no plugins"],
    )
    buf = io.StringIO()
    format_report(report, buf)
    text = buf.getvalue()
    assert "FAIL" in text
    assert "docker compose up -d" in text
    assert "(none installed)" in text
    assert report.exit_code() == 2
