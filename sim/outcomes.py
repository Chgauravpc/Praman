"""The outcome oracle -- did this event recover, and what caused it.

This is the only file in the repo that decides whether a payment came back, and
it lives under ``sim/`` on purpose. In production that answer arrives from a
Razorpay ``payment.captured`` webhook; here it is computed from latent truth. Two
consequences of putting the boundary here rather than inside ``pramaan/eval/``:

1. ``pramaan/eval/resolve.py`` never contains a modelling assumption. It applies
   an arm's policy, asks an oracle, and writes ledger rows. Swapping this oracle
   for a webhook reader is the whole of what productionising the measurement
   would take, and a reviewer can see that by reading the seam.
2. Every invented number in the measurement path is behind one import. "Where
   are the assumptions" has a one-word answer.

**Resolution is a pure function of (event latents, action, delay, window).** No
RNG here at all -- every random draw was made at generation time and is sitting
in ``LatentTruth``. That is what makes both potential outcomes -- what this event
does under arm A *and* under arm B -- exactly computable for every event, which
is what ``tests/test_estimator_unbiased.py`` compares the randomised estimate
against. An oracle that rolled dice at resolution time would give that test a
Monte-Carlo error of its own and turn a proof into a smell test.

**The censoring rule, applied identically to every arm.** A recovery counts if
and only if it lands inside ``detected_at + window``. Recoveries after the window
are counted as non-recoveries everywhere, in arm A as in arm B. That is what
makes the difference interpretable: both arms are censored the same way, so the
censoring cancels in B-A and only shows up in the absolute levels. It is stated
on screen for the same reason it is stated here -- an uncensored arm A next to a
censored arm B would manufacture an effect out of nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from pramaan import canonical
from pramaan.sense.models import RiskEvent
from sim.latent import WORLD

#: Actions that do not touch the customer payment path at all, so the outcome is
#: whatever arm A's outcome would have been.
#:
#: ``ACT_ALERT_MERCHANT`` and ``ACT_PAGE_ENGINEER`` are in this list and that is a
#: deliberate, load-bearing piece of honesty. Both are the *correct* action for
#: their class, and neither recovers the event in front of them: telling a
#: merchant their MCC is misconfigured fixes the next thousand payments, not this
#: one. Folding that value into a recovery figure would be inventing revenue, so
#: ``resolve.py`` counts these as non-recoveries and reports the value they touch
#: as a separate, explicitly labelled externality. Under-claiming a real benefit
#: is the right way round to be wrong here.
INERT_ACTIONS = frozenset(
    {
        "ACT_WAIT",
        "ACT_ALERT_MERCHANT",
        "ACT_PAGE_ENGINEER",
        "ACT_ESCALATE_HUMAN",
        "ACT_STOP",
        "ACT_CONCESSION",
        "none",
    }
)

CONTACT_ACTIONS = frozenset({"ACT_MESSAGE", "ACT_VOICE"})

#: Per-action cost, integer paise. Grades in the comments, because a cost table
#: is exactly the kind of thing that gets quoted back.
#:
#: - retries and routes cost nothing to attempt. A *successful* charge carries
#:   MDR, but MDR is a cost of revenue and not a cost of recovery -- charging it
#:   against the recovery would double-count, since the same MDR would have been
#:   paid had the customer come back unaided.
#: - a message is a WhatsApp utility template at ~Rs 0.145 **[B]** (PRD 10.3),
#:   rounded up to 15 paise.
#: - voice is the one line PRD 10.3 explicitly refuses to estimate: "metered.
#:   Measure it; do not estimate it." The figure below is a placeholder **[C]**
#:   carried only so arm C has a cost axis on Day 5, and metrics.py prints how
#:   many voice actions it was applied to -- which on arm B is zero.
#: - human escalation at Rs 25 **[B]** (PRD 10.3).
ACTION_COST_PAISE = {
    "ACT_WAIT": 0,
    "ACT_ROUTE": 0,
    "ACT_RETRY": 0,
    "ACT_MESSAGE": 15,
    "ACT_VOICE": 250,
    "ACT_CONCESSION": 0,
    "ACT_ALERT_MERCHANT": 0,
    "ACT_PAGE_ENGINEER": 0,
    "ACT_ESCALATE_HUMAN": 2_500,
    "ACT_STOP": 0,
    "none": 0,
}

#: Which actions cost money per *contact*, for PRD 10.3's real objective --
#: rupees recovered per customer contact, not per rupee of compute.
CONTACT_COST_PAISE = {"ACT_MESSAGE": 15, "ACT_VOICE": 250}


@dataclass(frozen=True)
class Resolution:
    """What happened to one event under one action. No randomness, no clock."""

    recovered: bool
    recovered_at: Optional[str]

    #: What caused it: ``organic`` | ``retry`` | ``route`` | ``message`` |
    #: ``voice`` | ``none``. Attribution is by *whichever came first*, so an
    #: intervention that fired after the customer had already paid is recorded as
    #: organic -- which is the honest reading and the one that keeps the
    #: false-intervention rate meaningful.
    cause: str

    #: Would this event have recovered inside the window with no action at all?
    #: The counterfactual, and the field the false-intervention rate is built
    #: from. Available here and *never* in production -- which is the entire
    #: reason the randomised holdout exists.
    would_recover_unaided: bool

    #: True if the action actually reached the customer. Distinct from
    #: ``recovered``: a message that was sent and ignored still spent the
    #: contact, and the contact is the scarce resource (PRD 10.3).
    contacted: bool

    #: True where the action was the right one and cannot recover *this* event --
    #: alerting a merchant about their own misconfiguration. Reported separately.
    externality: bool


def _iso_or_none(dt) -> Optional[str]:
    return None if dt is None else canonical.to_iso(dt)


def resolve(
    event: RiskEvent,
    action: str,
    delay_seconds: int,
    window_seconds: int,
) -> Resolution:
    """Resolve one event under one action. Pure.

    The order of business matters and is worth reading in one go:

    1. Establish the **unaided** outcome first, from ``self_recovers_at`` alone.
       This is arm A's answer, and it is also the counterfactual every other arm
       is scored against.
    2. If the action cannot touch the payment path, return that answer unchanged.
       Not "return a slightly better answer" -- unchanged. Arms must be identical
       wherever the policy does nothing, or B-A picks up a modelling artefact.
    3. Otherwise work out when the action would land a recovery, and take
       whichever of the two came **first**.
    """
    latent = event.latent
    if latent is None:
        raise ValueError(
            "the oracle needs latent truth; event %s has none. In production the "
            "outcome comes from a webhook instead -- see the module docstring."
            % event.event_id
        )
    world = WORLD[event.reason_class]
    detected = canonical.parse_iso(event.detected_at)
    window_end = detected + timedelta(seconds=window_seconds)

    # -- 1. the unaided outcome -------------------------------------------
    unaided_at = None
    if latent.self_recovers_at is not None:
        candidate = canonical.parse_iso(latent.self_recovers_at)
        if candidate <= window_end:
            unaided_at = candidate
    would_recover_unaided = unaided_at is not None

    # -- 2. inert actions leave it exactly alone ---------------------------
    externality = action in ("ACT_ALERT_MERCHANT", "ACT_PAGE_ENGINEER")
    if action in INERT_ACTIONS:
        return Resolution(
            recovered=would_recover_unaided,
            recovered_at=_iso_or_none(unaided_at),
            cause="organic" if would_recover_unaided else "none",
            would_recover_unaided=would_recover_unaided,
            contacted=False,
            externality=externality,
        )

    # -- 3. when would the action land? ------------------------------------
    fires_at = detected + timedelta(seconds=int(delay_seconds))
    capability_at = (
        canonical.parse_iso(latent.capability_clears_at)
        if latent.capability_clears_at is not None
        else None
    )

    action_at = None
    contacted = False

    if fires_at <= window_end:
        if action == "ACT_RETRY":
            # A merchant-initiated charge. Supplies intent, needs capability, and
            # is impossible where the customer must be present to authenticate or
            # to re-supply the instrument.
            if not world.retry_needs_customer:
                if capability_at is not None and capability_at <= fires_at:
                    action_at = fires_at

        elif action == "ACT_ROUTE":
            # An immediate re-attempt on a different rail. It wins exactly when
            # the block was rail-specific, which is a latent property of the
            # event drawn at generation. Also server-side, so it carries the same
            # customer-absent requirement as a retry.
            if not world.retry_needs_customer and latent.route_would_succeed:
                action_at = fires_at

        elif action in CONTACT_ACTIONS:
            # Reaching the customer spends the contact whether or not they act,
            # so ``contacted`` is set before any success test.
            contacted = True
            lag = (
                latent.message_response_lag_seconds
                if action == "ACT_MESSAGE"
                else latent.voice_response_lag_seconds
            )
            if lag is not None and latent.has_intent:
                acts_at = fires_at + timedelta(seconds=lag)
                if acts_at <= window_end:
                    if world.message_clears_block:
                        # The block *was* the customer not having done the thing.
                        # Asking is what removes it: re-enter the PIN, re-add the
                        # card, pick another method.
                        action_at = acts_at
                    elif capability_at is not None and capability_at <= acts_at:
                        # The block is money or a merchant defect. A reminder
                        # only converts once the block has cleared on its own.
                        action_at = acts_at

    # -- take whichever came first ----------------------------------------
    cause_by_action = {
        "ACT_RETRY": "retry",
        "ACT_ROUTE": "route",
        "ACT_MESSAGE": "message",
        "ACT_VOICE": "voice",
    }
    if action_at is None and unaided_at is None:
        recovered_at, cause = None, "none"
    elif action_at is None:
        recovered_at, cause = unaided_at, "organic"
    elif unaided_at is None or action_at <= unaided_at:
        recovered_at, cause = action_at, cause_by_action.get(action, "none")
    else:
        recovered_at, cause = unaided_at, "organic"

    return Resolution(
        recovered=recovered_at is not None,
        recovered_at=_iso_or_none(recovered_at),
        cause=cause,
        would_recover_unaided=would_recover_unaided,
        contacted=contacted,
        externality=externality,
    )


def action_cost_paise(action: str) -> int:
    return ACTION_COST_PAISE.get(action, 0)


def _self_check() -> None:
    from pramaan import canonical as _c

    unknown = [a for a in ACTION_COST_PAISE if a not in _c.ACTIONS and a != "none"]
    if unknown:
        raise AssertionError("cost table names unknown actions: %r" % unknown)
    uncosted = [a for a in _c.ACTIONS if a not in ACTION_COST_PAISE]
    if uncosted:
        raise AssertionError(
            "actions with no declared cost: %r. An uncosted action is free in the "
            "unit economics, and free is a claim." % uncosted
        )
    overlap = INERT_ACTIONS & CONTACT_ACTIONS
    if overlap:
        raise AssertionError(
            "an action cannot be both inert and a contact: %r" % sorted(overlap)
        )


_self_check()
