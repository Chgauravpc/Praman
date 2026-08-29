"""Every plan step judged before execution. The envelope disposes.

PRD 6.4/6.5, BUILD-PLAN Day 5 block B. There is deliberately almost no code in
this file, and that is the point rather than a gap: the envelope (Day 2) was
built *before* any LLM existed precisely so that judging a plan is not a new
capability written under time pressure on Day 5, it is the existing, tested
``envelope.judge_plan`` applied to a new kind of input. ``schemas.PlanStep``
already carries exactly the five attributes ``envelope.judge.as_step`` accepts
(``action``, ``channel``, ``delay_seconds``, ``expected_value_paise``,
``cost_paise``) -- structurally compatible on purpose, so this module has
nothing to adapt.

**AMEND is not optional here.** BUILD-PLAN Day 5 permits shipping a binary
envelope and logging what would have been amended, as a fallback if the day
runs behind. It is not needed: ``envelope.judge`` implemented full
ALLOW/AMEND/REJECT on Day 2, and re-implementing a weaker binary version here
would be building a worse envelope on the day the better one already exists.
What this module adds is the *plan-level* view -- was the sequence the planner
proposed clean, or did the envelope have to correct or refuse part of it -- and
that view is what the organic planner violation rate is computed from.

**Two violation rates, and they answer different questions.**
``pramaan.eval.metrics.refusals(outcomes, "C")`` reports the rate *as traffic
actually experienced it* -- weighted by how often each situation occurs.
``PlanJudgement`` here reports the rate *per distinct plan the planner wrote* --
unweighted by traffic, which is the fairer measure of the planner's own
proposal quality given that BUILD-PLAN 1.6 is explicit that the planner reasons
once per situation, not once per event. Reporting only one would answer only
half of "how good is the planner", so both are printed, separately labelled --
the same discipline PRD 10.2 applies to event-weighted versus value-weighted
recovery.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from pramaan.envelope import ALLOW, AMEND, REJECT, EnvelopeContext, Judgement, judge_plan
from pramaan.schemas import RecoveryPlan


@dataclass(frozen=True)
class PlanJudgement:
    """A plan, and the envelope's opinion on every step of it.

    ``judgements`` may be shorter than ``plan.steps`` -- ``judge_plan`` stops at
    the first terminating verdict (S1/S4/S7), and steps after a terminated
    thread were never going to be reached. Counting them as separate
    rejections would inflate the violation rate with steps that are moot, not
    wrong (see ``envelope.judge_plan``'s own docstring for the identical
    reasoning at the per-event gate).
    """

    plan: RecoveryPlan
    judgements: Tuple[Judgement, ...]

    @property
    def steps_judged(self) -> int:
        return len(self.judgements)

    @property
    def allowed(self) -> int:
        return sum(1 for j in self.judgements if j.verdict == ALLOW)

    @property
    def amended(self) -> int:
        return sum(1 for j in self.judgements if j.verdict == AMEND)

    @property
    def rejected(self) -> int:
        return sum(1 for j in self.judgements if j.verdict == REJECT)

    @property
    def clean(self) -> bool:
        """Every judged step was a clean ALLOW -- nothing corrected, nothing refused."""
        return self.amended == 0 and self.rejected == 0

    @property
    def violates(self) -> bool:
        """The planner proposed something the envelope had to amend or refuse.

        This is the per-plan unit the organic violation rate is built from.
        Amend and reject are counted together on purpose: both are cases where
        the envelope, not the planner, decided what actually happens, and the
        rate this project publishes is about how often that is true -- not
        only about outright refusals.
        """
        return not self.clean

    @property
    def first_allowed_index(self) -> Optional[int]:
        """Index into ``judgements`` of the first non-terminal ALLOW/AMEND step.

        ``None`` if the envelope refused every step it reached (including the
        case where step 0 itself terminates the thread, e.g. S1 on an
        already-paid order). This is the step that would actually execute.
        """
        for index, j in enumerate(self.judgements):
            # Deliberately not ``j.allowed`` -- that property is ``True`` only
            # for a clean ALLOW (``envelope/context.py``), and an amended step
            # still executes, with the amendment applied. Excluding AMEND here
            # would make a plan's would-be-first-action look refused when the
            # envelope actually corrected and ran it.
            if j.verdict in (ALLOW, AMEND):
                return index
        return None

    def as_ledger_payload(self) -> dict:
        return {
            "signature": self.plan.signature,
            "steps_in_plan": len(self.plan.steps),
            "steps_judged": self.steps_judged,
            "allowed": self.allowed,
            "amended": self.amended,
            "rejected": self.rejected,
            "clean": self.clean,
            "first_allowed_index": self.first_allowed_index,
            "rules_cited": [j.rule_id for j in self.judgements],
        }


def judge_recovery_plan(plan: RecoveryPlan, context: EnvelopeContext) -> PlanJudgement:
    """Judge every step of ``plan`` against ``context``. Deterministic, no LLM.

    A thin wrapper by design (see the module docstring): the whole of "the
    envelope disposes" is ``judge_plan`` itself, already built and tested on
    Day 2. Nothing here may import ``pramaan.llm`` or ``pramaan.execute`` --
    the envelope's own boundary test (``tests/test_redteam_envelope.py``)
    checks the package it lives under, and importing an execution concern into
    the module that judges a plan *before* execution would put the cart
    inside the horse.
    """
    judgements = judge_plan(plan.steps, context)
    return PlanJudgement(plan=plan, judgements=judgements)


def violation_rate(judgements: Sequence[PlanJudgement]) -> float:
    """Share of plans carrying at least one amended or rejected step.

    The per-plan (unweighted-by-traffic) organic violation rate. See the
    module docstring for why this is reported alongside, not instead of, the
    per-event figure in ``eval.metrics.refusals``.
    """
    if not judgements:
        return 0.0
    return sum(1 for j in judgements if j.violates) / len(judgements)
