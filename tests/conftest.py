from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"
for name in (
    "obsalt",
    "obsalt-testkit",
    "obsalt-example",
    "obsalt-vapi",
    "obsalt-retell",
    "obsalt-elevenlabs",
    "obsalt-cartesia",
    "obsalt-openai-realtime",
    "obsalt-gemini-live",
    "obsalt-pipecat",
    "obsalt-livekit",
):
    src = PACKAGES / name / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


@pytest.fixture
def example_plugin():
    from obsalt_example.plugin import ExamplePlugin

    return ExamplePlugin()
