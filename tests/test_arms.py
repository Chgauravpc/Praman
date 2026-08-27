"""Arm assignment: three arms, balanced, unchanged by the Day 3 refactor.

The assignment is the only thing the randomisation guarantee attaches to. If it
is not balanced, the heavy-tailed money metric is decided by luck; if it is
re-derived anywhere, the analysis and the assignment can disagree; and if arm C
is missing, Day 5 has to re-run everything.
"""
from __future__ import annotations

import pytest

from pramaan import canonical
from pramaan.eval import arms
from sim import generate as sim

# --------------------------------------------------------------------------
# The refactor was value-preserving. Pinned, not asserted in prose.
# --------------------------------------------------------------------------
#
# ``ArmAssigner`` moved from sim/generate.py to pramaan/eval/arms.py on Day 3.
# These hashes were measured at commit 3edae60, *before* the move, by running
# Day 2's own code. ADR-023 forbids a test that compares a function to its own
# delegate -- such a test cannot fail -- so the comparison is against recorded
# constants instead.
#
# If a change is ever made that legitimately moves assignment, these values
# change too, and the diff is the record that somebody decided to. Every number
# the project publishes about arms (2003/2001/1996 on the full batch) is
# downstream of them.

DAY2_DEV_ARM_HASH = "e259c0a446ac7ca1"
DAY2_DEV_ARM_COUNTS = {"A": 68, "B": 67, "C": 65}

DAY2_FULL_ARM_HASH = "56e5b02860e48e36"
DAY2_FULL_ARM_COUNTS = {"A": 2003, "B": 2001, "C": 1996}


def _arm_vector(events) -> str:
    return "".join(e.arm for e in events)


def test_the_dev_batch_arm_vector_is_unchanged_by_the_move():
    events = sim.dev_batch(42)
    digest = canonical.sha256_hex(_arm_vector(events))[:16]
    assert digest == DAY2_DEV_ARM_HASH, (
        "the dev-batch arm assignment changed. Measured at commit 3edae60 as %s, "
        "now %s. Every arm figure the project publishes moves with this."
        % (DAY2_DEV_ARM_HASH, digest)
    )
    assert arms.arm_counts(events) == DAY2_DEV_ARM_COUNTS


def test_the_full_batch_arm_vector_is_unchanged_by_the_move():
    events = sim.full_batch(42)
    digest = canonical.sha256_hex(_arm_vector(events))[:16]
    assert digest == DAY2_FULL_ARM_HASH
    assert arms.arm_counts(events) == DAY2_FULL_ARM_COUNTS


# --------------------------------------------------------------------------
# Balance
# --------------------------------------------------------------------------


@pytest.mark.parametrize("n", [200, 1_000, 6_000])
def test_arms_are_balanced_to_within_five_percent(n):
    events = sim.generate(n, seed=42)
    assert arms.max_imbalance(events) < 0.05


@pytest.mark.parametrize("n", [200, 1_000, 6_000])
def test_every_stratum_is_balanced_to_within_one_event(n):
    """The check the headline balance figure cannot make.

    Overall balance can look perfect while a single stratum is lopsided, and it
    is the strata that carry the heavy tail -- so a band-5 stratum stacked into
    one arm would swamp the money metric while the top-line count stayed at
    33.3%. Permuted blocks of three bound the within-stratum spread at 1 by
    construction, so a value above 1 means the block logic is broken rather than
    merely unlucky.
    """
    events = sim.generate(n, seed=42)
    assert arms.stratum_imbalance(events) <= 1


@pytest.mark.parametrize("seed", [1, 7, 42, 99, 12345])
def test_balance_does_not_depend_on_the_seed(seed):
    events = sim.generate(1_000, seed=seed)
    assert arms.max_imbalance(events) < 0.05
    assert arms.stratum_imbalance(events) <= 1


def test_the_split_is_near_exact_in_assignment_order():
    """Permuted blocks bound the spread in the order arms were *assigned*.

    This test replaced one that asserted the same property of the generator's
    emitted order, and failed. The reason is worth keeping, because the original
    claim came from Day 1's docstring and was simply wrong:

    ``generate`` sorts events by ``detected_at`` before returning them, so a
    time-ordered prefix is effectively a random subset of the assignment order and
    inherits no block guarantee. Measured on the 3,000-event batch at a 300-event
    prefix: spread 31 in time order, spread 2 in assignment order.

    ``event_id`` is ``evt_<seed>_<index>`` with a zero-padded index, so sorting by
    it recovers the assignment order exactly. Nothing in the project analyses a
    truncated batch, so no published figure depended on the false claim -- but the
    docstring asserting it has been corrected and the episode is in FAILURES.md.
    """
    events = sorted(sim.generate(3_000, seed=42), key=lambda e: e.event_id)
    for cut in (300, 900, 1_500, 3_000):
        counts = arms.arm_counts(events[:cut])
        spread = max(counts.values()) - min(counts.values())
        # One open permuted block per stratum can leave an arm short, and the
        # strata are (source_type x amount_band x segment) -- 15 of them for a
        # payments-only batch. So the bound is small and constant, not a fraction
        # of the prefix.
        assert spread <= 15, (
            "arm spread %d at a %d-event prefix of the assignment order; "
            "permuted blocks should bound this by roughly the stratum count"
            % (spread, cut)
        )


