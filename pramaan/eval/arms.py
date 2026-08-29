"""Three arms, and what each one is allowed to do.

PRD 8.1. Two independent contrasts fall out of a three-arm design, and the second
is the one nobody else will have:

- **B - A** -- does recovery work at all? The headline incremental number.
- **C - B** -- does the LLM add anything over a lookup table? The ablation.

Arm C exists in this file from Day 3, wired to nothing, and that is deliberate:
retrofitting a third arm on Day 5 would mean re-running and re-reporting
everything, and worse, it would mean designing arm C's measurement *after* seeing
arm B's result. Building the slot before the occupant is what keeps the
comparison honest.

**Arm C is unwired, and metrics.py refuses to print a number for it.** That
refusal is the point rather than a limitation. An unwired arm C takes no action,
so its outcomes are identical to arm A's by construction -- and a table printing
``C: 24.0%`` next to ``B: 26.8%`` would read as *the LLM is worse than the
table*, which is not a finding, it is an artefact of the LLM not existing yet.
The ``wired`` flag is enforced, not documented.

Three properties of the assignment, each from PRD 8.1 and each with a reason:

**Stratified on (source_type, amount_band, segment).** Order amounts are
log-normal, so a simple coin flip lets a handful of very large payments stack
into one arm by luck and swamp the money metric. Stratification is what stops the
estimate being decided by four outliers.

**Permuted blocks of three.** Within a stratum the arms cycle through a shuffled
A/B/C, so the within-stratum spread is bounded at one event by construction
rather than being balanced only in expectation. This is the property that stops a
band-5 stratum stacking into one arm.

*A correction to what Day 1 claimed here.* The original docstring said the split
is "near-exact at every prefix of the stream ... which matters for a
partially-completed batch". That is true of the **assignment sequence** and false
of what the generator emits, because ``generate`` sorts by ``detected_at`` at the
end -- so a time-ordered prefix is effectively a random subset of the assignment
order and carries no block guarantee at all. Measured: at a 300-event prefix of
the 3,000-event batch the arm spread is 31 in time order and 2 in assignment
order. Nothing analyses a truncated batch, so no published figure was affected,
but the claim was wrong and is recorded in FAILURES.md rather than quietly
reworded.

**At detection, before the window.** Assigning later would condition on
post-treatment information -- whether the payment had already self-healed -- and
that quietly destroys the experiment. The arm is recorded on the event and on its
DETECT ledger row, and is never re-derived anywhere.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from random import Random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pramaan import canonical, taxonomy
from pramaan.envelope import EnvelopeContext, Step
from pramaan.envelope.reason_map import MIN_SCHEDULED_RETRY_DELAY_SECONDS
from pramaan.sense.models import RiskEvent


class ArmAssigner:
    """Equal thirds across A/B/C, stratified, assigned at detection.

    Moved here from ``sim/generate.py`` on Day 3, where it was written on Day 1
    because the generator was the only thing that needed it. It belongs with the
    experiment, not with the simulator: in production the assigner runs on real
    detected events and the simulator does not exist.

    **The move is value-preserving and that is asserted, not asserted-in-prose.**
    ``tests/test_arms.py`` pins the dev-batch and full-batch arm vectors by
    SHA-256 against the values measured at commit ``3edae60``, before the move:
    ``e259c0a446ac7ca1`` (200 events, 68/67/65) and ``56e5b02860e48e36``
    (6,000 events, 2003/2001/1996). ADR-023 is why the test compares against a
    recorded constant rather than against this class's own output.

    Its own RNG, separate from the event stream, so that changing assignment
    logic does not shift the events themselves.
    """

    def __init__(self, seed: int) -> None:
        self.rng = Random(seed ^ 0x5A17)
        self._blocks: Dict[Tuple[str, int, str], List[str]] = {}

    def assign(self, source_type: str, amount_band: int, segment: str) -> str:
        stratum = (source_type, amount_band, segment)
        block = self._blocks.get(stratum)
        if not block:
            block = list(canonical.ARMS)
            self.rng.shuffle(block)
            self._blocks[stratum] = block
        return block.pop()


# --------------------------------------------------------------------------
# Envelope input -- shared by cli.gate_events, eval.resolve and arm C's own
# planner-envelope pass
# --------------------------------------------------------------------------
#
# Moved here from eval/resolve.py on Day 5, with its values unchanged
# (test_ledger_chain.py's pinned GATE rows are unaffected). It has to live
# somewhere lower in the import graph than resolve.py once arm C needs it:
# arm_step below builds an EnvelopeContext to judge the plan's proposed first
# step against, and resolve.py already imports arm_step from this module -- so
# arm_step needing something from resolve.py would be a cycle. Moving the
# single shared definition down here, with resolve.py importing it back, is
# what keeps the dependency one-directional.

#: What the harness assumes about the *sending infrastructure*, as distinct
#: from the event. A DLT template, a scripted AI disclosure and a scripted
#: self-identification are properties of a correctly-built sender, not
#: properties of a failed payment -- so assuming them is what makes a pass
#: measure the envelope's and the planner's judgement about timing, taxonomy
#: and tiers rather than measuring the fact that there is no channel plumbing
#: yet.
#:
#: A named constant, and printed in the output, because an assumption that
#: moves a headline count belongs on screen rather than in a comment.
SHADOW_SENDER_ASSUMPTIONS = dict(
    consent="implied",
    dlt_template_id="1207shadow",
    ai_disclosure_scripted=True,
    self_identification_scripted=True,
)


def envelope_context(event: RiskEvent) -> EnvelopeContext:
    """Build the envelope's input from an event. Event time only, no clock."""
    return EnvelopeContext(
        at=event.detected_at,
        legal_context=event.legal_context,
        source_type=event.source_type,
        reason_code=event.cause_signal,
        amount_paise=event.amount_at_risk_paise,
        counterparty_id=event.counterparty.id,
        merchant_id="acct_shadow",
        **SHADOW_SENDER_ASSUMPTIONS
    )


