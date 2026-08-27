"""The metrics. Rates, money, intervals, costs, refusals, sensitivity.

Everything here is a projection over ``List[Outcome]``. It computes no outcome of
its own, which is the same constraint the Day 7 recovery report is under and for
the same reason: if two layers can each derive the headline number, they can
disagree, and then there are two truths and no audit trail.

**Four things this module is careful about, because each one is a way to be
confidently wrong:**

1. **Event-weighted and value-weighted are different quantities** (PRD 10.2).
   Under log-normal amounts they diverge, and the value-weighted figure is
   usually the smaller of the two because small payments self-recover more
   readily than large ones. They are computed by separate functions, labelled
   separately in the output, and never averaged together.

2. **The money interval must be wider than the rate interval.** Not as a
   convention -- as a consequence of the amounts being heavy-tailed. If it comes
   out narrower, the bootstrap is wrong, so it is checked and reported as a
   check rather than left for a reader to notice.

3. **An unwired arm gets no number.** Arm C takes no action today, so its
   outcomes are identical to arm A's. Printing ``C: 24.0%`` would read as "the
   LLM is no better than nothing", which is not a finding -- the LLM does not
   exist yet. ``arm_summaries`` raises if asked to report one.

4. **Gross recovery is printed, and printed as inflated.** PRD 3: reporting gross
   recovered-rupees is a category error, because Razorpay's own webhook docs say
   ``payment.failed`` is frequently followed by ``payment.captured`` with nobody
   intervening. The gross figure appears next to the incremental one with the
   over-claim quantified, because the contrast is the argument.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

from pramaan import canonical
from pramaan.eval import bootstrap as bs
from pramaan.eval.arms import ARM_POLICIES, WIRED_ARMS
from pramaan.eval.resolve import (
    OBSERVATION_WINDOW_SECONDS,
    OBSERVATION_WINDOW_SWEEP,
    Outcome,
    resolve_batch,
)
from pramaan.sense.models import RiskEvent

#: The organic-recovery levels PRD 10.2 sweeps. 30% is the simulator's
#: calibration target and the central case.
SENSITIVITY_LEVELS: Tuple[float, ...] = (0.15, 0.30, 0.50, 0.70)
SENSITIVITY_CENTRAL = 0.30


# --------------------------------------------------------------------------
# Per-arm summary
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmSummary:
    """One arm's observed behaviour. Descriptive; no contrast, no interval."""

    arm: str
    n: int
    at_risk_paise: int
    recovered: int
    recovered_paise: int

    #: Recoveries the counterfactual says would have happened anyway. Available
    #: only in simulation, and the whole reason the holdout exists.
    would_recover_unaided: int

    actions_taken: int
    contacts: int
    false_interventions: int
    cost_paise: int

    externalities: int
    externality_paise: int
    exceptions: int

    #: Value recovered where a *contact* was what caused it. Distinct from
    #: ``recovered_paise``, which includes organic recovery and silent retries.
    #:
    #: This existed as a bug first and is worth the comment. "Rupees per contact"
    #: was originally ``recovered_paise / contacts``, which on the dev batch
    #: divided every rupee the arm recovered -- organic included -- by the two
    #: messages it happened to send, and reported Rs 50,166 per contact. The
    #: number was nonsense in the flattering direction, which is the worst kind.
    contact_attributed_paise: int = 0

    causes: Dict[str, int] = field(default_factory=dict)
    action_counts: Dict[str, int] = field(default_factory=dict)
    cost_by_action: Dict[str, int] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return self.recovered / self.n if self.n else 0.0

    @property
    def value_share(self) -> float:
        return self.recovered_paise / self.at_risk_paise if self.at_risk_paise else 0.0

    @property
    def money_per_event_paise(self) -> float:
        return self.recovered_paise / self.n if self.n else 0.0

    @property
    def false_intervention_rate(self) -> float:
        """Of contacts made, the share spent on someone already coming back.

        Denominator is contacts, not events. A rate over events would fall
        automatically as the policy contacted fewer people, which would reward
        doing nothing -- and doing nothing is already arm A.
        """
        return self.false_interventions / self.contacts if self.contacts else 0.0

    @property
    def cost_per_recovered_rupee(self) -> float:
        return self.cost_paise / self.recovered_paise if self.recovered_paise else 0.0

    @property
    def paise_recovered_per_contact(self) -> float:
        """PRD 10.3's actual objective: rupees recovered per customer contact.

        Not per rupee of compute. The scarce resource is permission to contact
        someone, and with CAC at Rs 800-1,200 a message that recovers Rs 1,500
        but costs the relationship destroys value even though it cost 15 paise.

        Numerator is ``contact_attributed_paise``, not total recovered value --
        only money a contact actually brought in belongs above a per-contact
        denominator. See the note on that field.
        """
        return (
            self.contact_attributed_paise / self.contacts if self.contacts else 0.0
        )


