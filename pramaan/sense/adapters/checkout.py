"""The checkout adapter -- order created, never captured, plus a stage beacon.

PRD 5: "order created, no payment captured within a window; plus client-side
stage beacons. Stage matters enormously: abandonment at method-selection is a
UX problem; at OTP entry it is a delivery problem. The investigator gets the
stage."

That last sentence is what makes this adapter thin. ``STAGES`` is not one of
the seven frozen signature fields (F7) -- it never reaches a prompt -- so it
does not need its own reason class or its own guardrail. It travels as the raw
``cause_signal`` instead, which the investigator's SQL tool belt (Day 4) can
read directly off the ``events`` table, exactly like a payment's raw decline
code. What the signature layer needs is only the *policy consequence* of the
stage, and ``taxonomy.NON_PAYMENT_CAUSE_CLASS`` supplies that:

- method-selection -> ``ELIGIBILITY`` (no retry; a message offering another
  method is the one thing that helps).
- OTP-entry -> ``AUTH_DROPOFF`` (structurally identical to a payment's own
  auth dropoff: no block to clear, contact is defensible).
- processing -> ``TECH_TRANSIENT`` (looks like the bank is degraded; contact is
  waste, wait it out).

There is no ``ACT_RETRY`` on a checkout event ever, at any stage: nothing has
been attempted yet to retry. That is a business decision this adapter makes,
not something derived from the class policy, which is why ``retry=False`` is
passed explicitly below rather than left to inherit from FUNDS/AUTH_DROPOFF's
own retry_mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from pramaan import canonical
from pramaan.sense.adapters import base_actions
from pramaan.sense.models import Counterparty, RiskEvent
from pramaan.taxonomy import reason_class_of

STAGES: Tuple[str, ...] = (
    "checkout_stage_method_selection",
    "checkout_stage_otp_entry",
    "checkout_stage_processing",
)

LEGAL_CONTEXT = "service"


@dataclass(frozen=True)
class CheckoutBeacon:
    """The raw shape a client-side beacon (or its webhook mirror) would send."""

    order_id: str
    counterparty_id: str
    segment: str
    amount_paise: int
    detected_at: str
    stage: str
    arm: str


def build_event(beacon: CheckoutBeacon) -> RiskEvent:
    """Normalise one abandoned-checkout beacon into a ``RiskEvent``."""
    if beacon.stage not in STAGES:
        raise ValueError("unknown checkout stage %r; expected one of %r" % (beacon.stage, STAGES))

    reason_class = reason_class_of(beacon.stage)
    band = canonical.amount_band(beacon.amount_paise)
    hour_bucket_value = canonical.hour_bucket(beacon.detected_at)
    eligibility = canonical.channel_eligibility(reason_class, LEGAL_CONTEXT, hour_bucket_value)

    return RiskEvent(
        event_id="chk_%s" % beacon.order_id,
        source_type="checkout",
        amount_at_risk_paise=beacon.amount_paise,
        counterparty=Counterparty(id=beacon.counterparty_id, kind="payer", segment=beacon.segment),
        detected_at=beacon.detected_at,
        decay_profile=canonical.DECAY_BY_SOURCE_TYPE["checkout"],
        cause_signal=beacon.stage,
        legal_context=LEGAL_CONTEXT,
        available_actions=base_actions(reason_class, eligibility, band, retry=False),
        arm=beacon.arm,
        external_ref=beacon.order_id,
    )