def test_time_ordered_prefixes_converge_even_though_they_are_not_bounded():
    """The honest version of the property for the order actually emitted.

    A time-ordered prefix has no block guarantee, but it must still converge to
    equal thirds as the prefix grows -- that is just the law of large numbers, and
    a violation would mean assignment correlates with detection time, which would
    be a real confound rather than a documentation slip.
    """
    events = sim.generate(3_000, seed=42)
    shares = []
    for cut in (300, 1_500, 3_000):
        counts = arms.arm_counts(events[:cut])
        shares.append((max(counts.values()) - min(counts.values())) / cut)
    assert shares[-1] < shares[0], (
        "time-ordered arm imbalance did not shrink with the prefix: %r. If "
        "assignment correlated with detection time, the arms would differ "
        "systematically in hour-of-day and the experiment would be confounded."
        % shares
    )
    assert shares[-1] < 0.02


# --------------------------------------------------------------------------
# Arm C: present and empty
# --------------------------------------------------------------------------


def test_arm_c_exists_and_holds_events():
    events = sim.dev_batch(42)
    assert arms.arm_counts(events)["C"] > 0


def test_arm_c_is_not_wired_and_takes_no_action():
    assert arms.ARM_POLICIES["C"].wired is False
    assert arms.ARM_POLICIES["C"].acts is False
    for event in sim.dev_batch(42):
        assert arms.arm_step("C", event) is None


def test_arm_a_never_acts():
    """The control is the counterfactual. If it acts, there is no experiment."""
    assert arms.ARM_POLICIES["A"].acts is False
    for event in sim.dev_batch(42):
        assert arms.arm_step("A", event) is None


def test_only_arm_b_acts_today():
    acting = [a for a, p in arms.ARM_POLICIES.items() if p.acts]
    assert acting == ["B"]


def test_an_unwired_arm_cannot_be_contrasted():
    """metrics.contrast refuses arm C rather than reporting arm A's numbers.

    An unwired arm takes no action, so its outcomes are identical to the
    control's. A contrast against it would print 0.00pp and read as "the LLM adds
    nothing" -- a finding about the calendar dressed as a finding about the model.
    """
    from pramaan.eval import metrics as M
    from pramaan.eval import resolve as R

    outcomes = R.resolve_batch(sim.dev_batch(42))
    with pytest.raises(ValueError, match="not wired"):
        M.contrast(outcomes, "C", "A", seed=1, resamples=100)


# --------------------------------------------------------------------------
# Arm B's policy
# --------------------------------------------------------------------------


def test_arm_b_carries_its_own_scheduled_retry_delay():
    """FUNDS gets a deferred retry from arm B, not an amendment from the envelope.

    Day 2 measured the raw reason-class map and found 18.2% of its actions came
    back AMEND, all of it G7: the map proposes an *immediate* retry on
    insufficient funds and the envelope defers it 24 hours. Arm B now carries that
    delay itself, taken from the taxonomy's own ``retry_mode`` rather than from a
    second constant, so arm B is the strongest table the taxonomy supports rather
    than one that needs a compliance layer to be sensible.

    This matters for fairness of the Day 5 ablation: a strawman arm B would make
    C-B look better for a reason that has nothing to do with the LLM.
    """
    from pramaan.envelope.reason_map import MIN_SCHEDULED_RETRY_DELAY_SECONDS

    funds = [e for e in sim.full_batch(42) if e.reason_class == "FUNDS"]
    assert funds, "the batch should contain FUNDS events"
    for event in funds[:50]:
        step = arms.arm_step("B", event)
        assert step.action == "ACT_RETRY"
        assert step.delay_seconds >= MIN_SCHEDULED_RETRY_DELAY_SECONDS


def test_arm_b_uses_the_published_default_action_map():
    """Arm B is the taxonomy's table, not a second copy of it."""
    from pramaan import taxonomy

    for event in sim.dev_batch(42):
        step = arms.arm_step("B", event)
        assert step.action == taxonomy.DEFAULT_ACTION_BY_CLASS[event.reason_class]


# --------------------------------------------------------------------------
# Power
# --------------------------------------------------------------------------


def test_the_power_table_matches_the_prd_formula():
    """PRD 8.1's rows, recomputed rather than transcribed.

    Two of the three match the PRD exactly. The 2pp row does not: the exact value
    is 8,241.32, the PRD publishes 8,241, and this returns 8,242 because a sample
    size is a floor requirement and must be rounded up. Recorded in FAILURES.md.
    """
    assert arms.n_per_arm(0.10) == 330
    assert arms.n_per_arm(0.05) == 1_319
    assert arms.n_per_arm(0.02) == 8_242


def test_n_per_arm_and_min_detectable_effect_are_inverses():
    for delta in (0.02, 0.05, 0.10, 0.20):
        per_arm = arms.n_per_arm(delta)
        recovered = arms.min_detectable_effect(per_arm)
        # Ceiling on n makes the round trip slightly conservative, never optimistic.
        assert recovered <= delta + 1e-9


def test_the_full_batch_clears_the_five_point_requirement():
    """PRD 8.1 chose 6,000 events from this calculation. Checked, not assumed."""
    per_arm = sim.FULL_BATCH_SIZE // 3
    assert per_arm >= arms.n_per_arm(0.05)


def test_the_dev_batch_cannot_detect_a_plausible_effect():
    """Stated on screen by the demo, and asserted here so it stays true.

    The dev batch's interval spanning zero is a fact about 67 events per arm, not
    about the interventions, and the demo says so. This pins the claim: if the dev
    batch ever became large enough to resolve a 10pp effect, that sentence in the
    output would be wrong and would need removing.
    """
    per_arm = sim.DEV_BATCH_SIZE // 3
    assert arms.min_detectable_effect(per_arm) > 0.10


def test_power_requires_a_positive_effect_size():
    with pytest.raises(ValueError):
        arms.n_per_arm(0.0)
    with pytest.raises(ValueError):
        arms.n_per_arm(-0.05)
