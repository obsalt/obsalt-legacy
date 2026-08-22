"""Write expected-output files from raw payloads. Review the diff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from obsalt.plugin.host import PluginHost
from obsalt.plugin.protocol import RawEnvelope


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="obsalt-record")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--fixtures", required=True)
    args = parser.parse_args(argv)
    host = PluginHost.load()
    plugin = host.webhook(args.provider)
    raw_dir = Path(args.fixtures) / "raw"
    expected_dir = Path(args.fixtures) / "expected"
    expected_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(raw_dir.glob("*.json")):
        envelope = RawEnvelope(
            envelope_id=path.stem,
            org_id="record",
            provider=args.provider,
            connection_id="record",
            object_key="record",
            body=path.read_bytes(),
            delivery_key=path.stem,
            received_at="2026-08-22T00:00:00+00:00",
        )
        events = [e.model_dump(mode="json") for e in plugin.decode(envelope)]
        dest = expected_dir / path.name
        dest.write_text(json.dumps(events, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {dest}")


if __name__ == "__main__":
    main()
