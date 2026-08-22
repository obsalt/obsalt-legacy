"""T5: every source's normalized events pass one redaction choke point."""

from __future__ import annotations

from obsalt.domain.enums import GroundingKind, Provenance, Speaker
from obsalt.domain.events import CallObserved, GroundingObserved, ToolObserved, TurnObserved
from obsalt.redact.choke import redact_events


def test_redaction_choke_strips_phone_email_and_tool_secrets() -> None:
    result = redact_events(
        [
            CallObserved(source_call_id="c1", from_number="+15551234567", to_number="+15557654321"),
            TurnObserved(turn_index=0, speaker=Speaker.USER, text="Call me at +1 555-111-2222 or a@b.co"),
            GroundingObserved(
                kind=GroundingKind.USER_TEXT,
                content="email me at user@example.com",
                provenance=Provenance.PROVIDER_REPORTED,
            ),
            ToolObserved(tool_id="t1", name="lookup", args={"phone": "+15550001111", "order_id": 9}),
        ]
    )
    call = next(e for e in result.events if isinstance(e, CallObserved))
    turn = next(e for e in result.events if isinstance(e, TurnObserved))
    grounding = next(e for e in result.events if isinstance(e, GroundingObserved))
    tool = next(e for e in result.events if isinstance(e, ToolObserved))
    assert call.from_number == "<phone>"
    assert call.to_number == "<phone>"
    assert "+1 555-111-2222" not in turn.text
    assert "<phone>" in turn.text
    assert "a@b.co" not in turn.text
    assert "user@example.com" not in grounding.content
    assert isinstance(tool.args, dict)
    assert "redacted" in str(tool.args["phone"])
    assert tool.args["order_id"] == 9
    assert result.redacted_fields
