from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from obsalt_elevenlabs.plugin import ElevenLabsPlugin
from obsalt_testkit.conformance import DecoderConformanceTests


class TestElevenLabsDecoder(DecoderConformanceTests):
    plugin_cls = ElevenLabsPlugin
    fixtures_dir = Path(str(files("obsalt_elevenlabs") / "fixtures"))