# --------------------------------------------------------------------------
# Arm C -- the planner. A lazily-built, run-lifetime default instance
# --------------------------------------------------------------------------
#
# One ``Planner`` per run, not one per event: the whole point of signature
# memoisation (PRD 9.1, BUILD-PLAN 1.7) is that a plan is built once per
# distinct signature and reused for every event that shares it. A fresh
# ``Planner`` per call to ``arm_step`` would rebuild -- or re-fall-back-to-
# default -- every single time, which defeats the cache before it does
# anything. ``arm_step`` accepts an explicit ``planner`` for a caller that
# wants its own instance (so it can read ``planner.stats`` and
# ``planner.newly_built`` afterwards, which is how ``pramaan.execute.runner``
# gets the numbers for its shadow-mode report); the module-level default below
# exists so that every *other* caller -- ``cli.py``'s sensitivity sweeps,
# ``metrics.py``'s window sweep, anything that touches arm C without asking
# for its own planner -- still gets memoisation rather than silently paying
# for a fresh LLM round-trip on every call.

_DEFAULT_PLANNER: Optional[Any] = None


def _default_planner() -> Any:
    global _DEFAULT_PLANNER
    if _DEFAULT_PLANNER is None:
        from pramaan.config import load_config
        from pramaan.llm.client import LLMClient
        from pramaan.plan.planner import Planner

        _DEFAULT_PLANNER = Planner(LLMClient(load_config()))
    return _DEFAULT_PLANNER


# --------------------------------------------------------------------------
# What each arm does
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmPolicy:
    """One arm's behaviour, and whether it is real yet."""

    arm: str
    label: str

    #: False means "this arm takes no action because it does not exist yet".
    #: Metrics refuse to report an unwired arm rather than reporting it as a
    #: zero-effect arm, because those two statements look identical in a table
    #: and only one of them is true.
    wired: bool

    #: True if this arm may take an action at all. Arm A is the control and never
    #: acts; that is what makes it the counterfactual.
    acts: bool

    note: str


