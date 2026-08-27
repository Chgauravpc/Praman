"""The latent world: structural invariants, and the calibration that is defended.

``sim/latent.py`` is where every invented number in the measurement path lives,
so it is where a reviewer should look first and where the tests have to be
strongest. Two kinds of check here, and the distinction matters:

**Structural invariants.** Things that must hold for the world to be coherent at
all -- you cannot pay before you are able to, everyone who paid unaided wanted to
pay. These are not calibration choices and a violation is a bug.

**The calibration shape.** The one **[A]**-graded anchor available is Razorpay's
webhook documentation: ``payment.failed`` is frequently followed by
``payment.captured`` because customers correct a wrong UPI PIN and retry inside
their own banking app. That fixes AUTH_DROPOFF as fast and customer-driven with no
block to clear, and FUNDS by contrast as slow and block-driven. The *magnitudes*
are judgement (**[C]**); the *shape* is anchored, so the shape is pinned here and
the reasoning is written out in EVALUATION.md.
"""
from __future__ import annotations

import pytest

from pramaan import canonical
from sim import generate as sim
from sim import latent as L

BATCH = 2_000


@pytest.fixture(scope="module")
def events():
    return sim.generate(BATCH, seed=42)


# --------------------------------------------------------------------------
# Structural invariants
# --------------------------------------------------------------------------


def test_capability_never_clears_after_the_customer_already_paid(events):
    """You cannot complete a payment before the block on it has cleared.

    The load-bearing consistency condition between the Day 1 latent
    (``self_recovers_at``) and the Day 3 ones. If it were violated, an event could
    have paid unaided at 10:00 while its blocking condition cleared at 14:00, and
    a retry at 12:00 would then be scored as impossible for an event that had
    demonstrably already succeeded.
    """
    for event in events:
        latent = event.latent
        if latent.self_recovers_at is None or latent.capability_clears_at is None:
            continue
        assert canonical.parse_iso(
            latent.capability_clears_at
        ) <= canonical.parse_iso(latent.self_recovers_at), (
            "event %s has capability clearing after it had already been paid"
            % event.event_id
        )


def test_everyone_who_paid_unaided_had_intent(events):
    """Intent is implied by the act. Nobody pays a bill they do not want to pay.

    Matters because a message can only convert someone with intent, so an event
    that self-recovered but was marked intent-less would make a message look
    ineffective on exactly the population most likely to respond.
    """
    for event in events:
        if event.latent.self_recovers_at is not None:
            assert event.latent.has_intent, event.event_id


def test_capability_is_present_whenever_the_customer_paid_unaided(events):
    for event in events:
        if event.latent.self_recovers_at is not None:
            assert event.latent.capability_ever_clears, event.event_id


def test_dead_classes_never_clear_and_never_recover(events):
    """MERCHANT_CONFIG, INTEGRATION_BUG and RISK do not heal on their own.

    A risk decline retried is how a merchant gets penalised, and a misconfigured
    MCC does not fix itself. If either ever showed capability clearing, arm B
    would harvest recoveries from classes where the correct action is to alert a
    human -- and the headline would be built on them.
    """
    for event in events:
        if event.reason_class in ("MERCHANT_CONFIG", "INTEGRATION_BUG", "RISK"):
            assert event.latent.self_recovers_at is None, event.event_id
            assert not event.latent.capability_ever_clears, event.event_id


def test_already_paid_events_have_already_recovered(events):
    """ALREADY_PAID recovers at t=0 in every arm, so it cancels in B-A.

    It inflates both arms' absolute levels by its share of volume (~0.4%) and
    contributes exactly nothing to the contrast, which is the correct treatment:
    the money was never at risk. S1 terminates the thread.
    """
    for event in events:
        if event.reason_class == "ALREADY_PAID":
            assert event.latent.self_recovers_at == event.detected_at


# --------------------------------------------------------------------------
# The calibration shape -- the [A]-graded anchor
# --------------------------------------------------------------------------


def test_auth_dropoff_has_no_block_to_clear():
    """The anchor, asserted. Razorpay's webhook docs describe this exact case.

    A wrong UPI PIN is not a block: the instrument works, the money is there, and
    the customer is at that moment fixing it inside their own app. So
    ``blocking_share`` is 0.0 -- the entire observed delay is the human -- and
    capability is present from the first second.

    The consequence that matters is that a merchant-initiated retry cannot help,
    because it cannot supply a PIN. Which is exactly why the taxonomy answers
    AUTH_DROPOFF with ACT_WAIT. The world model and the policy were derived
    independently and agree, and this test is what keeps them agreeing.
    """
    world = L.WORLD["AUTH_DROPOFF"]
    assert world.blocking_share == 0.0
    assert world.retry_needs_customer is True
    assert world.p_capability_clears_unrecovered == 1.0
    # A payment link hands the authentication opportunity back, so a message can.
    assert world.message_clears_block is True


def test_auth_dropoff_capability_is_immediate(events):
    """blocking_share 0.0 must actually produce capability at detection time."""
    seen = 0
    for event in events:
        if event.reason_class != "AUTH_DROPOFF":
            continue
        if event.latent.self_recovers_at is None:
            continue
        seen += 1
        assert event.latent.capability_clears_at == event.detected_at
    assert seen > 20, "not enough AUTH_DROPOFF self-recoveries to check"


