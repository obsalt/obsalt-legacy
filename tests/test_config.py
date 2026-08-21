from __future__ import annotations

import os

from obsalt.adapters.registry import detect_provider
from obsalt.config import Settings, flatten_toml
from obsalt.domain.enums import Provider, parse_provider
from tests.conftest import load_fixture


def test_parse_provider_aliases() -> None:
    assert parse_provider("vapi") is Provider.VAPI
    assert parse_provider("openai-realtime") is Provider.OPENAI_REALTIME
    assert parse_provider(Provider.BLAND) is Provider.BLAND


def test_detect_provider_from_fixtures() -> None:
    assert detect_provider(load_fixture("vapi_end_of_call.json")) is Provider.VAPI
    assert detect_provider(load_fixture("retell_call_ended.json")) is Provider.RETELL
    assert detect_provider(load_fixture("bland_post_call.json")) is Provider.BLAND
    assert detect_provider(load_fixture("openai_realtime_session.json")) is Provider.OPENAI_REALTIME
    assert detect_provider({"turns": [], "call_id": "n1", "final": True}) is Provider.NATIVE


def test_toml_nested_and_env_override(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("OBSALT_"):
            monkeypatch.delenv(key, raising=False)
    (tmp_path / "obsalt.toml").write_text(
        """
[server]
port = 9090
host = "127.0.0.1"

[auth]
api_keys = "acme:from-toml"
require_auth = true

[export]
otlp_endpoint = "http://localhost:4318"
environment = "staging"
""",
        encoding="utf-8",
    )
    loaded = Settings()
    assert loaded.port == 9090
    assert loaded.host == "127.0.0.1"
    assert loaded.require_auth is True
    assert loaded.parsed_api_keys()["from-toml"] == "acme"
    assert loaded.environment == "staging"

    monkeypatch.setenv("OBSALT_PORT", "8081")
    monkeypatch.setenv("OBSALT_API_KEYS", "prod:from-env")
    overridden = Settings()
    assert overridden.port == 8081
    assert overridden.parsed_api_keys()["from-env"] == "prod"


def test_flatten_toml_obsalt_root() -> None:
    flat = flatten_toml({"obsalt": {"port": 1, "auth": {"api_keys": "a:b"}}})
    assert flat["port"] == 1
    assert flat["api_keys"] == "a:b"


def test_settings_init_kwargs_win() -> None:
    settings = Settings(api_keys="x:y", require_auth=True)
    assert settings.parsed_api_keys() == {"y": "x"}
    assert settings.require_auth is True
