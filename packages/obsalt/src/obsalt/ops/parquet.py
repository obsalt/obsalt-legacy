"""Parquet export of finalized calls. Manifest records revision and deletion status (§10.6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obsalt.domain.models import CallRevision
from obsalt.util import canonical_json, utcnow


def export_revisions(
    revisions: list[CallRevision],
    dest_dir: Path,
    *,
    as_of_generation: str,
    deleted_call_ids: set[str] | None = None,
) -> dict[str, Any]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    deleted = deleted_call_ids or set()
    records = [
        {
            "org_id": rev.org_id,
            "call_id": rev.call_id,
            "revision": rev.revision,
            "source": rev.source,
            "agent_id": rev.agent_id,
            "started_at": rev.started_at.isoformat() if rev.started_at else None,
            "timeline_fidelity": rev.timeline_fidelity.value,
            "deleted": rev.call_id in deleted,
        }
        for rev in revisions
    ]
    data_path = dest_dir / "calls.jsonl"
    parquet_path = dest_dir / "calls.parquet"
    format_used = "jsonl"
    data_path.write_text("".join(canonical_json(row) + "\n" for row in records))
    if _write_parquet(records, parquet_path):
        format_used = "parquet"
    manifest = {
        "as_of_generation": as_of_generation,
        "exported_at": utcnow().isoformat(),
        "call_count": len(records),
        "deleted_count": sum(1 for row in records if row["deleted"]),
        "format": format_used,
        "files": [data_path.name] + ([parquet_path.name] if format_used == "parquet" else []),
        "note": (
            "obsalt propagates deletion to managed exports; it cannot revoke "
            "copies moved to an external warehouse."
        ),
    }
    (dest_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _write_parquet(records: list[dict[str, Any]], path: Path) -> bool:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return False
    table = pa.Table.from_pylist(records)
    pq.write_table(table, path)
    return True
