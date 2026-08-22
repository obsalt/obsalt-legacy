"""`.env.example` must not drift from Settings fields."""

from __future__ import annotations

from pathlib import Path

from obsalt.config import ENV_EXAMPLE, Settings

ROOT = Path(__file__).resolve().parents[2]


def test_env_example_covers_every_setting() -> None:
    missing = [
        f"OBSALT_{name.upper()}"
        for name in Settings.model_fields
        if f"OBSALT_{name.upper()}" not in ENV_EXAMPLE
    ]
    assert missing == [], f"ENV_EXAMPLE is missing {missing}"


def test_committed_env_example_matches_constant() -> None:
    assert (ROOT / ".env.example").read_text() == ENV_EXAMPLE
