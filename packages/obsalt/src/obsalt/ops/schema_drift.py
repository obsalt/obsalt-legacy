"""Scheduled-style schema drift check. Normal CI stays offline and reproducible (§13.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obsalt.util import sha256_text


def compare_vendored(
    fixtures_dir: Path,
    remote_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema_dir = fixtures_dir / "schema"
    files = [
        path
        for path in sorted(schema_dir.glob("*.json"))
        if path.name != "PIN.json" and not path.name.startswith("PIN")
    ]
    if not files:
        return {"status": "missing", "diverged": False}
    local = json.loads(files[0].read_text(encoding="utf-8"))
    local_hash = sha256_text(json.dumps(local, sort_keys=True))
    pin = {}
    pin_path = schema_dir / "PIN.json"
    if pin_path.exists():
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
    if remote_schema is None:
        return {
            "status": "offline",
            "diverged": False,
            "local_hash": local_hash,
            "schema_revision": pin.get("revision") or pin.get("schema_revision"),
            "note": "Normal CI is offline. Pass a refetched schema to report drift.",
        }
    remote_hash = sha256_text(json.dumps(remote_schema, sort_keys=True))
    return {
        "status": "compared",
        "diverged": local_hash != remote_hash,
        "local_hash": local_hash,
        "remote_hash": remote_hash,
        "schema_revision": pin.get("revision") or pin.get("schema_revision"),
    }
