"""The subscription adapter -- intervene in ``pending`` before ``halted``.

PRD 5: "``subscription.pending`` -> ``subscription.halted`` [A]. Razorpay
auto-retries the following day and halts after retries are exhausted [A]. The
``pending`` window is the seam -- intervene there or the subscription dies."

So this adapter deliberately has only one live cause signal,
``subscription_pending``: the halted state is not something to build a
``RiskEvent`` for, it is the failure this adapter exists to prevent. There is
nothing to recover once a subscription has actually halted that a
*recovery* action addresses -- re-subscribing is a new checkout, not a
retry -- so a halted event is out of scope for this adapter by construction,
not an oversight.

``FUNDS`` is the reason class: an instalment failure is, in the aggregate,
the same "the money is not here yet, time it to the credit cycle" story a
payment's own insufficient-balance failures tell, and Razorpay's own
auto-retry-the-next-day behaviour is direct evidence for exactly that.
"""
from __future__ import annotations

from dataclasses import dataclass

from pramaan import canonical
from pramaan.sense.adapters import base_actions
from pramaan.sense.models import Counterparty, RiskEvent
from pramaan.taxonomy import reason_class_of

CAUSE_SIGNAL = "subscription_pending"
LEGAL_CONTEXT = "service"


@dataclass(frozen=True)
class SubscriptionPendingEvent:
    """The raw shape of a ``subscription.pending`` webhook."""

    subscription_id: str
    counterparty_id: str
    segment: str
    instalment_amount_paise: int
    detected_at: str
    retry_attempt: int  # Razorpay's own attempt ordinal within the pending window
    arm: str


def build_event(raw: SubscriptionPendingEvent) -> RiskEvent:
    """Normalise one ``subscription.pending`` webhook into a ``RiskEvent``."""
    if raw.retry_attempt < 1:
        raise ValueError("retry_attempt must be >= 1, got %r" % raw.retry_attempt)

    reason_class = reason_class_of(CAUSE_SIGNAL)
    band = canonical.amount_band(raw.instalment_amount_paise)
    hour_bucket_value = canonical.hour_bucket(raw.detected_at)
    eligibility = canonical.channel_eligibility(reason_class, LEGAL_CONTEXT, hour_bucket_value)

    return RiskEvent(
        event_id="sub_%s_%d" % (raw.subscription_id, raw.retry_attempt),
        source_type="subscription",
        amount_at_risk_paise=raw.instalment_amount_paise,
        counterparty=Counterparty(id=raw.counterparty_id, kind="payer", segment=raw.segment),
        detected_at=raw.detected_at,
        decay_profile=canonical.DECAY_BY_SOURCE_TYPE["subscription"],
        cause_signal=CAUSE_SIGNAL,
        legal_context=LEGAL_CONTEXT,
        # Scheduled retry is on the table (Razorpay's own next-day cadence);
        # a message nudging the customer to keep the instrument funded is too.
        available_actions=base_actions(reason_class, eligibility, band, retry=True),
        arm=raw.arm,
        external_ref=raw.subscription_id,
    )