ARM_POLICIES: Dict[str, ArmPolicy] = {
    "A": ArmPolicy(
        arm="A",
        label="control -- detect, diagnose, log, do not act",
        wired=True,
        acts=False,
        note=(
            "Still produces a full ledger trail, which is what makes the "
            "comparison auditable rather than merely asserted."
        ),
    ),
    "B": ArmPolicy(
        arm="B",
        label="rules-only -- deterministic reason-class map, no LLM",
        wired=True,
        acts=True,
        note=(
            "taxonomy.DEFAULT_ACTION_BY_CLASS plus that same table's own "
            "retry_mode. Zero LLM calls anywhere in the decision."
        ),
    ),
    "C": ArmPolicy(
        arm="C",
        label="llm-planned -- planner -> envelope",
        wired=True,
        acts=True,
        note=(
            "Day 5: pramaan.plan.planner builds a RecoveryPlan per signature "
            "(memoised), and its first step is proposed exactly like arm B's "
            "action -- judged by the same envelope, subject to the same "
            "amend/reject verdicts. diagnosis_class stays 'undiagnosed' here: "
            "the investigator (Day 4) runs once per detected *incident*, not "
            "once per ordinary event, so an event with no incident open sees "
            "the same 'undiagnosed' value arm B's world always has."
        ),
    ),
}

#: Arms whose numbers may be printed as results.
WIRED_ARMS: Tuple[str, ...] = tuple(
    a for a in canonical.ARMS if ARM_POLICIES[a].wired
)


def arm_step(
    arm: str, event: RiskEvent, *, planner: Optional[Any] = None
) -> Optional[Step]:
    """The action this arm proposes for this event, or None for no action.

    ``planner`` matters only for arm C; arms A and B ignore it. Passed
    explicitly by a caller that wants its own ``Planner`` instance (so it can
    read back stats after a run); defaults to the module-level shared instance
    otherwise, which is what keeps signature memoisation working for every
    caller that does not ask for its own.

    **Arm C returns the plan's first step, unedited, and nothing more.** The
    envelope validates it exactly where arm B's proposal is validated -- inside
    ``eval.resolve.resolve_one``'s own ``judge()`` call, which every arm's
    proposed step passes through uniformly. That symmetry is deliberate: arm C
    does not get a bespoke execution path that arm B lacks, because a
    difference in *how* a step is validated would be a confound in the C-B
    contrast, not just a difference in *what* is proposed. Steps after the
    first are the sequencing PRD 6.4 asks the plan object to carry (a wait,
    then a template, then a conditional follow-up); they are inspectable in the
    ``RecoveryPlan`` itself and in the PLAN ledger row, but the single-action
    resolution model this project measures outcomes with (``sim.outcomes``)
    has never modelled a *sequence* for any arm, so this does not regress
    anything arm B already had.

    Arm B is the deterministic table **plus the table's own scheduling**, and the
    "plus" is a decision Day 2 left open, so it is worth stating why it went this
    way.

    Day 2 measured the raw map through the envelope and found 18.2% of its
    actions came back AMEND -- all of it G7, because the map answers FUNDS with
    an *immediate* ``ACT_RETRY`` and an immediate retry on insufficient funds
    fails for exactly the reason the first attempt did. The envelope defers it 24
    hours. Two ways to read that:

    - leave arm B proposing the immediate retry, and let the envelope fix it. Arm
      B is then a table that needs a compliance layer to be sensible, and 18% of
      its actions are corrected rather than proposed.
    - have arm B carry the schedule itself.

    The second is right, and the reason is that the delay is **already in the
    taxonomy**: ``REASON_CLASS_POLICY["FUNDS"].retry_mode == "scheduled"``. Arm B
    is meant to be the strongest table the taxonomy supports, because a strawman
    arm B would make C-B flatter for a reason that has nothing to do with the
    LLM. So arm B reads its own retry_mode, and the delay comes from
    ``reason_map.MIN_SCHEDULED_RETRY_DELAY_SECONDS`` -- the same constant the
    envelope would have amended it to, not a second number that could drift.

    The Day 2 figure is *not* restated as though it had changed: ``make demo``
    still gates the raw map and still prints 18.2% AMEND for it, labelled as the
    raw map, alongside arm B's post-scheduling verdicts. Both are true, they
    measure different things, and quietly replacing the first with the second
    would be exactly the sort of number-that-moved-without-a-diff this project
    keeps failing itself for.
    """
    policy = ARM_POLICIES[arm]
    if not policy.acts:
        return None

    if arm == "C":
        planner = planner or _default_planner()
        features = event.canonical_features()
        plan = planner.plan_for(features, situation_ts=event.detected_at)
        if not plan.steps:
            return None
        first = plan.steps[0]
        return Step(
            action=first.action,
            channel=first.channel,
            delay_seconds=first.delay_seconds,
            expected_value_paise=first.expected_value_paise,
            cost_paise=first.cost_paise,
        )

    action = taxonomy.default_action(event.cause_signal)
    delay = 0
    if action == "ACT_RETRY":
        mode = taxonomy.REASON_CLASS_POLICY[event.reason_class].retry_mode
        if mode == "scheduled":
            delay = MIN_SCHEDULED_RETRY_DELAY_SECONDS
    channel = DEFAULT_CHANNEL.get(action, "none")
    return Step(action=action, channel=channel, delay_seconds=delay)