def summarise_arm(outcomes: Sequence[Outcome], arm: str) -> ArmSummary:
    rows = [o for o in outcomes if o.arm == arm]
    causes: Dict[str, int] = {}
    actions: Dict[str, int] = {}
    cost_by_action: Dict[str, int] = {}
    for o in rows:
        causes[o.cause] = causes.get(o.cause, 0) + 1
        actions[o.action] = actions.get(o.action, 0) + 1
        if o.cost_paise:
            cost_by_action[o.action] = (
                cost_by_action.get(o.action, 0) + o.cost_paise
            )
    return ArmSummary(
        arm=arm,
        n=len(rows),
        at_risk_paise=sum(o.amount_at_risk_paise for o in rows),
        recovered=sum(1 for o in rows if o.recovered),
        recovered_paise=sum(o.recovered_paise for o in rows),
        would_recover_unaided=sum(1 for o in rows if o.would_recover_unaided),
        actions_taken=sum(1 for o in rows if o.acted),
        contacts=sum(1 for o in rows if o.contacted),
        false_interventions=sum(1 for o in rows if o.false_intervention),
        cost_paise=sum(o.cost_paise for o in rows),
        externalities=sum(1 for o in rows if o.externality),
        externality_paise=sum(o.amount_at_risk_paise for o in rows if o.externality),
        exceptions=sum(1 for o in rows if o.exception is not None),
        contact_attributed_paise=sum(
            o.recovered_paise for o in rows if o.cause in ("message", "voice")
        ),
        causes=causes,
        action_counts=actions,
        cost_by_action=cost_by_action,
    )


def arm_sample(
    outcomes: Sequence[Outcome], arm: str, *, actionable_only: bool = False
) -> bs.ArmSample:
    rows = [o for o in outcomes if o.arm == arm]
    if actionable_only:
        rows = [o for o in rows if o.actionable]
    return bs.ArmSample(
        recovered=tuple(1.0 if o.recovered else 0.0 for o in rows),
        recovered_paise=tuple(float(o.recovered_paise) for o in rows),
        at_risk_paise=tuple(float(o.amount_at_risk_paise) for o in rows),
    )


