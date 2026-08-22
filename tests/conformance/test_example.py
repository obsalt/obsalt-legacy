from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from obsalt_example.plugin import ExamplePlugin
from obsalt_testkit.conformance import AuthConformanceTests, DecoderConformanceTests


class TestExampleDecoder(DecoderConformanceTests):
    plugin_cls = ExamplePlugin
    fixtures_dir = Path(str(files("obsalt_example") / "fixtures"))


class TestExampleAuth(AuthConformanceTests):
    plugin_cls = ExamplePlugin
    valid_body = (
        Path(str(files("obsalt_example") / "fixtures" / "raw" / "call_completed.json"))
    ).read_bytes()
    valid_headers = [(b"x-example-secret", b"example-secret")]
    secret_field = "shared_secret"
    secret = "example-secret"
