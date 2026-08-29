"""Outcome resolution -- run an arm's policy, then find out what happened.

This module contains **no modelling assumption at all**, and that is its design.
It applies the arm's policy, puts the proposed step through the compliance
envelope, asks an oracle whether the event recovered, and writes the ledger rows.
Every invented number lives behind the oracle (``sim/outcomes.py`` today, a
``payment.captured`` webhook reader in production), so the question "where are
your assumptions" has a one-import answer.

**The observation window is applied identically to every arm**, and that is the
single property the whole comparison rests on. If arm A's recoveries were counted
over an unbounded horizon and arm B's over three days, B-A would be negative for
a reason that has nothing to do with recovery. So the window is a parameter of
``resolve_batch``, not of an arm, and it is threaded to the oracle unchanged.

Two things named apart, because PRD 5.1 and PRD 8.1 use "settle window" for both
and they are not the same quantity:

``OBSERVATION_WINDOW_SECONDS``
    How long after detection a recovery still counts. A **measurement** choice.
    Identical across arms.

``T_settle`` (``settle_delay_seconds``)
    How long the policy waits before acting, so it does not pay to message
    someone who is mid-retry. A **policy** choice. It differs by reason class and
    it is one of the things arm C could plausibly beat arm B on.

Conflating them is how a measurement window quietly becomes a treatment.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pramaan import canonical, taxonomy
from pramaan.envelope import EnvelopeContext, Step, judge
from pramaan.eval.arms import (
    ARM_POLICIES,
    DEFAULT_CHANNEL,
    SHADOW_SENDER_ASSUMPTIONS,
    arm_step,
    envelope_context,
)
from pramaan.sense.models import RiskEvent
from sim import outcomes as oracle_module

# --------------------------------------------------------------------------
# The observation window
# --------------------------------------------------------------------------
#
# 72 hours, and the figure is a consequence of two things already fixed rather
# than a free parameter:
#
# 1. FUNDS is the class arm B actually acts on at scale, its balance-arrival
#    median is ~2 days (`sim/generate.py`), and the taxonomy's own scheduled
#    retry lands at +24h. A window shorter than ~48h would censor the retry's
#    effect before it could occur, and would report "scheduled retries do not
#    work" when what happened is that nobody waited for them.
# 2. It has to be short enough to be a real merchant reconciliation horizon. A
#    30-day window would count almost all organic recovery and make the
#    incremental figure vanish into it.
#
# The trade-off is real in both directions, so it is swept rather than asserted:
# `metrics.observation_window_sweep` re-runs the headline at 24h/48h/72h/7d and
# `make demo` prints the curve. PRD 5.1 asks for exactly this ("Sweep it and
# publish the curve"), and a curve for a genuine trade-off is more convincing
# than any single defended constant.
OBSERVATION_WINDOW_SECONDS = 72 * 3600

#: The windows the sweep reports. 72h is the headline; the others bracket it.
OBSERVATION_WINDOW_SWEEP: Tuple[int, ...] = (
    24 * 3600,
    48 * 3600,
    72 * 3600,
    7 * 24 * 3600,
)

# --------------------------------------------------------------------------
# T_settle -- how long the policy waits before acting
# --------------------------------------------------------------------------
#
# PRD 5.1: "Because payment.failed may be followed by payment.captured, a failure
# is provisional. Every event waits T_settle before becoming actionable."
#
# Per class, and derived from the class's own self-recovery speed rather than
# picked: wait roughly one median self-recovery lag, so the events most likely to
# heal themselves are given the chance to. Zero for the classes that never heal,
# because waiting on a MERCHANT_CONFIG failure buys nothing and costs a day.
#
# This is what stops arm B messaging an AUTH_DROPOFF customer who is, per
# Razorpay's own documentation, at that moment correcting their PIN. It is also
# the direct lever on the false-intervention rate, which is why that rate is
# printed next to it.
SETTLE_DELAY_SECONDS: Dict[str, int] = {
    "TECH_TRANSIENT": 600,        # 10 min -- the bank usually returns first
    "AUTH_DROPOFF": 900,          # 15 min -- the anchor case; let them finish
    "FUNDS": 0,                   # the 24h scheduled retry already dominates
    "LIMIT": 0,                   # the cap resets on a date boundary, not a lag
    "INSTRUMENT_DEAD": 1_800,     # 30 min -- a few do re-add a card unprompted
    "MERCHANT_CONFIG": 0,         # does not heal; alert immediately
    "INTEGRATION_BUG": 0,         # does not heal; page immediately
    "ALREADY_PAID": 0,            # S1 terminates; nothing is scheduled
    "RISK": 0,                    # escalate immediately
    "ELIGIBILITY": 0,             # needs a different method, not more time
}


# --------------------------------------------------------------------------
# Envelope input -- moved to eval.arms on Day 5, values unchanged
# --------------------------------------------------------------------------
#
# ``SHADOW_SENDER_ASSUMPTIONS`` and ``envelope_context`` moved to
# ``pramaan.eval.arms`` on Day 5 (imported above) so that arm C's own
# planner-envelope pass, which lives in ``arm_step``, can build the same
# ``EnvelopeContext`` without importing this module -- ``resolve.py`` already
# imports ``arm_step`` from ``arms.py``, so the reverse import would be a
# cycle. `tests/test_ledger_chain.py` still pins the GATE rows byte for byte;
# the move changed where the definition lives, not what it produces.


# --------------------------------------------------------------------------
# The outcome record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What happened to one event in one arm. The unit of every metric."""

    event_id: str
    arm: str
    reason_class: str
    amount_at_risk_paise: int

    #: The action actually taken after the envelope had its say, or ``"none"``.
    action: str
    channel: str
    delay_seconds: int

    #: The action the arm's policy originally proposed, before any amendment.
    #: Kept so the amendment rate is measurable on the arm rather than only on
    #: Day 2's separate gate pass.
    proposed_action: str
    proposed_delay_seconds: int

    recovered: bool
    recovered_at: Optional[str]
    cause: str
    would_recover_unaided: bool
    contacted: bool
    externality: bool
    cost_paise: int

    #: Envelope verdict on the proposed step, or None where the arm proposed
    #: nothing at all (arm A always; arm C until Day 5).
    decision: Optional[str] = None
    rule_fired: Optional[str] = None

    #: Set when the arm wanted to act and could not. Never None-and-acted.
    exception: Optional[str] = None

    #: Would the **rules-only policy** propose a real action for this event?
    #:
    #: A pre-treatment property, and that is what makes it usable. It is a
    #: function of ``cause_signal`` alone -- the reason class picks the default
    #: action, and the default action is either inert or it is not -- so it is
    #: known at detection, identical for the same event in every arm, and cannot
    #: be contaminated by the outcome. Set on arm A events too, which is the
    #: whole point: it defines a subgroup that exists in both arms.
    #:
    #: Why it is worth carrying. Arm B answers TECH_TRANSIENT and AUTH_DROPOFF
    #: with ACT_WAIT, and those are ~72% of volume, so for 72% of events arm B is
    #: *identical to arm A by construction*. Those events contribute zero signal
    #: and their full share of variance to the headline contrast, which is a real
    #: dilution rather than a presentational one. Reporting the actioned subset
    #: alongside the intent-to-treat headline separates "the interventions do not
    #: work" from "the policy declined to intervene", and those two findings call
    #: for opposite responses.
    #:
    #: It is a *subgroup*, not a replacement headline, and metrics.py prints it as
    #: one. Defining a subgroup on a pre-treatment variable is legitimate;
    #: defining one on the outcome would not be.
    actionable: bool = False

    @property
    def recovered_paise(self) -> int:
        return self.amount_at_risk_paise if self.recovered else 0

    @property
    def acted(self) -> bool:
        return self.action != "none"

    @property
    def false_intervention(self) -> bool:
        """A contact spent on someone who was coming back anyway.

        PRD 5.1's whole argument for ``ACT_WAIT`` being first-class. Restricted to
        *contacts* rather than all actions on purpose: a silent retry on a
        customer who would have paid anyway costs nothing and annoys nobody,
        whereas a message does both. Conflating the two would make the metric
        look worse and mean less.
        """
        return self.contacted and self.would_recover_unaided


