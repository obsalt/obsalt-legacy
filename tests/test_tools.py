from __future__ import annotations

from datetime import datetime, timezone

from obsalt.domain.enums import Provider, Speaker, ToolStatus
from obsalt.domain.models import CanonicalCall, ToolInvocation, Turn
from obsalt.tools.telemetry import enrich_call_tools, enrich_tool, retry_counts, rollup_tools
from obsalt.util import call_id_for


def test_payload_shape_strips_values_and_redacts() -> None:
    tool = ToolInvocation(id="1", name="send_email", metadata={"arguments": {"email": "ada@example.com", "amount": 20}})
    enrich_tool(tool)
    assert tool.payload_shape == {"amount": "integer", "email": "string"}
    assert "ada@example.com" not in (tool.argument_hash or "")


def test_retries_increment_after_failure() -> None:
    tools = [
        ToolInvocation(id="a", name="lookup_order", status=ToolStatus.ERROR),
        ToolInvocation(id="b", name="lookup_order", status=ToolStatus.SUCCESS),
        ToolInvocation(id="c", name="refund_order", status=ToolStatus.SUCCESS),
    ]
    retry_counts(tools)
    assert tools[0].retry_count == 0
    assert tools[1].retry_count == 1
    assert tools[2].retry_count == 0


def test_time_to_tool_and_rollup() -> None:
    start = datetime(2026, 8, 21, 12, 0, 5, tzinfo=timezone.utc)
    call = CanonicalCall(
        id=call_id_for("o", "vapi", "1"),
        org_id="o",
        provider=Provider.VAPI,
        provider_call_id="1",
        turns=[Turn(index=0, speaker=Speaker.USER, text="hi", started_at=datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc))],
        tools=[
            ToolInvocation(
                id="t",
                name="lookup_order",
                status=ToolStatus.SUCCESS,
                started_at=start,
                ended_at=datetime(2026, 8, 21, 12, 0, 5, 400000, tzinfo=timezone.utc),
                metadata={"arguments": {"order_id": "x"}},
            )
        ],
    )
    enrich_call_tools(call)
    assert call.tools[0].time_to_tool_ms == 5000
    assert call.tools[0].duration_ms == 400
    rollup = rollup_tools([call])
    assert rollup[0].name == "lookup_order"
    assert rollup[0].success_rate == 1
    assert rollup[0].p50_ms == 400
