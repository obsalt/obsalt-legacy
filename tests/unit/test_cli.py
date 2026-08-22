"""CLI retain/export must not fall back to in-memory stores."""

from __future__ import annotations

import argparse
import json

from obsalt.cli import build_parser, cmd_export, cmd_init, cmd_plugins, cmd_retain
from obsalt.config import ENV_EXAMPLE


def test_retain_fails_when_durable_stack_is_down(monkeypatch, tmp_path) -> None:
    def _boom(_settings):
        raise RuntimeError("postgres refused")

    monkeypatch.setattr("obsalt.cli.production_state", _boom)
    assert cmd_retain(argparse.Namespace()) == 2
    assert cmd_export(argparse.Namespace(org="acme", dest=str(tmp_path / "out"))) == 2


def test_init_writes_example_and_optional_env(tmp_path) -> None:
    assert cmd_init(argparse.Namespace(dir=str(tmp_path), write_env=False)) == 0
    example = (tmp_path / ".env.example").read_text()
    assert example == ENV_EXAMPLE
    assert not (tmp_path / ".env").exists()

    assert cmd_init(argparse.Namespace(dir=str(tmp_path), write_env=True)) == 0
    assert (tmp_path / ".env").read_text() == ENV_EXAMPLE
    (tmp_path / ".env").write_text("keep\n")
    assert cmd_init(argparse.Namespace(dir=str(tmp_path), write_env=True)) == 0
    assert (tmp_path / ".env").read_text() == "keep\n"


def test_plugins_lists_installed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "obsalt.cli.plugin_rows",
        lambda: [
            {
                "name": "example",
                "display_name": "Example",
                "capabilities": ["webhook_source"],
                "source_format": "example.v1",
            }
        ],
    )
    assert cmd_plugins(argparse.Namespace(json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "example"


def test_plugins_empty_is_nonzero(monkeypatch, capsys) -> None:
    monkeypatch.setattr("obsalt.cli.plugin_rows", lambda: [])
    assert cmd_plugins(argparse.Namespace(json=False)) == 1
    assert "No plugins" in capsys.readouterr().out


def test_parser_exposes_doctor_and_plugins() -> None:
    parser = build_parser()
    doctor = parser.parse_args(["doctor", "--skip-network", "--json"])
    assert doctor.command == "doctor"
    assert doctor.skip_network is True
    plugins = parser.parse_args(["plugins", "--json"])
    assert plugins.command == "plugins"