#: An oracle answers "did this event recover under this action". ``sim.outcomes``
#: today; a webhook reader in production.
Oracle = Callable[[RiskEvent, str, int, int], "oracle_module.Resolution"]


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def _proposed_step(
    arm: str, event: RiskEvent, *, planner: Optional[Any] = None
) -> Optional[Step]:
    """The arm's step, with T_settle folded into its delay.

    ``max`` rather than ``+``: the scheduled-retry delay already clears the settle
    period, and stacking them would push a FUNDS retry to 24h + 0 = 24h in one
    class and 10 min + 24h in another for no reason anybody could defend.

    ``planner`` is arm C's own ``Planner`` instance, threaded through from
    ``resolve_batch`` so a caller can read its stats back after a run; ``None``
    lets ``arm_step`` fall back to the shared default (see ``eval.arms``).
    """
    step = arm_step(arm, event, planner=planner)
    if step is None:
        return None
    settle = SETTLE_DELAY_SECONDS[event.reason_class]
    if settle > step.delay_seconds:
        step = replace(step, delay_seconds=settle)
    return step


def is_actionable(event: RiskEvent) -> bool:
    """Would the rules-only policy propose a real action for this event?

    Depends on ``cause_signal`` only, via the reason class and its default
    action, so it is a pre-treatment property of the event and safe to condition
    on. ``ACT_WAIT``, a merchant alert and an engineer page are all inert on the
    customer payment path (``sim.outcomes.INERT_ACTIONS``), so an event whose
    default action is one of those is one arm B was never going to move.
    """
    step = arm_step("B", event)
    return step is not None and step.action not in oracle_module.INERT_ACTIONS


