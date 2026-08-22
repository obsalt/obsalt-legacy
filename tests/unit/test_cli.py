from __future__ import annotations

from obsalt.cli import run


def test_version() -> None:
    assert run(["version"]) == 0


def test_plugins() -> None:
    assert run(["plugins"]) == 0
