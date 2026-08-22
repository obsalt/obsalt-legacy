from __future__ import annotations

from typing import Any

_SENSITIVE = {
    "email",
    "email_address",
    "phone",
    "phone_number",
    "ssn",
    "card",
    "card_number",
    "cvv",
    "password",
    "token",
    "secret",
    "authorization",
}


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        inner = json_type_name(value[0]) if value else "unknown"
        return f"array<{inner}>"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def payload_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): payload_shape(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, list):
        if not value:
            return []
        return [payload_shape(value[0])]
    return json_type_name(value)