def resolve_one(
    event: RiskEvent,
    arm: str,
    *,
    window_seconds: int = OBSERVATION_WINDOW_SECONDS,
    oracle: Optional[Oracle] = None,
    planner: Optional[Any] = None,
) -> Outcome:
    """Resolve one event as if it were in ``arm``.

    ``arm`` is a parameter rather than read off the event so that the same
    function can produce *both* potential outcomes for the unbiasedness test.
    Nothing in the live path ever calls it with an arm other than the event's own
    -- ``resolve_batch`` uses ``event.arm`` -- and that separation is the whole
    reason the estimator can be validated at all.

    ``planner`` is arm C's ``Planner`` instance. See ``_proposed_step``.
    """
    resolve_fn = oracle or oracle_module.resolve
    step = _proposed_step(arm, event, planner=planner)
    # Computed from the rules-only policy regardless of which arm we are
    # resolving, so an arm A event and an arm B event with the same reason code
    # agree. Pre-treatment by construction.
    actionable = is_actionable(event)

    if step is None:
        resolution = resolve_fn(event, "none", 0, window_seconds)
        return Outcome(
            event_id=event.event_id,
            arm=arm,
            reason_class=event.reason_class,
            amount_at_risk_paise=event.amount_at_risk_paise,
            action="none",
            channel="none",
            delay_seconds=0,
            proposed_action="none",
            proposed_delay_seconds=0,
            recovered=resolution.recovered,
            recovered_at=resolution.recovered_at,
            cause=resolution.cause,
            would_recover_unaided=resolution.would_recover_unaided,
            contacted=False,
            externality=False,
            cost_paise=0,
            actionable=actionable,
        )

    proposed_action, proposed_delay = step.action, step.delay_seconds
    exception: Optional[str] = None
    decision: Optional[str] = None
    rule_fired: Optional[str] = None

    # -- can the adapter physically do this? -------------------------------
    #
    # Checked before the envelope, because "the channel is not eligible at this
    # hour" is a capability fact and the envelope answers a legal question. An
    # INSTRUMENT_DEAD failure at 02:00 wants ACT_MESSAGE and the messaging
    # channel is not open, and that is not a compliance refusal -- it is arm B
    # having no move. Recording it as a rule refusal would inflate the refusal
    # counts with events no rule ever saw.
    if step.action not in event.available_actions:
        exception = "action %s not available for this event (channel eligibility " \
                    "%s at detection)" % (step.action, event.channel_eligibility)
        step = None
    else:
        judgement = judge(step, envelope_context(event))
        decision, rule_fired = judgement.verdict, judgement.rule_id
        if judgement.verdict == "REJECT":
            exception = "envelope refused: %s (%s)" % (rule_fired, judgement.reason)
            step = None
        elif judgement.verdict == "AMEND" and judgement.amendment:
            step = replace(step, **judgement.amendment)

    if step is None:
        # The arm wanted to act and could not, so the event is resolved as if no
        # action had been taken. Not as "recovered=False" -- that would be a
        # different and wrong claim, because organic recovery still happens to
        # events nobody touched.
        resolution = resolve_fn(event, "none", 0, window_seconds)
        return Outcome(
            event_id=event.event_id,
            arm=arm,
            reason_class=event.reason_class,
            amount_at_risk_paise=event.amount_at_risk_paise,
            action="none",
            channel="none",
            delay_seconds=0,
            proposed_action=proposed_action,
            proposed_delay_seconds=proposed_delay,
            recovered=resolution.recovered,
            recovered_at=resolution.recovered_at,
            cause=resolution.cause,
            would_recover_unaided=resolution.would_recover_unaided,
            contacted=False,
            externality=False,
            cost_paise=0,
            decision=decision,
            rule_fired=rule_fired,
            exception=exception,
            actionable=actionable,
        )

    resolution = resolve_fn(event, step.action, step.delay_seconds, window_seconds)
    return Outcome(
        event_id=event.event_id,
        arm=arm,
        reason_class=event.reason_class,
        amount_at_risk_paise=event.amount_at_risk_paise,
        action=step.action,
        channel=step.channel,
        delay_seconds=step.delay_seconds,
        proposed_action=proposed_action,
        proposed_delay_seconds=proposed_delay,
        recovered=resolution.recovered,
        recovered_at=resolution.recovered_at,
        cause=resolution.cause,
        would_recover_unaided=resolution.would_recover_unaided,
        contacted=resolution.contacted,
        externality=resolution.externality,
        cost_paise=oracle_module.action_cost_paise(step.action),
        decision=decision,
        rule_fired=rule_fired,
        actionable=actionable,
    )