# --------------------------------------------------------------------------
# Contrasts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Contrast:
    """One arm minus another, with an interval per statistic."""

    treatment: str
    control: str
    n_treatment: int
    n_control: int

    #: Keyed by statistic name: rate | value_share | money_per_event.
    intervals: Dict[str, bs.Interval]

    #: The Wald interval on the rate, for cross-checking the bootstrap against a
    #: closed form. PRD 8.1 says a normal CI is fine for a proportion, so this is
    #: the one statistic where the two methods should agree -- which makes their
    #: agreement evidence that the bootstrap is implemented correctly.
    rate_normal: bs.Interval

    #: Control-arm levels, used to put the rate and money intervals on a common
    #: scale. See ``relative_widths``.
    baseline_rate: float = 0.0
    baseline_money_paise: float = 0.0

    #: True if this contrast is restricted to the pre-specified actioned subset
    #: rather than the whole batch. Carried on the object so that a caller cannot
    #: print a subgroup figure as though it were the headline.
    actionable_only: bool = False

    @property
    def relative_widths(self):
        """(rate, money) interval widths as a share of the control-arm level.

        The two statistics are in different units, so raw widths cannot be
        compared and something has to normalise them.

        **Not by their own point estimates.** That was the first attempt and it is
        wrong: an incremental rate near zero makes its own relative width
        explode, so the test reported "the money interval is narrower" on the
        full batch purely because B-A on the rate came out at 0.6pp. The quantity
        being compared is how precisely each statistic can be *estimated*, and
        dividing by a noisy near-zero estimate measures the estimate rather than
        the precision.

        **By the control arm's level instead.** Arm A's recovery rate and arm A's
        rupees-per-event are both stable, well-estimated quantities, and each
        sets the natural scale for its own metric. So this asks: as a fraction of
        the baseline it is measured against, which interval is wider? On
        log-normal amounts the money one must be, because a handful of band-5
        events move a mean far more than any single event moves a proportion.
        """
        rate_iv = self.intervals["rate"]
        money_iv = self.intervals["money_per_event"]
        rate_rel = (
            rate_iv.width / self.baseline_rate
            if self.baseline_rate > 0
            else float("inf")
        )
        money_rel = (
            money_iv.width / self.baseline_money_paise
            if self.baseline_money_paise > 0
            else float("inf")
        )
        return rate_rel, money_rel

    @property
    def money_interval_is_wider(self) -> bool:
        """The heavy-tail check. If this is False, the bootstrap is wrong."""
        rate_rel, money_rel = self.relative_widths
        return money_rel > rate_rel

    @property
    def money_interval_asymmetry(self) -> float:
        """Upper half-width over lower half-width, for the money interval.

        The second heavy-tail signature, and the one a normal approximation
        cannot produce at all: a symmetric interval has this at exactly 1.0 by
        construction. A value away from 1.0 is direct visible evidence that the
        interval came from the resample distribution's shape rather than from a
        standard error, which is the whole reason PRD 8.1 forbids the normal
        approximation for this metric.
        """
        iv = self.intervals["money_per_event"]
        upper = iv.high - iv.point
        lower = iv.point - iv.low
        if abs(lower) < 1e-12:
            return float("inf")
        return upper / lower

    @property
    def bootstrap_agrees_with_normal(self) -> bool:
        """Do the BCa and Wald rate intervals agree to within 25% of width?

        Loose on purpose. BCa should *not* match Wald exactly -- if it did, the
        correction would be doing nothing. What would be alarming is a large
        disagreement on the one statistic where the closed form is known to be
        adequate, and that is what this catches.
        """
        boot = self.intervals["rate"]
        if self.rate_normal.width <= 0.0:
            return False
        return abs(boot.width - self.rate_normal.width) / self.rate_normal.width < 0.25


def contrast(
    outcomes: Sequence[Outcome],
    treatment: str,
    control: str,
    *,
    seed: int,
    resamples: int = bs.DEFAULT_RESAMPLES,
    actionable_only: bool = False,
) -> Contrast:
    for arm in (treatment, control):
        if not ARM_POLICIES[arm].wired:
            raise ValueError(
                "arm %s is not wired, so a contrast against it is not a result. "
                "It takes no action, which makes its outcomes identical to arm "
                "A's -- printing that as a number would read as a finding about "
                "the LLM rather than about the calendar." % arm
            )
    c_sample = arm_sample(outcomes, control, actionable_only=actionable_only)
    t_sample = arm_sample(outcomes, treatment, actionable_only=actionable_only)
    intervals = {
        name: bs.bootstrap_contrast(
            c_sample,
            t_sample,
            statistic,
            # Per-statistic seed offset, so the three intervals are not driven by
            # one shared resample draw. Derived from the statistic name rather
            # than an index, so adding a statistic cannot silently reseed the
            # existing ones and move published numbers.
            seed=seed ^ (int(canonical.sha256_hex(name)[:8], 16)),
            resamples=resamples,
        )
        for name, statistic in bs.STATISTICS.items()
    }
    nc = len(c_sample)
    return Contrast(
        treatment=treatment,
        control=control,
        n_treatment=len(t_sample),
        n_control=nc,
        intervals=intervals,
        rate_normal=bs.normal_rate_interval(c_sample, t_sample),
        baseline_rate=(sum(c_sample.recovered) / nc) if nc else 0.0,
        baseline_money_paise=(sum(c_sample.recovered_paise) / nc) if nc else 0.0,
        actionable_only=actionable_only,
    )


