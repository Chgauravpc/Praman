"""Reversibility tiers T0-T4. "Bounded" is meaningless without them.

PRD 6.5. Every action in the system sits in exactly one tier, and the tier
determines which gates it has to clear. The tier is a property of the *action*,
not of the situation, so it is a lookup rather than a judgment -- which is
precisely why it belongs in the deterministic component.

The asymmetry to internalise
----------------------------

**A charge retry is refundable. A message is not, and a phone call really is
not.** Most dunning systems gate retries hard -- caps, cool-offs, idempotency
keys -- and gate messaging loosely, because a retry touches money and a message
"only" touches a person. That is exactly backwards on the axis that matters
here, which is reversibility:

- A retry that should not have happened can be refunded. The customer may never
  notice. Cost: an API call and a possible chargeback fee.
- A message that should not have been sent cannot be unsent. It has already
  been read. Cost: the merchant's DLT reputation, an unsubscribe, sometimes a
  complaint that counts against the header everyone else on it also depends on.
- A voice call that should not have been made cannot be un-made, and under R9 a
  badly-made one is *harassment* -- which, under R3, is the acquirer's liability
  and not only the merchant's.

So T1 (retry) is auto-approved inside caps, and T2/T3 (message/voice) carry the
window, consent, budget and disclosure gates. The tiers are how that inversion
becomes structural instead of a matter of remembering.

What each tier costs to get wrong, in one line each::

    T0  silently reversible      nothing. Undo it and nobody knew.
    T1  reversible, visible      a refund and possibly a fee.
    T2  irreversible             a person read something they should not have.
    T3  irreversible, high-touch a person was telephoned. Possibly harassed.
    T4  irreversible, costly     money given away, or a human's hour spent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple

from pramaan.canonical import ACTIONS
from pramaan.envelope.context import (
    EnvelopeContext,
    Ruling,
    not_applicable,
    passes,
    refuses,
)

TIERS: Tuple[str, ...] = ("T0", "T1", "T2", "T3", "T4")


@dataclass(frozen=True)
class Tier:
    """What a tier is, and what it demands. Data, so it reads as a table."""

    tier: str
    label: str
    reversible: bool
    customer_visible: bool
    #: Does the (legal_context x channel x hour) matrix bind this tier?
    needs_legal_window: bool
    #: Does it need consent, a contact budget and a DND scrub?
    needs_consent: bool
    #: A rupee floor below which the action is not worth its own cost. Only
    #: meaningful for the expensive tiers; ``None`` means no floor.
    min_amount_paise: Optional[int]
    #: A rupee ceiling above which a human must approve. ``None`` means never.
    human_approval_above_paise: Optional[int]


#: P2. A voice call is metered and a person's phone rings. Below Rs 500 the call
#: can cost more than the expected recovery, so band 1 does not earn one. A
#: **house number** -- no regulator specifies it -- and it is deliberately set at
#: the band-1 ceiling so it moves with F6 rather than drifting independently.
VOICE_MIN_AMOUNT_PAISE = 50_000

#: P3. Above Rs 5,000 a concession is a commercial decision, not a recovery
#: tactic, and an agent should not be able to give away more than that on its
#: own judgement. Also a house number.
CONCESSION_APPROVAL_ABOVE_PAISE = 500_000

TIER_SPEC: Dict[str, Tier] = {
    "T0": Tier(
        "T0", "silently reversible", True, False, False, False, None, None,
    ),
    "T1": Tier(
        "T1", "reversible, customer-visible", True, True, False, False, None, None,
    ),
    "T2": Tier(
        "T2", "irreversible", False, True, True, True, None, None,
    ),
    "T3": Tier(
        "T3", "irreversible, high-touch", False, True, True, True,
        VOICE_MIN_AMOUNT_PAISE, None,
    ),
    "T4": Tier(
        "T4", "irreversible, costly", False, True, True, True,
        None, CONCESSION_APPROVAL_ABOVE_PAISE,
    ),
}

#: Every action in ``canonical.ACTIONS``, tiered. Checked for completeness at
#: import: an action added to the vocabulary without a tier would otherwise fall
#: through every gate this module owns.
ACTION_TIER: Dict[str, str] = {
    "ACT_WAIT": "T0",             # doing nothing, on purpose. Fully reversible.
    "ACT_ROUTE": "T0",            # rail/routing change. The customer sees a
                                  # different button, not a different charge.
    "ACT_ALERT_MERCHANT": "T0",   # internal. Never reaches a customer.
    "ACT_PAGE_ENGINEER": "T0",    # internal.
    "ACT_STOP": "T0",             # terminating a thread is always safe.
    "ACT_RETRY": "T1",            # money moves, and it can move back.
    "ACT_MESSAGE": "T2",          # cannot be unsent.
    "ACT_VOICE": "T3",            # cannot be un-called. R8/R9/R10 live here.
    "ACT_CONCESSION": "T4",       # money given away.
    "ACT_ESCALATE_HUMAN": "T4",   # a human's time, spent.
}

#: ``ACT_ESCALATE_HUMAN`` is T4 by cost but exempt from T4's approval gate, and
#: the reason is not a special case so much as a fixed point: escalating *to* a
#: human is how approval is obtained. Requiring approval first would make the
#: only available response to a distress signal (S7) unreachable, which is the
#: one place in the system where being unable to act is genuinely dangerous.
APPROVAL_EXEMPT_ACTIONS: FrozenSet[str] = frozenset({"ACT_ESCALATE_HUMAN"})


def tier_of(action: str) -> str:
    try:
        return ACTION_TIER[action]
    except KeyError:
        raise KeyError(
            "action %r has no reversibility tier. Every action must be tiered "
            "before it can be judged -- an untiered action would skip the "
            "window, consent and approval gates entirely." % action
        ) from None


def spec_of(action: str) -> Tier:
    return TIER_SPEC[tier_of(action)]


def needs_legal_window(action: str) -> bool:
    return spec_of(action).needs_legal_window


def needs_consent(action: str) -> bool:
    return spec_of(action).needs_consent


def check_value_floor(action: str, context: EnvelopeContext) -> Ruling:
    """T3's floor: is this action worth its own cost at this amount?

    Cited as P2 rather than as a T id, because the tier is the structure and the
    threshold is the policy. Somebody may reasonably want a different number;
    nobody should be able to change it without noticing they are changing a
    house rule rather than a legal one.
    """
    spec = spec_of(action)
    if spec.min_amount_paise is None:
        return not_applicable("P2", "%s has no value floor" % spec.tier)
    if context.amount_paise < spec.min_amount_paise:
        return refuses(
            "P2",
            "%s at Rs %.2f is below the Rs %.2f floor for %s (%s) -- a metered "
            "call on this amount can cost more than it recovers"
            % (
                action,
                context.amount_paise / 100,
                spec.min_amount_paise / 100,
                spec.tier,
                spec.label,
            ),
        )
    return passes(
        "P2",
        "Rs %.2f clears the Rs %.2f floor for %s"
        % (context.amount_paise / 100, spec.min_amount_paise / 100, spec.tier),
    )


def check_human_approval(action: str, context: EnvelopeContext) -> Ruling:
    """T4's ceiling: above a threshold, a person signs it off."""
    spec = spec_of(action)
    if spec.human_approval_above_paise is None or action in APPROVAL_EXEMPT_ACTIONS:
        return not_applicable("P3", "%s needs no human approval" % action)
    if context.amount_paise <= spec.human_approval_above_paise:
        return passes(
            "P3",
            "Rs %.2f is within the Rs %.2f autonomous limit for %s"
            % (
                context.amount_paise / 100,
                spec.human_approval_above_paise / 100,
                spec.tier,
            ),
        )
    if context.human_approved:
        return passes(
            "P3",
            "Rs %.2f exceeds the Rs %.2f autonomous limit and carries human "
            "approval"
            % (context.amount_paise / 100, spec.human_approval_above_paise / 100),
        )
    return refuses(
        "P3",
        "%s of Rs %.2f exceeds the Rs %.2f autonomous limit for %s (%s) and "
        "carries no human approval"
        % (
            action,
            context.amount_paise / 100,
            spec.human_approval_above_paise / 100,
            spec.tier,
            spec.label,
        ),
    )


