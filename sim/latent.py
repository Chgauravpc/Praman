"""The latent world -- what an intervention actually interacts with.

Simulator v0 (``sim/generate.py``) gave every event one latent:
``self_recovers_at``, the time the customer would have completed the payment
unaided. That is enough to measure **arm A**, and not enough to measure arm B,
because it says nothing about *why* the payment failed to complete or what an
intervention would change.

The cheap way to close that gap is a per-action uplift table::

    P(recover | ACT_RETRY, FUNDS) = organic_rate + 0.12   # <- worthless

It is worthless in a specific and fatal way: the headline incremental number
then *is* the 0.12, restated. Sweeping the organic rate around it does not help,
because the uplift was never derived from the organic rate. A reviewer asking
"where does 0.12 come from" has no answer, and they are right to ask.

So this module models the world instead, and lets the uplift fall out. Two
quantities, and the split between them is the whole idea:

**Capability.** When does the *blocking condition* stop blocking? The balance
arrives, the bank comes back, the daily cap rolls over. Capability is
indifferent to whether the customer does anything.

**Intent.** Does the customer still want this payment to complete? Intent is
indifferent to whether the money is there.

A failed payment completes when capability and intent coincide. And the two
interventions available reach exactly one each:

- a **merchant-initiated retry** supplies intent (the merchant's) and needs
  capability -- so it converts *capability without intent*;
- a **message** supplies a reminder and needs capability -- so it converts
  *intent that never got round to it*.

Which yields the decomposition this module rests on. If a customer paid unaided
at time tau, then the block cleared at some point at-or-before tau and the rest
of the delay was them noticing. Write that split as ``blocking_share`` -- the
fraction of the observed self-recovery delay that was the block rather than the
customer -- and a retry's value becomes precisely the noticing lag it skips.
Nothing was assumed about the size of the effect.

**What this buys, concretely.** The organic-recovery sensitivity sweep (PRD 10.2,
printed by ``make demo``) re-derives the headline at 15/30/50/70% organic
recovery, and the answer moves *non-proportionally*, because capability and
intent respond differently to the sweep. Under an uplift table the same sweep
would just rescale a constant, and would prove nothing at all.

**Determinism.** Every draw here comes from a per-event RNG seeded from
``sha256(seed | event_id)``, never from the generator's own stream. That is not
style: ``sim/generate.py`` consumes a fixed number of draws per event from one
shared ``Random``, so taking even one extra draw there would shift every
subsequent event's reason code and amount, moving the Day 1 golden ledger, the
published 22.0x memoisation ratio and the 4,905/1,095 envelope tally -- for
nothing. Seeding per event costs a hash and leaves all of it byte-identical.
See ADR-024.

**Grades.** ``blocking_share``, the conditional clearance probabilities and the
response rates are **[C]** -- judgement, not measurement. What is **[A]** is the
*shape*: Razorpay's webhook documentation warns that ``payment.failed`` is
frequently followed by ``payment.captured`` because customers correct a wrong UPI
PIN and retry inside their own banking app. That single documented fact fixes
AUTH_DROPOFF as a fast-decaying, customer-driven hazard with no block to clear,
and by contrast FUNDS as a slow, block-driven one. ``EVALUATION.md`` states the
calibration and its reasoning in full, and ``tests/test_latent_world.py`` pins
the shape so it cannot drift quietly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import timedelta
from random import Random
from typing import Dict, List, Optional, Sequence

from pramaan import canonical
from pramaan.sense.models import LatentTruth, RiskEvent


@dataclass(frozen=True)
class WorldModel:
    """How one reason class responds to the two things an agent can do.

    Six numbers per class. Every one of them is a claim about the world rather
    than about the size of the effect, which is the property that makes the
    resulting estimate worth printing.
    """

    #: Fraction of an observed self-recovery delay that was the *block*, the rest
    #: being the customer noticing. 0.0 means nothing was ever blocked and the
    #: whole delay was human; 1.0 means the customer acted the instant the block
    #: cleared. This is the parameter a retry monetises: it skips the
    #: (1 - blocking_share) part and nothing else.
    blocking_share: float

    #: Can a merchant-initiated charge succeed with the customer absent? False
    #: wherever the failure was an authentication, or an instrument the customer
    #: must re-supply -- no schedule fixes a mistyped PIN.
    retry_needs_customer: bool

    #: P(the block clears at all | the customer never paid unaided). The
    #: population a silent retry exists to reach: money arrived, nobody came
    #: back.
    p_capability_clears_unrecovered: float

    #: P(the customer still wants to pay | they never paid unaided). The
    #: population a message exists to reach.
    p_intent_unrecovered: float

    #: P(a rail switch clears this block). Non-zero only where the block is
    #: rail-specific: a per-rail cap, one degraded bank.
    p_route_clears: float

    #: Does a message *itself* remove the block? True where the block is "the
    #: customer has not done the thing yet" -- re-enter a PIN, re-add a card,
    #: choose another method. False where the block is money or a merchant
    #: defect, which no amount of asking will move.
    message_clears_block: bool


#: The ten reason classes. Asserted against ``taxonomy.REASON_CLASSES`` at
#: import: a class added to the taxonomy without a world model here would
#: otherwise resolve as silently inert, and inert means "arm B never helps",
#: which is a number rather than an error.
WORLD: Dict[str, WorldModel] = {
    # The bank is degraded, not the customer. The outage is nearly all of the
    # delay, it nearly always ends, and it is rail-specific -- which is the
    # entire premise of smart routing.
    "TECH_TRANSIENT": WorldModel(
        blocking_share=0.85,
        retry_needs_customer=False,
        p_capability_clears_unrecovered=0.90,
        p_intent_unrecovered=0.55,
        p_route_clears=0.45,
        message_clears_block=False,
    ),
    # The anchor class. Razorpay's webhook docs [A]: the customer corrects the
    # PIN inside their own app. So there is *no block* -- blocking_share 0.0, and
    # capability is present from the first second. What is missing is a completed
    # authentication, which only the customer can supply: retry_needs_customer.
    # A payment link, by contrast, hands the authentication opportunity back, so
    # a message does clear it.
    "AUTH_DROPOFF": WorldModel(
        blocking_share=0.00,
        retry_needs_customer=True,
        p_capability_clears_unrecovered=1.00,
        p_intent_unrecovered=0.35,
        p_route_clears=0.00,
        message_clears_block=True,
    ),
    # Waiting for money. Almost all of the delay is the balance arriving, and
    # they pay soon after -- so a retry timed to the credit cycle captures very
    # little noticing lag but a great deal of the population that never
    # returned. A message cannot conjure a balance: message_clears_block False.
    "FUNDS": WorldModel(
        blocking_share=0.90,
        retry_needs_customer=False,
        p_capability_clears_unrecovered=0.55,
        p_intent_unrecovered=0.45,
        p_route_clears=0.00,
        message_clears_block=False,
    ),
    # A cap. It rolls over on a date boundary, and a *different rail* has a
    # different cap -- which is why LIMIT is the one class where routing is
    # worth more than waiting.
    "LIMIT": WorldModel(
        blocking_share=0.80,
        retry_needs_customer=False,
        p_capability_clears_unrecovered=0.85,
        p_intent_unrecovered=0.40,
        p_route_clears=0.60,
        message_clears_block=True,
    ),
    # The instrument is dead. Clearance *is* the customer re-adding one, so
    # blocking_share 1.0 and retry_needs_customer both hold, and they are not in
    # tension: the block only ever clears by a customer action, so a retry on the
    # dead instrument cannot win and a message can.
    "INSTRUMENT_DEAD": WorldModel(
        blocking_share=1.00,
        retry_needs_customer=True,
        p_capability_clears_unrecovered=0.10,
        p_intent_unrecovered=0.30,
        p_route_clears=0.00,
        message_clears_block=True,
    ),
    # The merchant's own configuration. Alerting them fixes the *next* payment,
    # not this one -- see resolve.py, which counts that as a declared externality
    # rather than folding it into the headline.
    "MERCHANT_CONFIG": WorldModel(
        blocking_share=1.00,
        retry_needs_customer=True,
        p_capability_clears_unrecovered=0.00,
        p_intent_unrecovered=0.50,
        p_route_clears=0.00,
        message_clears_block=False,
    ),
    "INTEGRATION_BUG": WorldModel(
        blocking_share=1.00,
        retry_needs_customer=True,
        p_capability_clears_unrecovered=0.00,
        p_intent_unrecovered=0.50,
        p_route_clears=0.00,
        message_clears_block=False,
    ),
    # Already paid. There is nothing to recover, and the only thing an agent can
    # do here is embarrass its merchant. Capability is trivially clear and intent
    # is moot; S1 terminates the thread.
    "ALREADY_PAID": WorldModel(
        blocking_share=0.00,
        retry_needs_customer=False,
        p_capability_clears_unrecovered=1.00,
        p_intent_unrecovered=1.00,
        p_route_clears=0.00,
        message_clears_block=False,
    ),
    # A risk decline. Nothing clears it without a human, and retrying it is how
    # a merchant gets penalised.
    "RISK": WorldModel(
        blocking_share=1.00,
        retry_needs_customer=True,
        p_capability_clears_unrecovered=0.00,
        p_intent_unrecovered=0.00,
        p_route_clears=0.00,
        message_clears_block=False,
    ),
    # Not eligible for this method. Needs a different *method*, not a different
    # *time* -- so no retry and no schedule helps, and a message offering an
    # alternative is the only thing that can.
    "ELIGIBILITY": WorldModel(
        blocking_share=1.00,
        retry_needs_customer=True,
        p_capability_clears_unrecovered=0.05,
        p_intent_unrecovered=0.40,
        p_route_clears=0.00,
        message_clears_block=True,
    ),
}

# --------------------------------------------------------------------------
# Response to being contacted
# --------------------------------------------------------------------------
#
# Two parameters per channel: whether they answer at all, and how long they take.
# Both **[C]**.
#
# The response rate is the one number here that could be accused of setting the
# answer, so it is worth being precise about how much it can move: arm B's only
# contact actions are INSTRUMENT_DEAD and ELIGIBILITY, which together are ~5% of
# volume. Halving P_MESSAGE_RESPONSE moves the headline by tenths of a percentage
# point, and metrics.py prints the contact-driven share of the incremental figure
# so that is checkable rather than asserted.

P_MESSAGE_RESPONSE = 0.28
MESSAGE_RESPONSE_MEDIAN_SECONDS = 4 * 3600     # read now, act after work
MESSAGE_RESPONSE_SIGMA = 1.20

#: Voice converts far better and costs ~17x more (PRD 10.3). Arm B never calls --
#: no reason class defaults to ACT_VOICE -- so these are declared now, before any
#: arm C result is visible, rather than tuned afterwards.
P_VOICE_RESPONSE = 0.55
VOICE_RESPONSE_MEDIAN_SECONDS = 30 * 60
VOICE_RESPONSE_SIGMA = 0.90


def _standard_normal(rng: Random) -> float:
    """Box-Muller, matching ``sim/generate.py``.

    Duplicated deliberately rather than imported: importing it would create an
    edge from the latent world back into the generator, and the generator's
    stream discipline is exactly what this module exists not to disturb.
    """
    u1 = rng.random()
    while u1 <= 0.0:
        u1 = rng.random()
    u2 = rng.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _lognormal_seconds(rng: Random, median: float, sigma: float) -> float:
    if median <= 0.0:
        # Still consumes the two Box-Muller draws, so the stream position does
        # not depend on which class the event belongs to.
        _standard_normal(rng)
        return 0.0
    return math.exp(math.log(median) + sigma * _standard_normal(rng))


def event_rng(seed: int, event_id: str) -> Random:
    """A per-event RNG, independent of stream position.

    Keyed on the event id rather than the loop index so that it survives the
    generator's final sort, a partial batch, or a reordered replay. Two events
    can never share a stream, and no draw here can move a draw there.
    """
    digest = canonical.sha256_hex("latent-v1|%d|%s" % (seed, event_id))
    return Random(int(digest[:16], 16))


def _clearance_median(reason_class: str) -> float:
    """How long an unrecovered event's block takes to clear, when it does.

    Reused from ``SELF_RECOVERY`` rather than declared again. That is one fewer
    invented number, and it is also the right claim: the credit cycle that
    governs when a customer pays unaided is the same credit cycle that governs
    when their balance arrives.
    """
    from sim.generate import SELF_RECOVERY

    return float(SELF_RECOVERY[reason_class].median_lag_seconds)


def _clearance_sigma(reason_class: str) -> float:
    from sim.generate import SELF_RECOVERY

    sigma = SELF_RECOVERY[reason_class].sigma
    return sigma if sigma > 0.0 else 1.0


def enrich(event: RiskEvent, seed: int) -> LatentTruth:
    """Fill in the capability/intent latents, conditional on ``self_recovers_at``.

    Conditional is the operative word. ``self_recovers_at`` is already drawn and
    calibrated to a ~30% organic rate; these fields have to be consistent with it
    rather than independent of it. Two branches:

    **The customer paid unaided at tau.** Then the block had cleared by tau -- you
    cannot pay before you are able to -- and intent is certain. Split the delay at
    ``blocking_share``: the front part was the block, the back part was them
    noticing. A retry inside the back part wins; a retry before the split loses.
    This is what makes ``capability_clears_at <= self_recovers_at`` a structural
    invariant rather than a hope, and ``tests/test_latent_world.py`` asserts it
    over the full batch.

    **They never paid unaided.** Then capability and intent are independent
    draws, and the interesting cell is capability-yes/intent-no: the money is
    there and nobody is coming back. That cell is what a silent retry harvests,
    and it is the largest single source of arm B's effect.
    """
    if event.latent is None:
        raise ValueError(
            "enrich() needs an event that already carries self_recovers_at; got "
            "one with no latent truth at all (%s)" % event.event_id
        )
    latent = event.latent
    world = WORLD[event.reason_class]
    rng = event_rng(seed, event.event_id)
    detected = canonical.parse_iso(event.detected_at)

    # Draw order is fixed and unconditional. Every event consumes the same draws
    # in the same order whichever branch it takes, so a scenario sweep that flips
    # an event between branches does not also shuffle its other latents -- which
    # would make the sensitivity table measure two changes at once.
    u_capability = rng.random()
    u_intent = rng.random()
    u_route = rng.random()
    u_message = rng.random()
    message_lag = _lognormal_seconds(
        rng, MESSAGE_RESPONSE_MEDIAN_SECONDS, MESSAGE_RESPONSE_SIGMA
    )
    u_voice = rng.random()
    voice_lag = _lognormal_seconds(
        rng, VOICE_RESPONSE_MEDIAN_SECONDS, VOICE_RESPONSE_SIGMA
    )
    unrecovered_lag = _lognormal_seconds(
        rng, _clearance_median(event.reason_class), _clearance_sigma(event.reason_class)
    )

    if latent.self_recovers_at is not None:
        recovered_at = canonical.parse_iso(latent.self_recovers_at)
        delay = (recovered_at - detected).total_seconds()
        capability_at = detected + timedelta(
            seconds=int(round(world.blocking_share * max(0.0, delay)))
        )
        capability_clears_at: Optional[str] = canonical.to_iso(capability_at)
        has_intent = True
    else:
        if u_capability < world.p_capability_clears_unrecovered:
            capability_clears_at = canonical.to_iso(
                detected + timedelta(seconds=int(unrecovered_lag))
            )
        else:
            capability_clears_at = None
        has_intent = u_intent < world.p_intent_unrecovered

    return replace(
        latent,
        capability_clears_at=capability_clears_at,
        has_intent=has_intent,
        route_would_succeed=u_route < world.p_route_clears,
        message_response_lag_seconds=(
            int(message_lag) if u_message < P_MESSAGE_RESPONSE else None
        ),
        voice_response_lag_seconds=(
            int(voice_lag) if u_voice < P_VOICE_RESPONSE else None
        ),
    )


def enrich_batch(events: Sequence[RiskEvent], seed: int) -> List[RiskEvent]:
    """``enrich`` over a batch, returning new events. Order is preserved."""
    return [replace(e, latent=enrich(e, seed)) for e in events]


def _self_check() -> None:
    from pramaan.taxonomy import REASON_CLASSES

    missing = [c for c in REASON_CLASSES if c not in WORLD]
    if missing:
        raise AssertionError(
            "reason classes with no world model: %r. An unmodelled class resolves "
            "as inert, and inert silently means 'arm B never helps here' -- a "
            "number, not an error, which is why this is checked at import."
            % missing
        )
    extra = [c for c in WORLD if c not in REASON_CLASSES]
    if extra:
        raise AssertionError("world models for unknown reason classes: %r" % extra)
    for name, model in WORLD.items():
        for field in (
            "blocking_share",
            "p_capability_clears_unrecovered",
            "p_intent_unrecovered",
            "p_route_clears",
        ):
            value = getattr(model, field)
            if not 0.0 <= value <= 1.0:
                raise AssertionError(
                    "%s.%s = %r is not a probability" % (name, field, value)
                )
    # The [A]-graded shape, asserted rather than commented. AUTH_DROPOFF is the
    # class Razorpay's own documentation describes, and the thing it describes is
    # a customer-driven recovery with no block: if blocking_share ever drifts up
    # from zero, the anchor has been abandoned and the calibration written into
    # EVALUATION.md no longer describes the code.
    if WORLD["AUTH_DROPOFF"].blocking_share != 0.0:
        raise AssertionError(
            "AUTH_DROPOFF.blocking_share must stay 0.0: the [A]-graded anchor is "
            "that the customer corrects the PIN in their own app, i.e. nothing "
            "was blocked. See EVALUATION.md."
        )
    if not WORLD["AUTH_DROPOFF"].retry_needs_customer:
        raise AssertionError(
            "AUTH_DROPOFF.retry_needs_customer must stay True: a merchant charge "
            "cannot supply a PIN."
        )
    if WORLD["FUNDS"].blocking_share <= WORLD["AUTH_DROPOFF"].blocking_share:
        raise AssertionError(
            "FUNDS must be more block-driven than AUTH_DROPOFF; that contrast is "
            "the calibration."
        )


_self_check()