# --------------------------------------------------------------------------
# Refusals, broken down by the rule that refused
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RefusalBreakdown:
    """Why arm B could not act, split by cause. PRD 8's "refusals by rule".

    Three buckets, and keeping them apart is the point:

    ``by_rule``
        The envelope refused, and this is the rule that did it. The per-rule
        breakdown *is* the guardrail price list of PRD 10.4 -- "R9 blocked N
        contacts carrying Rs X" -- so it carries value alongside each count.

    ``amended_by_rule``
        The envelope allowed the action after changing it. Not a refusal, and
        collapsing it into one would report a fixed plan as a blocked one.

    ``unavailable``
        The channel was not open at that hour, so no rule ever saw the action.
        A capability gap, not a compliance decision, and counting it as a
        refusal would inflate the rules' apparent cost.
    """

    by_rule: Dict[str, int]
    by_rule_paise: Dict[str, int]
    amended_by_rule: Dict[str, int]
    unavailable: int
    unavailable_paise: int
    total_actions_proposed: int

    #: Every envelope verdict on an arm-B action, keyed ``(verdict, rule_id)``,
    #: with the at-risk value each rule touched.
    #:
    #: The refusal breakdown above is the metric PRD 8 asks for, and on a
    #: compliant policy it is **empty** -- arm B never proposes what the envelope
    #: refuses, which is what makes it a fair baseline rather than a strawman. An
    #: empty table is the right answer and a useless thing to print, so this
    #: carries the verdicts that did fire: which rule authorised or corrected each
    #: action, and how much value it governed. Same per-rule shape, non-empty, and
    #: it still answers "which rules are actually deciding anything here".
    verdicts_by_rule: Dict[Tuple[str, str], int] = field(default_factory=dict)
    verdicts_by_rule_paise: Dict[Tuple[str, str], int] = field(default_factory=dict)

    @property
    def refused(self) -> int:
        return sum(self.by_rule.values())

    @property
    def amended(self) -> int:
        return sum(self.amended_by_rule.values())

    @property
    def refusal_rate(self) -> float:
        return (
            self.refused / self.total_actions_proposed
            if self.total_actions_proposed
            else 0.0
        )


def refusals(outcomes: Sequence[Outcome], arm: str = "B") -> RefusalBreakdown:
    by_rule: Dict[str, int] = {}
    by_rule_paise: Dict[str, int] = {}
    amended: Dict[str, int] = {}
    unavailable = 0
    unavailable_paise = 0
    proposed = 0
    verdicts: Dict[Tuple[str, str], int] = {}
    verdicts_paise: Dict[Tuple[str, str], int] = {}
    for o in outcomes:
        if o.arm != arm or o.proposed_action == "none":
            continue
        proposed += 1
        if o.decision and o.rule_fired:
            key = (o.decision, o.rule_fired)
            verdicts[key] = verdicts.get(key, 0) + 1
            verdicts_paise[key] = (
                verdicts_paise.get(key, 0) + o.amount_at_risk_paise
            )
        if o.exception is not None and o.decision == "REJECT" and o.rule_fired:
            by_rule[o.rule_fired] = by_rule.get(o.rule_fired, 0) + 1
            by_rule_paise[o.rule_fired] = (
                by_rule_paise.get(o.rule_fired, 0) + o.amount_at_risk_paise
            )
        elif o.exception is not None:
            unavailable += 1
            unavailable_paise += o.amount_at_risk_paise
        elif o.decision == "AMEND" and o.rule_fired:
            amended[o.rule_fired] = amended.get(o.rule_fired, 0) + 1
    return RefusalBreakdown(
        by_rule=by_rule,
        by_rule_paise=by_rule_paise,
        amended_by_rule=amended,
        unavailable=unavailable,
        unavailable_paise=unavailable_paise,
        total_actions_proposed=proposed,
        verdicts_by_rule=verdicts,
        verdicts_by_rule_paise=verdicts_paise,
    )


