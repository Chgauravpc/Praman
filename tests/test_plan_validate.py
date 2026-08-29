"""Day 5, block B: every plan step judged before execution.

PRD 6.5/12.1. The claim under test is narrow and load-bearing: judging a
*plan* is not a new capability, it is ``envelope.judge_plan`` -- built and
red-teamed on Day 2 -- applied to ``schemas.PlanStep`` objects instead of
``envelope.Step`` objects. So these tests do not re-prove the envelope (that
is ``tests/test_redteam_envelope.py``'s job); they prove the *wiring*: that a
``RecoveryPlan``'s steps reach ``judge_plan`` unmodified, that a clean plan is
reported clean, and that a plan carrying an engineered violation is reported
as violating and cites the rule.
"""
from __future__ import annotations

from pramaan.envelope import ALLOW, AMEND, REJECT, EnvelopeContext
from pramaan.plan.validate import judge_recovery_plan, violation_rate
from pramaan.schemas import PlanStep, RecoveryPlan

CLEAN_CONTEXT = EnvelopeContext(
    at="2026-08-03T12:00:00+05:30",
    legal_context="service",
    reason_code="insufficient_funds",
    amount_paise=250_000,
    merchant_id="acct_test",
    consent="explicit",
    dlt_template_id="1207161234567890",
    ai_disclosure_scripted=True,
    self_identification_scripted=True,
)


def _plan(*steps: PlanStep) -> RecoveryPlan:
    return RecoveryPlan(signature="reason_class=FUNDS|x=1", steps=list(steps))


def test_a_clean_plan_is_judged_clean():
    plan = _plan(
        PlanStep(step_index=0, action="ACT_WAIT", delay_seconds=0),
        PlanStep(step_index=1, action="ACT_RETRY", delay_seconds=24 * 3600),
    )
    result = judge_recovery_plan(plan, CLEAN_CONTEXT)
    assert result.clean is True
    assert result.violates is False
    assert result.steps_judged == 2
    assert result.allowed == 2
    assert result.first_allowed_index == 0
    for judgement in result.judgements:
        assert judgement.verdict == ALLOW


def test_a_plan_step_that_violates_a_rule_is_reported_and_named():
    """R10: an AI voice call with no disclosure. Not amendable (Day 2's own case)."""
    plan = _plan(
        PlanStep(step_index=0, action="ACT_WAIT", delay_seconds=0),
        PlanStep(step_index=1, action="ACT_VOICE", channel="voice", delay_seconds=0),
    )
    context = EnvelopeContext(
        **{**CLEAN_CONTEXT.__dict__, "ai_disclosure_scripted": False}
    )
    result = judge_recovery_plan(plan, context)
    assert result.violates is True
    assert result.rejected == 1
    assert result.judgements[1].verdict == REJECT
    assert result.judgements[1].rule_id == "R10"
    # the earlier, lawful ACT_WAIT is still the step that would execute
    assert result.first_allowed_index == 0


def test_an_amendable_step_counts_as_a_violation_but_still_has_a_first_allowed_step():
    """G7: an immediate FUNDS retry is amended to a scheduled one, not refused.

    An amendment is a corrected action, not a blocked one (ADR-016) -- but the
    planner still proposed something the envelope had to fix, which is exactly
    what the organic violation rate counts.
    """
    plan = _plan(PlanStep(step_index=0, action="ACT_RETRY", delay_seconds=0))
    result = judge_recovery_plan(plan, CLEAN_CONTEXT)
    assert result.amended == 1
    assert result.rejected == 0
    assert result.violates is True
    assert result.first_allowed_index == 0  # AMEND is an allowed verdict
    assert result.judgements[0].amendment == {"delay_seconds": 24 * 3600}


def test_a_terminal_verdict_stops_the_plan_and_leaves_nothing_allowed():
    """S1: already paid. The thread terminates; later steps are moot, not judged."""
    plan = _plan(
        PlanStep(step_index=0, action="ACT_RETRY", delay_seconds=24 * 3600),
        PlanStep(step_index=1, action="ACT_MESSAGE", channel="sms", delay_seconds=0),
    )
    context = EnvelopeContext(**{**CLEAN_CONTEXT.__dict__, "reason_code": "order_already_paid"})
    result = judge_recovery_plan(plan, context)
    assert result.steps_judged == 1  # judge_plan stops at the first terminator
    assert result.judgements[0].rule_id == "S1"
    assert result.first_allowed_index is None
    assert result.violates is True


def test_as_ledger_payload_is_json_shaped():
    plan = _plan(PlanStep(step_index=0, action="ACT_WAIT", delay_seconds=0))
    result = judge_recovery_plan(plan, CLEAN_CONTEXT)
    payload = result.as_ledger_payload()
    assert payload["signature"] == plan.signature
    assert payload["clean"] is True
    assert payload["rules_cited"] == [result.judgements[0].rule_id]


def test_violation_rate_over_several_plans():
    clean_plan = _plan(PlanStep(step_index=0, action="ACT_WAIT", delay_seconds=0))
    bad_plan = _plan(PlanStep(step_index=0, action="ACT_RETRY", delay_seconds=0))
    judgements = [
        judge_recovery_plan(clean_plan, CLEAN_CONTEXT),
        judge_recovery_plan(clean_plan, CLEAN_CONTEXT),
        judge_recovery_plan(bad_plan, CLEAN_CONTEXT),
    ]
    assert violation_rate(judgements) == 1 / 3
    assert violation_rate([]) == 0.0


def test_validate_does_not_import_the_llm_package():
    """The same architectural boundary the envelope itself is checked for.

    A plan is judged before it is executed and before anything about the model
    that wrote it matters; a validator that could reach ``pramaan.llm`` or
    ``pramaan.execute`` would put the thing under judgement inside the judge.
    """
    import ast
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "pramaan"
        / "plan"
        / "validate.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".")[0:2]
            if top[:2] == ["pramaan", "llm"] or top[:2] == ["pramaan", "execute"]:
                offenders.append(name)
    assert not offenders, offenders