def resolve_batch(
    events: Sequence[RiskEvent],
    *,
    ledger=None,
    window_seconds: int = OBSERVATION_WINDOW_SECONDS,
    oracle: Optional[Oracle] = None,
    planner: Optional[Any] = None,
) -> List[Outcome]:
    """Resolve every event in its own assigned arm.

    ``event.arm`` is read and never recomputed. That is not a micro-optimisation:
    re-deriving the arm here would mean the analysis and the assignment could
    disagree, and the assignment is the thing the randomisation guarantee
    attaches to.

    ``planner`` is threaded to every event's resolution so that arm C shares
    one ``Planner`` (and therefore one signature cache) across the whole
    batch. Passed explicitly by ``pramaan.execute.runner`` so it can read back
    ``planner.stats`` and ``planner.newly_built`` once the batch is resolved;
    left as ``None`` for every caller that does not care, which falls back to
    ``eval.arms``'s shared default instance.
    """
    out: List[Outcome] = []
    for event in events:
        outcome = resolve_one(
            event, event.arm, window_seconds=window_seconds, oracle=oracle,
            planner=planner,
        )
        out.append(outcome)
        if ledger is not None:
            _write_rows(ledger, event, outcome)
    if ledger is not None:
        ledger.conn.commit()
    return out


def _write_rows(ledger, event: RiskEvent, outcome: Outcome) -> None:
    """One OUTCOME row per event, plus an EXCEPTION row where the arm was stuck.

    **No latent field is written.** The ledger is the auditable record of what the
    system did and observed, and ``would_recover_unaided`` is neither -- it is the
    answer key. Writing it would put ground truth into the artifact a reviewer is
    invited to inspect, and worse, into a table the Day 4 investigator's SQL tool
    can read. ADR-010 keeps latent truth in its own quarantined table; this keeps
    it out of the ledger for the same reason.
    """
    ledger.append(
        "OUTCOME",
        ts=outcome.recovered_at or event.detected_at,
        payload={
            "event_id": event.event_id,
            "action": outcome.action,
            "channel": outcome.channel,
            "delay_seconds": outcome.delay_seconds,
            "recovered": outcome.recovered,
            "recovered_at": outcome.recovered_at,
            "cause": outcome.cause,
            "amount_at_risk_paise": outcome.amount_at_risk_paise,
            "amount_recovered_paise": outcome.recovered_paise,
            "contacted": outcome.contacted,
        },
        arm=outcome.arm,
        rule_fired=outcome.rule_fired,
        decision=outcome.decision,
        cost_paise=outcome.cost_paise,
    )
    if outcome.exception is not None:
        ledger.append(
            "EXCEPTION",
            ts=event.detected_at,
            payload={
                "event_id": event.event_id,
                "proposed_action": outcome.proposed_action,
                "reason": outcome.exception,
                "reason_class": outcome.reason_class,
                "channel_eligibility": event.channel_eligibility,
            },
            arm=outcome.arm,
            rule_fired=outcome.rule_fired,
            decision=outcome.decision,
        )


# --------------------------------------------------------------------------
# Both potential outcomes -- for validating the estimator
# --------------------------------------------------------------------------