# --------------------------------------------------------------------------
# Per-class attribution -- where the effect actually comes from
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassContribution:
    """One reason class's share of the incremental effect.

    Printed because the headline on its own invites the wrong conclusion. A small
    B-A could mean the interventions do not work, or it could mean the policy
    declined to act on most of the volume -- and those call for opposite
    responses. This table distinguishes them, and it is also the map of where arm
    C has room to beat arm B.
    """

    reason_class: str
    share_of_events: float
    default_action: str

    #: True where arm B's action for this class does nothing to the customer
    #: payment path (ACT_WAIT, a merchant alert, an engineer page, ACT_STOP).
    #:
    #: For these classes the true per-event effect is **exactly zero**, because
    #: the resolver returns arm A's outcome unchanged. So any non-zero number in
    #: the lift column is pure arm-assignment noise, and printing it without
    #: saying so invites the reasonable question of why a no-op appears to move
    #: recovery by several points. It also makes the column useful in a second
    #: way: on these rows it is a direct read-out of the noise floor at this
    #: sample size.
    inert: bool
    n_control: int
    n_treatment: int
    control_rate: float
    treatment_rate: float

    @property
    def lift(self) -> float:
        return self.treatment_rate - self.control_rate

    @property
    def contribution(self) -> float:
        """This class's contribution to the overall event-weighted lift."""
        return self.lift * self.share_of_events


def class_contributions(
    outcomes: Sequence[Outcome], treatment: str = "B", control: str = "A"
) -> List[ClassContribution]:
    from pramaan.taxonomy import DEFAULT_ACTION_BY_CLASS
    from sim.outcomes import INERT_ACTIONS

    total = sum(1 for o in outcomes if o.arm in (treatment, control))
    per: Dict[str, Dict[str, List[Outcome]]] = {}
    for o in outcomes:
        if o.arm not in (treatment, control):
            continue
        per.setdefault(o.reason_class, {treatment: [], control: []})[o.arm].append(o)

    out: List[ClassContribution] = []
    for reason_class, arms in per.items():
        c, t = arms[control], arms[treatment]
        n = len(c) + len(t)
        out.append(
            ClassContribution(
                reason_class=reason_class,
                share_of_events=n / total if total else 0.0,
                default_action=DEFAULT_ACTION_BY_CLASS[reason_class],
                inert=DEFAULT_ACTION_BY_CLASS[reason_class] in INERT_ACTIONS,
                n_control=len(c),
                n_treatment=len(t),
                control_rate=(sum(1 for o in c if o.recovered) / len(c)) if c else 0.0,
                treatment_rate=(sum(1 for o in t if o.recovered) / len(t)) if t else 0.0,
            )
        )
    out.sort(key=lambda r: -abs(r.contribution))
    return out


# --------------------------------------------------------------------------
# The whole picture
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchMetrics:
    """Everything ``make demo`` prints about one batch, computed once."""

    n_events: int
    window_seconds: int
    resamples: int
    summaries: Dict[str, ArmSummary]
    contrasts: Dict[str, Contrast]
    refusal_breakdown: RefusalBreakdown
    contributions: List[ClassContribution]
    unwired_arms: Tuple[str, ...]

    @property
    def headline(self) -> Contrast:
        return self.contrasts["B-A"]

    @property
    def gross_over_claim(self) -> float:
        """How much a gross recovered-rupees headline would over-state arm B.

        Gross divided by incremental, on the value-weighted figure. PRD 3 calls a
        gross headline a category error; this is that error, in multiples.
        """
        b = self.summaries["B"]
        incremental = self.headline.intervals["value_share"].point
        if incremental <= 0.0:
            # An underpowered batch can put the point estimate below zero. The
            # ratio is then meaningless rather than large, so it is not reported.
            return float("inf")
        return b.value_share / incremental


