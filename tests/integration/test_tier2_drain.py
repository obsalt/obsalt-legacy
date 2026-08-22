"""Tier-2 drain runs after promotion and does not pass ungrounded prices."""

from __future__ import annotations

from obsalt.domain.enums import AnalysisState, Speaker, ToolStatus
from obsalt.domain.events import CallObserved, ToolObserved, TurnObserved
from obsalt.worker.drain import drain_tier2
from obsalt.worker.process import process_normalized_events
from tests.helpers import example_state, fidelity_declaration


def test_drain_tier2_marks_ungrounded_price_failed() -> None:
    state = example_state()
    revision = process_normalized_events(
        [
            CallObserved(source_call_id="claim-1"),
            TurnObserved(
                turn_index=0,
                speaker=Speaker.AGENT,
                text="I refunded $48.50 for order ORD-99999.",
            ),
            ToolObserved(
                tool_id="t1",
                name="lookup_order",
                status=ToolStatus.ERROR,
                error="not_found",
            ),
        ],
        org_id="acme",
        source="example",
        source_call_id="claim-1",
        envelope_id="e-claim",
        declaration=fidelity_declaration(),
        pointers=state.pointers,
        sink=state.sink,
        decoder_version="t/1",
    )
    pending = state.sink.analysis[(revision.org_id, revision.call_id, revision.revision)]
    assert any(row.execution.analyzer_id == "hallucination" for row in pending)
    state.tier2_queue.append((revision.org_id, revision.call_id, revision.revision))
    assert drain_tier2(state) == 1
    rows = state.sink.analysis[(revision.org_id, revision.call_id, revision.revision)]
    hallo = next(row for row in rows if row.execution.analyzer_id == "hallucination")
    assert hallo.execution.state is AnalysisState.COMPLETED
    assert hallo.payload.get("passed") is False
    claims = hallo.payload.get("claims") or hallo.payload.get("candidates") or []
    assert claims
    assert all(item.get("verdict") != "grounded" for item in claims)