def potential_outcomes(
    events: Sequence[RiskEvent],
    control_arm: str = "A",
    treatment_arm: str = "B",
    *,
    window_seconds: int = OBSERVATION_WINDOW_SECONDS,
    oracle: Optional[Oracle] = None,
    planner: Optional[Any] = None,
) -> List[Tuple[Outcome, Outcome]]:
    """Every event resolved under **both** arms. Simulation only.

    This is the thing production cannot have and PRD 8.2 is built on: with both
    potential outcomes known for every event, the true average treatment effect
    is an exact quantity rather than an estimate, so the randomised estimator can
    be checked against it with no Monte-Carlo error on the truth side.

    It is also one of the most dangerous functions in the measurement layer,
    since it computes the answer key. Nothing on the agent path may call it,
    and nothing writes its output to the ledger. Its callers as of Day 5:
    ``tests/test_estimator_unbiased.py`` (B against A), and
    ``pramaan.execute.runner`` (C against B) -- the latter uses it only to
    print the *exact* C-B effect alongside the estimated one, never to decide
    or gate anything, which is the same non-agent-path guarantee the B-A use
    already had.
    """
    return [
        (
            resolve_one(e, control_arm, window_seconds=window_seconds, oracle=oracle, planner=planner),
            resolve_one(e, treatment_arm, window_seconds=window_seconds, oracle=oracle, planner=planner),
        )
        for e in events
    ]


def true_effect(pairs: Sequence[Tuple[Outcome, Outcome]]) -> Dict[str, float]:
    """The exact ATE over a batch, from both potential outcomes.

    Three quantities, matching the three the estimator reports, so a caller
    cannot accidentally compare an event-weighted truth against a value-weighted
    estimate -- which PRD 10.2 warns is a defect rather than a rounding issue.
    """
    n = len(pairs)
    if n == 0:
        return {"rate": 0.0, "value_share": 0.0, "money_per_event": 0.0}
    risk = sum(c.amount_at_risk_paise for c, _ in pairs)
    control_rec = sum(1 for c, _ in pairs if c.recovered)
    treat_rec = sum(1 for _, t in pairs if t.recovered)
    control_val = sum(c.recovered_paise for c, _ in pairs)
    treat_val = sum(t.recovered_paise for _, t in pairs)
    return {
        "rate": (treat_rec - control_rec) / n,
        "value_share": ((treat_val - control_val) / risk) if risk else 0.0,
        "money_per_event": (treat_val - control_val) / n,
    }


def _self_check() -> None:
    from pramaan.taxonomy import REASON_CLASSES

    missing = [c for c in REASON_CLASSES if c not in SETTLE_DELAY_SECONDS]
    if missing:
        raise AssertionError("reason classes with no T_settle: %r" % missing)
    if any(v < 0 for v in SETTLE_DELAY_SECONDS.values()):
        raise AssertionError("T_settle cannot be negative")
    if OBSERVATION_WINDOW_SECONDS not in OBSERVATION_WINDOW_SWEEP:
        raise AssertionError(
            "the headline window must appear in the sweep, or the printed curve "
            "will not contain the printed headline"
        )
    # The window has to be long enough to contain the action arm B actually
    # takes at scale, or the measurement censors the treatment rather than the
    # outcome. Checked rather than commented, because the two constants live in
    # different modules and could drift apart.
    from pramaan.envelope.reason_map import MIN_SCHEDULED_RETRY_DELAY_SECONDS

    if OBSERVATION_WINDOW_SECONDS <= MIN_SCHEDULED_RETRY_DELAY_SECONDS:
        raise AssertionError(
            "the observation window (%ds) must exceed the scheduled retry delay "
            "(%ds), or arm B's main action fires after the window closes and the "
            "result would be an artefact of the measurement"
            % (OBSERVATION_WINDOW_SECONDS, MIN_SCHEDULED_RETRY_DELAY_SECONDS)
        )
    # Every channel-bearing action in the map must have a channel, or the
    # envelope would judge (action, "none") and never test the window at all.
    channel_bearing = {"ACT_MESSAGE", "ACT_VOICE"}
    for action in taxonomy.DEFAULT_ACTION_BY_CLASS.values():
        if action in channel_bearing and action not in DEFAULT_CHANNEL:
            raise AssertionError(
                "%s is a default action and has no channel; the envelope would "
                "judge it as channel 'none' and the window would never bind"
                % action
            )


_self_check()