def compute_metrics(
    outcomes: Sequence[Outcome],
    *,
    seed: int,
    window_seconds: int = OBSERVATION_WINDOW_SECONDS,
    resamples: int = bs.DEFAULT_RESAMPLES,
) -> BatchMetrics:
    summaries = {arm: summarise_arm(outcomes, arm) for arm in canonical.ARMS}
    contrasts = {
        # The headline: intent-to-treat over every event, including the ~72% the
        # policy declines to act on. This is the number that answers "does this
        # recover money", and it is deliberately the diluted one.
        "B-A": contrast(outcomes, "B", "A", seed=seed, resamples=resamples),
        # The pre-specified subgroup: only events where the rules-only policy
        # proposes a real action. Same randomisation, same window; the subset is
        # defined entirely by pre-treatment information. Reported as a subgroup,
        # never as the headline.
        "B-A actioned": contrast(
            outcomes, "B", "A", seed=seed, resamples=resamples, actionable_only=True
        ),
    }
    return BatchMetrics(
        n_events=len(outcomes),
        window_seconds=window_seconds,
        resamples=resamples,
        summaries=summaries,
        contrasts=contrasts,
        refusal_breakdown=refusals(outcomes, "B"),
        contributions=class_contributions(outcomes),
        unwired_arms=tuple(a for a in canonical.ARMS if not ARM_POLICIES[a].wired),
    )


# --------------------------------------------------------------------------
# Sensitivity to the organic-recovery assumption
# --------------------------------------------------------------------------
#
# PRD 10.2's sweep, and BUILD-PLAN Day 3 is explicit that it belongs in the
# standard output rather than an appendix: it converts "your simulator is made
# up" from an objection into a question already answered on screen.
#
# The sweep **re-derives** the headline at each level rather than rescaling it.
# That distinction is the entire value of the table. Under a per-action uplift
# table the sweep would move the organic baseline and leave the uplift untouched,
# so every row would be the same number and the table would prove nothing. Here
# the organic rate and the incremental effect are computed from the same latent
# world (`sim/latent.py`), so raising organic recovery genuinely eats the
# headroom an intervention has to work in -- which is what the 70% row is
# supposed to show.


@dataclass(frozen=True)
class SensitivityRow:
    """One organic-recovery scenario, reported two ways.

    Both columns are needed and the first draft printed only the second, which
    made the table useless. The story the sweep exists to tell is how the
    *quantity* moves as the assumption moves -- and each arm-based estimate
    carries about +/-2.5pp of its own sampling noise at 2,000 events per arm,
    which is larger than the whole range the quantity moves across. So four rows
    of estimates came out non-monotone (+1.96 / +0.53 / +2.15 / +0.92) and looked
    like a bug rather than a sensitivity analysis.

    ``true_*`` is the **estimand**: the exact effect in that scenario, computed
    from both potential outcomes with no sampling error at all. It answers "how
    much does the assumption matter", which is the question asked.

    ``*_interval`` is the **estimate**: what a randomised holdout of this size
    would have seen. It answers "could production tell these scenarios apart",
    and the honest answer is no.

    Printing only the estimand would overstate what the experiment can resolve.
    Printing only the estimate hides the trend inside the noise. Both, labelled.
    """

    target_organic: float
    achieved_organic: float
    control_rate: float
    treatment_rate: float

    #: Exact, from both potential outcomes. Simulation only.
    true_rate: float
    true_value_share: float
    true_money_paise: float

    rate_interval: bs.Interval
    money_interval: bs.Interval
    value_share_interval: bs.Interval


def _rescaled_self_recovery(factor: float):
    """``SELF_RECOVERY`` with every probability scaled, capped at 1.

    ALREADY_PAID is excluded: its probability is 1.0 because the payment has
    already been captured, which is a fact about the reason code and not a
    parameter of customer behaviour. Scaling it would be scaling a definition.
    """
    from sim.generate import SELF_RECOVERY

    out = {}
    for name, model in SELF_RECOVERY.items():
        if name == "ALREADY_PAID":
            out[name] = model
            continue
        out[name] = replace(model, probability=min(1.0, model.probability * factor))
    return out


