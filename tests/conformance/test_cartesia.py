from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from obsalt_cartesia.plugin import CartesiaPlugin
from obsalt_testkit.conformance import AuthConformanceTests, DecoderConformanceTests


class TestCartesiaDecoder(DecoderConformanceTests):
    plugin_cls = CartesiaPlugin
    fixtures_dir = Path(str(files("obsalt_cartesia") / "fixtures"))


class TestCartesiaAuth(AuthConformanceTests):
    plugin_cls = CartesiaPlugin
    valid_body = (Path(str(files("obsalt_cartesia") / "fixtures" / "raw" / "call.json"))).read_bytes()
    valid_headers = [(b"x-webhook-secret", b"cartesia-secret")]
    secret_field = "webhook_secret"
    secret = "cartesia-secret"
