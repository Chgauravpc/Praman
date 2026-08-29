"""Day 6, block D: the canary protocol.

The real-batch test proves the honest, unstaged outcome: the Day 4 incident's
own ``decompose`` call, read straight off a real ``ToolBelt`` with no LLM
involved, already names the injected segments correctly, so the canary
confirms it. The refutation and retraction path is proved separately,
against a tool result built specifically to be wrong -- never against the
real batch. See ``pramaan/investigate/canary.py``'s own docstring for why
that split is deliberate, not a missed opportunity.
"""
from __future__ import annotations

import json

import pytest

from pramaan.investigate import canary as C
from pramaan.investigate import tools as T
from pramaan.ledger.chain import Ledger
from pramaan.sense.store import connect
from sim.generate import dev_batch_degraded
from sim.incident import DEV_INCIDENT, build_downtime, build_traffic, truth_for

TS = "2026-08-03T10:00:00+05:30"


def _rows(ledger):
    """(kind, payload) for every row, in sequence order -- payload parsed."""
    return [(r["kind"], json.loads(r["payload"])) for r in ledger.iter_raw()]


@pytest.fixture(scope="module")
def real_decompose_call():
    """A genuine decompose call against the real, degraded dev batch --
    exactly what a Day 4 investigation session would have made, with no
    scripted or synthetic data anywhere in this fixture."""
    events, _truth = dev_batch_degraded()
    traffic = build_traffic(events, 42, DEV_INCIDENT)
    downtime = build_downtime(DEV_INCIDENT)
    belt = T.ToolBelt(T.build_agent_db(events, traffic, downtime))
    return belt.call("decompose", {"window": [3, 4], "dimension": "segment"})


@pytest.fixture()
def ledger():
    conn = connect(None)
    yield Ledger(conn)
    conn.close()


# --------------------------------------------------------------------------
# The honest case: the real incident, unstaged
# --------------------------------------------------------------------------


def test_the_canary_confirms_the_real_incidents_own_diagnosis(real_decompose_call):
    """Not staged. ``DEV_INCIDENT`` was built to be correctly diagnosable
    (STATE.md, Day 4), so this is expected to confirm -- and a canary that
    could not correctly confirm a genuine true positive would be a broken
    canary, not a more impressive one."""
    truth = truth_for(DEV_INCIDENT)
    verdict = C.run_canary([real_decompose_call], truth)

    assert verdict.verdict == C.CONFIRMED
    assert verdict.observed_rate_segment == "tier2" == truth.rate_segment
    assert verdict.observed_mix_segment == "tier3" == truth.mix_segment


def test_confirming_writes_only_a_canary_row(ledger, real_decompose_call):
    truth = truth_for(DEV_INCIDENT)
    verdict = C.run_canary([real_decompose_call], truth)
    C.write_canary_result(ledger, ts=TS, arm="A", verdict=verdict)

    kinds = [kind for kind, _payload in _rows(ledger)]
    assert kinds.count("CANARY") == 1
    assert kinds.count("RETRACTION") == 0
    assert ledger.verify_chain().ok


# --------------------------------------------------------------------------
# NOT_TESTABLE -- a session that never called decompose
# --------------------------------------------------------------------------


def test_a_session_with_no_decompose_call_is_not_testable():
    truth = truth_for(DEV_INCIDENT)
    verdict = C.run_canary([], truth)
    assert verdict.verdict == C.NOT_TESTABLE
    assert verdict.observed_rate_segment is None


def test_not_testable_writes_a_canary_row_but_no_retraction(ledger):
    truth = truth_for(DEV_INCIDENT)
    verdict = C.run_canary([], truth)
    C.write_canary_result(ledger, ts=TS, arm="A", verdict=verdict)
    kinds = [kind for kind, _payload in _rows(ledger)]
    assert kinds == ["CANARY"]


# --------------------------------------------------------------------------
# The refutation and retraction path -- proved against a deliberately wrong
# tool result, never against the real batch (see the module docstring)
# --------------------------------------------------------------------------


def _fake_decompose_call(*, rate_segment: str, mix_segment: str) -> T.ToolResult:
    """A synthetic ``decompose`` result naming ``rate_segment``/``mix_segment``
    as the dominant terms -- built for exactly one purpose: to drive the
    canary's REFUTED path deterministically, with evidence that never came
    from a real investigation."""
    per_key = [
        {"key": "metro", "rate_effect": 0.0001, "mix_effect": 0.0001},
        {"key": "tier2", "rate_effect": 0.0001, "mix_effect": 0.0001},
        {"key": "tier3", "rate_effect": 0.0001, "mix_effect": 0.0001},
    ]
    for row in per_key:
        if row["key"] == rate_segment:
            row["rate_effect"] = 0.0379
        if row["key"] == mix_segment:
            row["mix_effect"] = 0.0348
    result = {
        "dimension": "segment",
        "window_days": [3, 4],
        "baseline_days": [0, 1, 2],
        "blended_rate_baseline": 0.238,
        "blended_rate_window": 0.310,
        "observed_change": 0.072,
        "rate_effect": 0.0379,
        "mix_effect": 0.0348,
        "interaction": 0.0001,
        "per_key": per_key,
    }
    return T.ToolResult(
        call_id="tc_fake_01",
        tool="decompose",
        args={"window": [3, 4], "dimension": "segment"},
        args_hash="sha256:fake",
        result=result,
        result_hash="sha256:fake",
        row_count=len(per_key),
        ok=True,
        rendered="fake, for testing the refutation path only",
        elapsed_ms=0,
    )


def test_a_diagnosis_naming_the_wrong_segment_is_refuted():
    truth = truth_for(DEV_INCIDENT)  # rate_segment=tier2, mix_segment=tier3
    wrong_call = _fake_decompose_call(rate_segment="tier3", mix_segment="tier2")
    verdict = C.run_canary([wrong_call], truth)

    assert verdict.verdict == C.REFUTED
    assert verdict.observed_rate_segment == "tier3"
    assert verdict.truth_rate_segment == "tier2"


def test_a_refutation_writes_a_canary_row_and_a_retraction_row_with_evidence(ledger):
    truth = truth_for(DEV_INCIDENT)
    wrong_call = _fake_decompose_call(rate_segment="tier3", mix_segment="tier2")
    verdict = C.run_canary([wrong_call], truth)
    assert verdict.verdict == C.REFUTED

    C.write_canary_result(ledger, ts=TS, arm="A", verdict=verdict)
    rows = _rows(ledger)
    kinds = [kind for kind, _payload in rows]
    assert kinds == ["CANARY", "RETRACTION"]

    _kind, retraction_payload = rows[1]
    evidence = retraction_payload["contradicting_evidence"]
    assert evidence["truth"]["rate_segment"] == "tier2"
    assert evidence["per_key"]  # the wrong tool result travels with the retraction
    assert ledger.verify_chain().ok


def test_a_diagnosis_naming_the_correct_segments_is_confirmed_even_when_synthetic():
    """The flip side of the fake-call fixture: a synthetic call that happens
    to name the true segments still confirms -- the canary judges the
    numbers, not where they came from."""
    truth = truth_for(DEV_INCIDENT)
    right_call = _fake_decompose_call(rate_segment="tier2", mix_segment="tier3")
    verdict = C.run_canary([right_call], truth)
    assert verdict.verdict == C.CONFIRMED


def test_verdict_rejects_an_unknown_string():
    with pytest.raises(ValueError):
        C.CanaryVerdict(
            verdict="MAYBE",
            observed_rate_segment=None,
            observed_mix_segment=None,
            truth_rate_segment=None,
            truth_mix_segment=None,
            evidence={},
        )