#: Which channel each action would use. The envelope judges (action, channel), so
#: sending "none" for everything would never exercise the window at all. Shared
#: with cli.gate_events by import rather than by duplication.
DEFAULT_CHANNEL: Dict[str, str] = {"ACT_MESSAGE": "sms", "ACT_VOICE": "voice"}


# --------------------------------------------------------------------------
# Balance
# --------------------------------------------------------------------------


def arm_counts(events: Sequence[RiskEvent]) -> Dict[str, int]:
    counts = {arm: 0 for arm in canonical.ARMS}
    for event in events:
        counts[event.arm] += 1
    return counts


def max_imbalance(events: Sequence[RiskEvent]) -> float:
    """Largest relative deviation from an equal third. 0.0 is perfect."""
    if not events:
        return 0.0
    expected = len(events) / len(canonical.ARMS)
    counts = arm_counts(events)
    return max(abs(n - expected) / expected for n in counts.values())


def stratum_imbalance(events: Sequence[RiskEvent]) -> int:
    """Largest within-stratum arm-count spread.

    The headline balance figure can look perfect while a single stratum is
    lopsided, and it is the strata that carry the heavy tail -- so this is the
    number that says whether stratification actually did its job. Permuted blocks
    of three bound it at 1 by construction, and a value above 1 means the block
    logic is broken rather than merely unlucky.
    """
    per: Dict[Tuple[str, int, str], Dict[str, int]] = {}
    for event in events:
        key = (event.source_type, event.amount_band, event.counterparty.segment)
        cell = per.setdefault(key, {a: 0 for a in canonical.ARMS})
        cell[event.arm] += 1
    if not per:
        return 0
    return max(max(c.values()) - min(c.values()) for c in per.values())


# --------------------------------------------------------------------------
# Power -- so the batch size is justified rather than picked
# --------------------------------------------------------------------------
#
# PRD 8.1 states the formula and three worked rows. Computing them here rather
# than transcribing them means the printed table cannot disagree with the
# arithmetic, which is the same discipline ADR-021 applies to every other count a
# reviewer could check.

