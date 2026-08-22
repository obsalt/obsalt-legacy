from __future__ import annotations

from itertools import permutations

from hypothesis import given, settings
from hypothesis import strategies as st
from obsalt.assemble.assembler import fold_events, stamp_events
from obsalt.assemble.timeline import timeline_view
from obsalt.domain.enums import MeasurementPlacement, Metric, PipelineArchitecture, Provenance, Speaker, Stage
from obsalt.domain.events import CallObserved, StageObserved, TurnObserved
from obsalt.domain.time import parse_datetime


def _events() -> list:
    return [
        CallObserved(
            source_call_id="c1",
            agent_id="a",
            architecture=PipelineArchitecture.CASCADE,
        ),
        TurnObserved(
            turn_index=0,
            speaker=Speaker.USER,
            text="hi",
            started_at=parse_datetime("2026-08-22T12:00:00Z"),
            ended_at=parse_datetime("2026-08-22T12:00:02Z"),
        ),
        StageObserved(
            stage=Stage.STT,
            metric=Metric.DURATION,
            value_ms=120,
            turn_index=0,
            placement=MeasurementPlacement.UNPLACED,
            provenance=Provenance.PROVIDER_REPORTED,
            source_path="latency.stt",
        ),
    ]


def test_fold_is_idempotent() -> None:
    events = _events()
    a = fold_events(events + events, org_id="o", source="example", source_call_id="c1")
    b = fold_events(events, org_id="o", source="example", source_call_id="c1")
    assert len(a.turns) == len(b.turns) == 1
    assert len(a.stage_measurements) == len(b.stage_measurements) == 1


def test_fold_is_order_independent() -> None:
    events = _events()
    revisions = []
    for perm in permutations(range(len(events))):
        ordered = [events[i] for i in perm]
        revisions.append(fold_events(ordered, org_id="o", source="example", source_call_id="c1"))
    keys = {(len(r.turns), len(r.stage_measurements), r.identity.agent_id) for r in revisions}
    assert len(keys) == 1


def test_conflict_without_revision() -> None:
    first = CallObserved(source_call_id="c1", agent_id="one")
    second = CallObserved(source_call_id="c1", agent_id="two")
    first.fact_id = "same"
    second.fact_id = "same"
    revision = fold_events([first, second], org_id="o", source="example", source_call_id="c1")
    assert revision.conflicts


def test_unplaced_stages_do_not_draw_waterfall() -> None:
    revision = fold_events(_events(), org_id="o", source="example", source_call_id="c1")
    view = timeline_view(revision)
    assert view["waterfall"] is False
    assert view["intervals"] == []
    assert view["turns"][0]["chips"]


@given(st.lists(st.sampled_from(_events()), min_size=1, max_size=6))
@settings(max_examples=20)
def test_stamp_then_fold_never_crashes(sample) -> None:
    stamped = stamp_events(
        sample,
        org_id="o",
        source="example",
        envelope_id="e",
        decoder_version="example/1",
        processing_run_id="r",
    )
    fold_events(stamped, org_id="o", source="example", source_call_id="c1")
