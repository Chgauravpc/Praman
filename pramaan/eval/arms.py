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
from typing import Dict, List, Optional, Sequence, Tuple

from pramaan import canonical, taxonomy
from pramaan.envelope import Step
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
        label="llm-planned -- investigator -> planner -> envelope",
        wired=False,
        acts=False,
        note=(
            "Present and empty. Wired on Day 5. Takes no action today, so its "
            "outcomes would be identical to arm A's -- which is exactly why "
            "metrics.py withholds its figures instead of printing them."
        ),
    ),
}

#: Arms whose numbers may be printed as results.
WIRED_ARMS: Tuple[str, ...] = tuple(
    a for a in canonical.ARMS if ARM_POLICIES[a].wired
)


def arm_step(arm: str, event: RiskEvent) -> Optional[Step]:
    """The action this arm proposes for this event, or None for no action.

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
    if acting != ["B"]:
        raise AssertionError(
            "exactly one arm acts today, and it is B: got %r. If arm C has been "
            "wired, update this check in the same commit -- an arm that acts "
            "without the check moving is an arm nobody decided to turn on."
            % acting
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
