from __future__ import annotations

from obsalt.cli import run
from tests.conftest import FIXTURES


def test_cli_version(capsys) -> None:
    assert run(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_cli_init_and_doctor(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OBSALT_CONFIG", raising=False)
    monkeypatch.delenv("OBSALT_API_KEYS", raising=False)
    monkeypatch.delenv("OBSALT_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("OBSALT_OTLP_ENDPOINT", raising=False)
    assert run(["init"]) == 0
    assert (tmp_path / "obsalt.toml").is_file()
    assert (tmp_path / ".env.example").is_file()
    assert run(["init"]) == 0  # skip existing
    out = capsys.readouterr().out
    assert "skipped" in out

    assert run(["doctor"]) == 0
    doctor = capsys.readouterr().out
    assert "obsalt doctor" in doctor
    assert "keys" in doctor


def test_cli_parse_vapi_fixture(capsys) -> None:
    path = str(FIXTURES / "vapi_end_of_call.json")
    assert run(["parse", path, "--org", "acme"]) == 0
    out = capsys.readouterr().out
    assert "vapi" in out
    assert "user_hangup" in out
    assert "lookup_order" in out


def test_cli_parse_detects_retell(capsys) -> None:
    path = str(FIXTURES / "retell_call_ended.json")
    assert run(["parse", path]) == 0
    out = capsys.readouterr().out
    assert "retell" in out
    assert "agent_hangup" in out


def test_cli_parse_json_dump(capsys) -> None:
    import json

    path = str(FIXTURES / "bland_post_call.json")
    assert run(["parse", path, "--json", "--org", "acme"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "bland"
    assert payload["finalized"] is True


def test_cli_parse_missing_file() -> None:
    assert run(["parse", "does-not-exist.json"]) == 1


def test_detect_used_by_parse_openai(capsys) -> None:
    assert run(["parse", str(FIXTURES / "openai_realtime_session.json"), "--org", "acme"]) == 0
    assert "openai_realtime" in capsys.readouterr().out
