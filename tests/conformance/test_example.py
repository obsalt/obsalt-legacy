from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from obsalt_example.plugin import ExamplePlugin
from obsalt_testkit.conformance import DecoderConformanceTests


class TestExampleDecoder(DecoderConformanceTests):
    plugin = ExamplePlugin
    fixtures_dir = Path(str(files("obsalt_example") / "fixtures"))
