from __future__ import annotations

import re
from typing import Any

_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{7,}\d")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

_SENSITIVE_KEYS = {
    "email",
    "email_address",
    "phone",
    "phone_number",
    "ssn",
    "social_security",
    "card",
    "card_number",
    "credit_card",
    "cvv",
    "password",
    "token",
    "secret",
    "authorization",
    "dob",
    "date_of_birth",
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
    """Return a JSON-type sketch of a tool payload without values."""
    if isinstance(value, dict):
        return {str(k): payload_shape(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list):
        if not value:
            return []
        return [payload_shape(value[0])]
    return json_type_name(value)


def redact_value(key: str | None, value: Any) -> Any:
    key_l = (key or "").lower()
    if key_l in _SENSITIVE_KEYS or any(part in key_l for part in ("email", "phone", "ssn", "card", "secret", "token")):
        return f"<{json_type_name(value)}:redacted>"
    if isinstance(value, str):
        text = _EMAIL_RE.sub("<email>", value)
        text = _SSN_RE.sub("<ssn>", text)
        text = _CARD_RE.sub("<card>", text)
        text = _PHONE_RE.sub("<phone>", text)
        return text[:500]
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(key, item) for item in value[:20]]
    return value


def preview_text(value: Any, limit: int = 240) -> str | None:
    if value is None:
        return None
    redacted = redact_value(None, value)
    text = redacted if isinstance(redacted, str) else str(redacted)
    text = text.strip()
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text or None