def test_funds_is_block_driven_where_auth_dropoff_is_human_driven():
    """The contrast IS the calibration, so it is asserted rather than described."""
    assert L.WORLD["FUNDS"].blocking_share > 0.5
    assert L.WORLD["FUNDS"].blocking_share > L.WORLD["AUTH_DROPOFF"].blocking_share
    # And money is not conjured by asking for it.
    assert L.WORLD["FUNDS"].message_clears_block is False
    # But a merchant-initiated debit does not need the customer present.
    assert L.WORLD["FUNDS"].retry_needs_customer is False


def test_the_self_recovery_hazard_decays_fast_for_auth_and_slowly_for_funds():
    """AUTH_DROPOFF in minutes, FUNDS in days. Pinned as an order-of-magnitude gap.

    BUILD-PLAN Day 3 states this as the calibration to write down and defend. A
    factor of 100 is a deliberately loose bound: the point is that these are
    different regimes -- an app retry versus a credit cycle -- not that either
    median is exactly right.
    """
    auth = sim.SELF_RECOVERY["AUTH_DROPOFF"]
    funds = sim.SELF_RECOVERY["FUNDS"]
    assert auth.median_lag_seconds < 3_600, "AUTH_DROPOFF should recover in minutes"
    assert funds.median_lag_seconds > 24 * 3_600, "FUNDS should take days"
    assert funds.median_lag_seconds > 100 * auth.median_lag_seconds
    # And AUTH_DROPOFF is the highest-probability class, per PRD Appendix A.
    assert auth.probability == max(
        m.probability
        for name, m in sim.SELF_RECOVERY.items()
        if name != "ALREADY_PAID"
    )


def test_routing_only_helps_where_the_block_is_rail_specific():
    """A rail switch dodges a per-rail cap or one degraded bank, nothing else.

    If ``p_route_clears`` were non-zero for FUNDS, arm B would recover
    insufficient-funds failures by changing rails, which is not a thing that
    happens and would put a fabricated effect straight into the headline.
    """
    for name, world in L.WORLD.items():
        if name in ("TECH_TRANSIENT", "LIMIT"):
            assert world.p_route_clears > 0.0, name
        else:
            assert world.p_route_clears == 0.0, (
                "%s has non-zero route success; routing should only help where "
                "the block is rail-specific" % name
            )


def test_every_reason_class_has_a_world_model():
    from pramaan.taxonomy import REASON_CLASSES

    assert set(L.WORLD) == set(REASON_CLASSES)


def test_intent_is_at_least_as_common_as_self_recovery(events):
    """More people want to pay than actually get round to it.

    The gap between them is the entire population a reminder can convert. If it
    were zero or negative, messaging would have no mechanism at all and the
    INSTRUMENT_DEAD and ELIGIBILITY lift would have to be coming from somewhere
    else.
    """
    n = len(events)
    self_recovering = sum(1 for e in events if e.latent.self_recovers) / n
    with_intent = sum(1 for e in events if e.latent.has_intent) / n
    assert with_intent > self_recovering
    # And capability is more common still: the money is there more often than
    # anyone comes back for it, which is what a silent retry harvests.
    with_capability = sum(1 for e in events if e.latent.capability_ever_clears) / n
    assert with_capability > with_intent


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_enrichment_is_deterministic(events):
    for event in events[:200]:
        assert L.enrich(event, 42) == event.latent


def test_the_per_event_rng_is_independent_of_position():
    """Keyed on event_id, so the generator's final sort cannot move a draw.

    This is what lets the Day 3 latents be added without shifting the Day 1
    stream -- and therefore without moving the golden ledger, the 22.0x
    memoisation ratio or the 4,905/1,095 envelope tally.
    """
    events = sim.generate(400, seed=42)
    shuffled = list(reversed(events))
    for event in shuffled[:100]:
        assert L.enrich(event, 42) == event.latent


def test_a_different_seed_gives_different_latents():
    events = sim.generate(300, seed=42)
    differences = sum(1 for e in events if L.enrich(e, 43) != e.latent)
    assert differences > 50, (
        "changing the seed barely moved the latents, so the seed is probably not "
        "reaching the per-event RNG"
    )


def test_enrich_refuses_an_event_with_no_latent_truth():
    from dataclasses import replace

    event = replace(sim.generate(1, seed=42)[0], latent=None)
    with pytest.raises(ValueError, match="self_recovers_at"):
        L.enrich(event, 42)


def test_generate_always_enriches():
    """There is no unenriched path, and that is a safety property.

    An event whose ``capability_clears_at`` is None reads to the oracle as "this
    block never clears", so every intervention on it fails and the incremental
    figure comes out at zero -- silently, with nothing raising. A wrong number
    that looks like a real one is the worst available failure, so enrichment
    happens inside ``generate`` rather than being left to a caller to remember.
    """
    for event in sim.generate(50, seed=42):
        assert event.latent is not None
        # has_intent defaults False, so it alone cannot prove enrichment ran.
        # capability_clears_at being set for a self-recovering event can.
        if event.latent.self_recovers:
            assert event.latent.capability_clears_at is not None
