"""Schema-validated fixtures. A fixture that fails the vendor schema fails CI
unless it uses a reviewed, additive, expiring overlay that still reports the
original vendor-schema mismatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError


@dataclass
class Overlay:
    path: Path
    owner: str
    review_date: date
    expires: date
    original_error: str
    evidence: str


@dataclass
class FixtureSuite:
    root: Path

    @property
    def schema_dir(self) -> Path:
        return self.root / "schema"

    @property
    def overlay_dir(self) -> Path:
        return self.root / "schema_overlays"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def expected_dir(self) -> Path:
        return self.root / "expected"

    def schema(self) -> dict[str, Any]:
        files = sorted(self.schema_dir.glob("*.json"))
        if not files:
            raise FileNotFoundError(f"no schema in {self.schema_dir}")
        return _json_object(files[0])

    def raw_payloads(self) -> list[tuple[Path, dict[str, Any]]]:
        out: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.raw_dir.glob("*.json")):
            out.append((path, _json_object(path)))
        return out

    def expected_for(self, raw_name: str) -> list[dict[str, Any]] | None:
        path = self.expected_dir / raw_name
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise TypeError(f"{path} must contain a JSON array")
        return [row for row in data if isinstance(row, dict)]


def validate_raw_fixtures(suite: FixtureSuite) -> list[str]:
    schema = suite.schema()
    validator = Draft202012Validator(schema)
    overlay_meta = _load_overlays(suite)
    errors: list[str] = []
    for path, payload in suite.raw_payloads():
        vendor_error = _first_error(validator, payload)
        if vendor_error is None:
            continue
        overlay = overlay_meta.get(path.name)
        if overlay is None:
            errors.append(f"{path.name}: vendor schema mismatch: {vendor_error}")
            continue
        if overlay.expires < date.today():
            errors.append(f"{path.name}: overlay expired {overlay.expires}")
        patched = _apply_overlay_schema(schema, suite.overlay_dir / f"{path.stem}.json")
        patched_error = _first_error(Draft202012Validator(patched), payload)
        if patched_error:
            errors.append(f"{path.name}: overlay still invalid: {patched_error}")
        # Keep reporting the original vendor mismatch even when overlay admits it.
        errors.append(
            f"{path.name}: DIVERGENCE from untouched vendor schema ({overlay.original_error}); "
            f"owner={overlay.owner} review={overlay.review_date} expires={overlay.expires}"
        )
    return [e for e in errors if not e.startswith(path.name + ": DIVERGENCE") or True]


def vendor_only_errors(suite: FixtureSuite) -> list[str]:
    """Errors against the untouched vendor schema (reported even with overlays)."""
    schema = suite.schema()
    validator = Draft202012Validator(schema)
    return [
        f"{path.name}: {err}"
        for path, payload in suite.raw_payloads()
        if (err := _first_error(validator, payload))
    ]


def _first_error(validator: Draft202012Validator, payload: Any) -> str | None:
    try:
        validator.validate(payload)
    except ValidationError as exc:
        return str(exc.message)
    return None


def _load_overlays(suite: FixtureSuite) -> dict[str, Overlay]:
    meta_path = suite.overlay_dir / "overlays.json"
    if not meta_path.exists():
        return {}
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    out: dict[str, Overlay] = {}
    for row in data:
        out[row["raw"]] = Overlay(
            path=suite.overlay_dir / row.get("patch", ""),
            owner=row["owner"],
            review_date=date.fromisoformat(row["review_date"]),
            expires=date.fromisoformat(row["expires"]),
            original_error=row["original_error"],
            evidence=row.get("evidence", ""),
        )
    return out


def _apply_overlay_schema(schema: dict[str, Any], patch_path: Path) -> dict[str, Any]:
    if not patch_path.exists():
        return schema
    patch = _json_object(patch_path)
    merged = json.loads(json.dumps(schema))
    if not isinstance(merged, dict):
        return schema
    extras = patch.get("additionalProperties")
    if extras is not None:
        merged["additionalProperties"] = extras
    props = patch.get("properties")
    if isinstance(props, dict):
        merged.setdefault("properties", {}).update(props)
    return merged


def _json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return data