def _organic_rate_at(events: Sequence[RiskEvent], factor: float, seed: int) -> float:
    """The batch's organic recovery rate if probabilities were scaled by factor.

    Analytic rather than re-simulated: each event's self-recovery is a Bernoulli
    draw at its class probability, so the expected rate is the mean of those
    probabilities. Using the expectation instead of a re-draw keeps the bisection
    below monotone, which a resampled estimate would not be.
    """
    scaled = _rescaled_self_recovery(factor)
    if not events:
        return 0.0
    return sum(scaled[e.reason_class].probability for e in events) / len(events)


def solve_scale_factor(
    events: Sequence[RiskEvent], target: float, seed: int
) -> Tuple[float, float]:
    """Find the scale factor whose expected organic rate hits ``target``.

    Bisection on a monotone function, 60 iterations, no derivative. Returns
    (factor, achieved_rate) and the achieved rate is reported alongside the
    target in the printed table, because the cap at 1.0 means a high target may
    be unreachable -- ALREADY_PAID is already at 1.0 and the dead classes are
    pinned at 0.0, so no factor can move them. Printing the achieved value is the
    difference between a sensitivity table and a wish.
    """
    lo, hi = 0.0, 1.0
    # Grow the upper bound until it brackets the target or saturates.
    for _ in range(40):
        if _organic_rate_at(events, hi, seed) >= target:
            break
        hi *= 2.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _organic_rate_at(events, mid, seed) < target:
            lo = mid
        else:
            hi = mid
    factor = (lo + hi) / 2.0
    return factor, _organic_rate_at(events, factor, seed)


def sensitivity_table(
    events: Sequence[RiskEvent],
    *,
    seed: int,
    window_seconds: int = OBSERVATION_WINDOW_SECONDS,
    resamples: int = bs.SENSITIVITY_RESAMPLES,
    levels: Sequence[float] = SENSITIVITY_LEVELS,
) -> List[SensitivityRow]:
    """Re-derive the headline at each organic-recovery level.

    Each row is a full re-simulation of the latent world at a rescaled
    self-recovery probability, followed by a full re-resolution of every arm. It
    is not a rescaling of the central row.
    """
    import sim.generate as generate
    from sim import latent as latent_module

    rows: List[SensitivityRow] = []
    original = generate.SELF_RECOVERY
    for target in levels:
        factor, achieved = solve_scale_factor(events, target, seed)
        try:
            # Patched at module level because `_self_recovery` and
            # `latent._clearance_median` both read the table by name. Restored in
            # the finally block -- a leaked patch here would silently change the
            # headline for every later caller in the same process.
            generate.SELF_RECOVERY = _rescaled_self_recovery(factor)
            redrawn = _redraw_self_recovery(events, seed)
            enriched = latent_module.enrich_batch(redrawn, seed)
            outcomes = resolve_batch(enriched, window_seconds=window_seconds)
            c = contrast(outcomes, "B", "A", seed=seed, resamples=resamples)
            summaries = {a: summarise_arm(outcomes, a) for a in ("A", "B")}
            # The exact effect in this scenario. Simulation only, and the reason
            # the table is legible at all -- see SensitivityRow.
            from pramaan.eval.resolve import potential_outcomes, true_effect

            truth = true_effect(
                potential_outcomes(enriched, window_seconds=window_seconds)
            )
            rows.append(
                SensitivityRow(
                    target_organic=target,
                    achieved_organic=achieved,
                    control_rate=summaries["A"].rate,
                    treatment_rate=summaries["B"].rate,
                    true_rate=truth["rate"],
                    true_value_share=truth["value_share"],
                    true_money_paise=truth["money_per_event"],
                    rate_interval=c.intervals["rate"],
                    money_interval=c.intervals["money_per_event"],
                    value_share_interval=c.intervals["value_share"],
                )
            )
        finally:
            generate.SELF_RECOVERY = original
    return rows


