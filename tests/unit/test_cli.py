"""CLI retain/export must not fall back to in-memory stores."""

from __future__ import annotations

import argparse

from obsalt.cli import cmd_export, cmd_retain


def test_retain_fails_when_durable_stack_is_down(monkeypatch, tmp_path) -> None:
    def _boom(_settings):
        raise RuntimeError("postgres refused")

    monkeypatch.setattr("obsalt.cli.production_state", _boom)
    assert cmd_retain(argparse.Namespace()) == 2
    assert cmd_export(argparse.Namespace(org="acme", dest=str(tmp_path / "out"))) == 2