def _self_check() -> None:
    missing = set(ACTIONS) - set(ACTION_TIER)
    extra = set(ACTION_TIER) - set(ACTIONS)
    if missing or extra:
        raise AssertionError(
            "ACTION_TIER must cover exactly canonical.ACTIONS; missing=%r extra=%r"
            % (sorted(missing), sorted(extra))
        )
    if set(TIER_SPEC) != set(TIERS):
        raise AssertionError("TIER_SPEC must cover exactly %r" % (TIERS,))

    # The asymmetry, asserted. If somebody ever "simplifies" this table by
    # putting messaging on the same footing as retrying, this is what fails.
    if TIER_SPEC[tier_of("ACT_RETRY")].reversible is not True:
        raise AssertionError("a charge retry is refundable; it must be reversible")
    for action in ("ACT_MESSAGE", "ACT_VOICE"):
        if TIER_SPEC[tier_of(action)].reversible:
            raise AssertionError("%s cannot be un-sent; it is not reversible" % action)
    if tier_of("ACT_VOICE") <= tier_of("ACT_MESSAGE"):
        raise AssertionError(
            "a phone call must sit in a stricter tier than a message"
        )
    if TIER_SPEC[tier_of("ACT_RETRY")].needs_legal_window:
        raise AssertionError(
            "a silent retry is not a communication -- binding it to the legal "
            "window would surrender the 19:00-22:00 failure peak, which is the "
            "whole point of PRD 7"
        )


_self_check()
