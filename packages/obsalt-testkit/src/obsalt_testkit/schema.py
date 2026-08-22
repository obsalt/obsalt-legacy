"""Schema-validated fixtures as a CI gate (§13.1).

A fixture that fails the vendor schema fails CI unless it uses a reviewed,
additive, expiring overlay that still reports the original vendor-schema
mismatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError


@dataclass(frozen=True)
class Overlay:
    path: Path
    owner: str
    review_date: date
    expires: date
    original_error: str
    evidence: str
    raw_name: str


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

    def schema_files(self) -> list[Path]:
        if not self.schema_dir.exists():
            return []
        return [
            path
            for path in sorted(self.schema_dir.glob("*.json"))
            if path.name != "PIN.json" and not path.name.startswith("PIN")
        ]

    def primary_schema(self) -> dict[str, Any]:
        files = self.schema_files()
        if not files:
            raise FileNotFoundError(f"no schema in {self.schema_dir}")
        return _json_object(files[0])

    def raw_payloads(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self.raw_dir.exists():
            return []
        out: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.raw_dir.glob("*.json")):
            out.append((path, _json_object(path)))
        return out


def validate_raw_fixtures(suite: FixtureSuite) -> list[str]:
    """Return human-readable problems. Divergence-with-overlay lines are warnings."""

    files = suite.schema_files()
    if not files:
        return [f"{suite.root}: no vendor schema"]
    schema = suite.primary_schema()
    validator = Draft202012Validator(schema)
    overlays = _load_overlays(suite)
    errors: list[str] = []
    for path, payload in suite.raw_payloads():
        vendor_error = _first_error(validator, payload)
        if vendor_error is None:
            continue
        overlay = overlays.get(path.name)
        if overlay is None:
            errors.append(f"{path.name}: vendor schema mismatch: {vendor_error}")
            continue
        if overlay.expires < date.today():
            errors.append(f"{path.name}: overlay expired {overlay.expires}")
        patched = _apply_overlay_schema(schema, overlay.path)
        patched_error = _first_error(Draft202012Validator(patched), payload)
        if patched_error:
            errors.append(f"{path.name}: overlay still invalid: {patched_error}")
        errors.append(
            f"{path.name}: DIVERGENCE from untouched vendor schema "
            f"({overlay.original_error}); owner={overlay.owner} "
            f"review={overlay.review_date} expires={overlay.expires}"
        )
    return errors


def blocking_errors(messages: list[str]) -> list[str]:
    return [item for item in messages if ": DIVERGENCE " not in item]


def vendor_only_errors(suite: FixtureSuite) -> list[str]:
    schema = suite.primary_schema()
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
    if not suite.overlay_dir.exists():
        return {}
    out: dict[str, Overlay] = {}
    index = suite.overlay_dir / "overlays.json"
    if index.exists():
        data = json.loads(index.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("overlays") or []
        for row in rows:
            raw_name = str(row.get("raw") or row.get("raw_name") or "")
            patch_name = str(row.get("patch") or f"{Path(raw_name).stem}.json")
            out[raw_name] = Overlay(
                path=suite.overlay_dir / patch_name,
                owner=str(row["owner"]),
                review_date=date.fromisoformat(str(row["review_date"])),
                expires=date.fromisoformat(str(row.get("expires") or row.get("expiry"))),
                original_error=str(row.get("original_error") or row.get("original_failure") or ""),
                evidence=str(row.get("evidence") or ""),
                raw_name=raw_name,
            )
    for path in suite.overlay_dir.glob("*.json"):
        if path.name == "overlays.json":
            continue
        meta = json.loads(path.read_text(encoding="utf-8"))
        if "owner" not in meta:
            continue
        raw_name = str(meta.get("raw") or f"{path.stem}.json")
        if raw_name in out:
            continue
        out[raw_name] = Overlay(
            path=path,
            owner=str(meta["owner"]),
            review_date=date.fromisoformat(str(meta["review_date"])),
            expires=date.fromisoformat(str(meta.get("expires") or meta.get("expiry"))),
            original_error=str(meta.get("original_error") or meta.get("original_failure") or ""),
            evidence=str(meta.get("evidence") or ""),
            raw_name=raw_name,
        )
    return out


def _apply_overlay_schema(schema: dict[str, Any], patch_path: Path) -> dict[str, Any]:
    if not patch_path.exists():
        return schema
    patch = _json_object(patch_path)
    merged = json.loads(json.dumps(schema))
    extras = patch.get("additionalProperties")
    if extras is not None:
        merged["additionalProperties"] = extras
    props = patch.get("properties")
    if isinstance(props, dict):
        merged.setdefault("properties", {}).update(props)
    required = patch.get("required")
    if isinstance(required, list):
        existing = list(merged.get("required") or [])
        for item in required:
            if item not in existing:
                existing.append(item)
        merged["required"] = existing
    return merged


def _json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return data