#: Two-sided alpha = 0.05 -> 1.959964; power = 0.80 -> 0.841621.
Z_ALPHA_TWO_SIDED_95 = 1.959963984540054
Z_POWER_80 = 0.8416212335729143

#: The organic recovery rate PRD 8.1 uses as the baseline for its power
#: arithmetic. **Event-weighted** -- a proportion of events, not of rupees. PRD
#: 10.2 is explicit that conflating the two is a defect, and this is the one that
#: belongs in a two-proportion power calculation.
POWER_BASELINE_RATE = 0.30


def n_per_arm(delta: float, p_bar: float = POWER_BASELINE_RATE) -> int:
    """Events per arm to detect ``delta`` at alpha=0.05, 80% power.

    ``n = 2 * (z_alpha + z_power)^2 * p_bar * (1 - p_bar) / delta^2``
    """
    if delta <= 0:
        raise ValueError("delta must be positive")
    z = Z_ALPHA_TWO_SIDED_95 + Z_POWER_80
    return int(math.ceil(2.0 * z * z * p_bar * (1.0 - p_bar) / (delta * delta)))


def min_detectable_effect(per_arm: int, p_bar: float = POWER_BASELINE_RATE) -> float:
    """The smallest lift this many events per arm can detect. The inverse.

    This is the number that matters for the dev batch, and it is unflattering:
    67 events per arm cannot detect anything a recovery system would plausibly
    produce. Printing it next to the dev-batch estimate is what stops a
    confidence interval spanning zero from being read as a failure of the
    interventions rather than a failure of the sample size.
    """
    if per_arm <= 0:
        return float("inf")
    z = Z_ALPHA_TWO_SIDED_95 + Z_POWER_80
    return math.sqrt(2.0 * z * z * p_bar * (1.0 - p_bar) / per_arm)


#: The rows PRD 8.1 publishes.
POWER_ROWS: Tuple[float, ...] = (0.10, 0.05, 0.02)


def power_table(p_bar: float = POWER_BASELINE_RATE):
    """[(lift, per_arm, total_three_arms)]. Three arms, so total is 3x."""
    return [
        (delta, n_per_arm(delta, p_bar), 3 * n_per_arm(delta, p_bar))
        for delta in POWER_ROWS
    ]


def _self_check() -> None:
    if set(ARM_POLICIES) != set(canonical.ARMS):
        raise AssertionError("one policy per arm, no more and no fewer")
    if [p.arm for p in ARM_POLICIES.values()] != list(canonical.ARMS):
        raise AssertionError("ARM_POLICIES keys must match their own arm fields")
    acting = [a for a, p in ARM_POLICIES.items() if p.acts]
    if acting != ["B", "C"]:
        raise AssertionError(
            "B and C act today, and only they: got %r. This check exists so "
            "that an arm gains the ability to move money only in the same "
            "commit that updates it -- an arm that acts without the check "
            "moving is an arm nobody decided to turn on." % acting
        )
    if ARM_POLICIES["A"].acts:
        raise AssertionError("arm A is the control and must never act")
    # PRD 8.1's published rows, recomputed rather than transcribed.
    #
    # Two of the three match exactly. The 2pp row does not: the exact value is
    # 8,241.32, PRD 8.1 publishes **8,241**, and this function returns **8,242**.
    # The difference is round() versus ceil(), and ceil is the correct one -- a
    # sample size is a floor requirement, so 8,241.32 events means 8,242 events.
    # Recorded in FAILURES.md rather than smoothed over; the magnitude is
    # irrelevant and the discipline is not, because this is the same class of
    # error as the compliance count that was wrong at the end of Day 2.
    expected = {0.10: 330, 0.05: 1319, 0.02: 8242}
    for delta, want in expected.items():
        got = n_per_arm(delta)
        if got != want:
            raise AssertionError(
                "a %.0fpp lift needs %d events per arm by the PRD 8.1 formula; "
                "this implementation gives %d" % (delta * 100, want, got)
            )


_self_check()