def _redraw_self_recovery(events: Sequence[RiskEvent], seed: int) -> List[RiskEvent]:
    """Re-draw ``self_recovers_at`` under the currently-patched table.

    Uses the same per-event derived RNG as ``sim.latent``, so the redraw is
    deterministic and independent of the generator's stream. Every event keeps
    its amount, timing, reason code and **arm** -- only the counterfactual moves,
    which is what makes the sweep a sensitivity analysis on one parameter rather
    than four different batches.
    """
    import math
    from dataclasses import replace as _replace
    from datetime import timedelta

    import sim.generate as generate
    from sim.latent import event_rng

    out: List[RiskEvent] = []
    for event in events:
        rng = event_rng(seed ^ 0x5EED, event.event_id)
        model = generate.SELF_RECOVERY[event.reason_class]
        detected = canonical.parse_iso(event.detected_at)
        if model.probability <= 0.0 or rng.random() >= model.probability:
            latent = _replace(event.latent, self_recovers_at=None)
        else:
            if model.median_lag_seconds == 0:
                lag = 0.0
            else:
                u1 = rng.random() or 1e-12
                u2 = rng.random()
                z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
                lag = math.exp(math.log(model.median_lag_seconds) + model.sigma * z)
            latent = _replace(
                event.latent,
                self_recovers_at=canonical.to_iso(
                    detected + timedelta(seconds=int(lag))
                ),
            )
        out.append(_replace(event, latent=latent))
    return out


# --------------------------------------------------------------------------
# Sensitivity to the observation window
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowRow:
    window_seconds: int
    control_rate: float
    treatment_rate: float
    rate_interval: bs.Interval
    false_intervention_rate: float
    is_headline: bool


def observation_window_sweep(
    events: Sequence[RiskEvent],
    *,
    seed: int,
    resamples: int = bs.SENSITIVITY_RESAMPLES,
    windows: Sequence[int] = OBSERVATION_WINDOW_SWEEP,
) -> List[WindowRow]:
    """The headline at each observation window. PRD 5.1's "publish the curve".

    The trade-off is genuine in both directions and the curve shows it: a short
    window censors the scheduled retry before it can fire, and a long one lets
    organic recovery swallow the incremental effect. A hand-picked constant
    invites the question; the curve answers it.
    """
    rows: List[WindowRow] = []
    for window in windows:
        outcomes = resolve_batch(events, window_seconds=window)
        c = contrast(outcomes, "B", "A", seed=seed, resamples=resamples)
        rows.append(
            WindowRow(
                window_seconds=window,
                control_rate=summarise_arm(outcomes, "A").rate,
                treatment_rate=summarise_arm(outcomes, "B").rate,
                rate_interval=c.intervals["rate"],
                false_intervention_rate=summarise_arm(
                    outcomes, "B"
                ).false_intervention_rate,
                is_headline=window == OBSERVATION_WINDOW_SECONDS,
            )
        )
    return rows


def inert_classes_are_identical(events, *, window_seconds=None) -> bool:
    """For every event whose arm-B action is inert, arm A and arm B agree.

    Checked per event rather than per arm, and that is the whole value of it. The
    per-class table shows arm A and arm B rates differing by several points on
    TECH_TRANSIENT, whose arm-B action is ACT_WAIT -- which looks alarming until
    you notice the two rates are computed over *different events*. This resolves
    the same event both ways and asserts the outcomes are identical, which is the
    claim actually being made.

    Cheap, exact, and it would catch the class of bug where an "inert" action
    quietly does something -- for example if ACT_WAIT ever gained a side effect,
    or if the resolver's inert branch stopped returning the unaided outcome
    unchanged.
    """
    from pramaan.eval.resolve import (
        OBSERVATION_WINDOW_SECONDS,
        is_actionable,
        resolve_one,
    )

    window = window_seconds or OBSERVATION_WINDOW_SECONDS
    for event in events:
        if is_actionable(event):
            continue
        a = resolve_one(event, "A", window_seconds=window)
        b = resolve_one(event, "B", window_seconds=window)
        if (a.recovered, a.recovered_at, a.cause) != (
            b.recovered,
            b.recovered_at,
            b.cause,
        ):
            return False
    return True


def _self_check() -> None:
    if SENSITIVITY_CENTRAL not in SENSITIVITY_LEVELS:
        raise AssertionError(
            "the central organic-recovery case must appear in the swept levels"
        )
    if "B" not in WIRED_ARMS or "A" not in WIRED_ARMS:
        raise AssertionError("A and B must both be wired for B-A to be a result")


_self_check()
