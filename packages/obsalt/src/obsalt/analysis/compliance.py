"""Deterministic compliance flags. Regex-grade, audit-proof, no judge.

The agent speaking a card number or SSN aloud is the violation, so only
agent turns are scanned. Spans are masked before they ever reach a
payload: a detector must not republish the secret it caught.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from obsalt.domain.models import CallRevision

# 13-19 digits with optional separators between digit runs.
_PAN_RE = re.compile(r"(?<!\d)\d(?:[\s-]?\d){12,18}(?!\d)")
_SSN_RE = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
_SECRET_REQUEST_RE = re.compile(
    r"\b(?:what(?:'s| is| are)|tell me|give me|can i get|could you (?:give|provide|share)"
    r"|please (?:provide|share|confirm)|share|confirm|say|read (?:out|me))\b[^.?!]{0,60}?"
    r"\b(password|passcode|pin|one[- ]time (?:code|password)|verification code|"
    r"social security number|mother'?s maiden name)\b",
    re.I,
)


def compliance_flags(call: CallRevision) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for turn in call.agent_turns():
        if not turn.text:
            continue
        for span in _luhn_pan_spans(turn.text):
            out.append(
                {
                    "kind": "pan_spoken",
                    "turn_index": turn.index,
                    "span_text": _mask_pan(span),
                }
            )
        for span in _SSN_RE.findall(turn.text):
            out.append(
                {
                    "kind": "ssn_spoken",
                    "turn_index": turn.index,
                    "span_text": _mask_ssn(span),
                }
            )
        for match in _SECRET_REQUEST_RE.finditer(turn.text):
            out.append(
                {
                    "kind": "verbal_secret_request",
                    "turn_index": turn.index,
                    "span_text": match.group(0),
                }
            )
    return out


def luhn_valid(digits: str) -> bool:
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _luhn_pan_spans(text: str) -> Iterator[str]:
    for match in _PAN_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if luhn_valid(digits):
            yield match.group(0)


def _mask_pan(span: str) -> str:
    digits = re.sub(r"\D", "", span)
    masked = "*" * (len(digits) - 4) + digits[-4:]
    groups = [masked[i : i + 4] for i in range(0, len(masked), 4)]
    return " ".join(group for group in groups if group)


def _mask_ssn(span: str) -> str:
    digits = re.sub(r"\D", "", span)
    return f"***-**-{digits[-4:]}"
