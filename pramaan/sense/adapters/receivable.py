"""The receivable adapter -- a B2B invoice past due, reconciled against Smart
Collect virtual-account credits so the chaser never chases someone who has
already paid.

PRD 5 / 12.1: "Reconciled against Smart Collect virtual-account credits
(``virtual_account.credited``, attributed via ``customer_id``) so the chaser
never chases someone who already paid."

Two cause signals, and the second one is the point of this adapter rather than
an edge case bolted on:

``receivable_overdue``
    The ordinary case. ``legal_context="collection"`` -- PRD 3.1 names this
    exactly: a receivables chase *is* debt collection, unlike a payment retry,
    which is a service message. That is what routes it through R9's
    08:00-19:00 window instead of running unrestricted, and it is the
    clearest place in the whole codebase to see ``legal_context`` do its job.

``receivable_reconciled``
    A Smart Collect virtual-account credit has already matched this invoice
    by the time recovery would otherwise fire. This is *not* modelled as
    simulator ground truth (``LatentTruth``) the way a payment's own organic
    recovery is -- ground truth is deliberately invisible to every agent and
    every rule in this system. A reconciled credit is instead an *observed*
    fact, exactly the way ``order_already_paid`` is an observed decline code
    for payments: real, on the wire, and something S1 can act on without
    reading an answer key. ``pramaan.eval.arms.envelope_context`` sets
    ``virtual_account_credited=True`` whenever it sees this cause signal,
    which is what lets S1 terminate the thread (see ``stopping.s1_already_paid``)
    -- the mechanism the Day 6 definition of done names directly: "a paid
    invoice is never chased."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pramaan import canonical
from pramaan.sense.adapters import base_actions
from pramaan.sense.models import Counterparty, RiskEvent
from pramaan.taxonomy import reason_class_of

CAUSE_OVERDUE = "receivable_overdue"
CAUSE_RECONCILED = "receivable_reconciled"
CAUSE_SIGNALS = (CAUSE_OVERDUE, CAUSE_RECONCILED)

#: Debt collection, per PRD 3.1 -- the one adapter of the five where this is
#: unambiguous rather than a judgment call.
LEGAL_CONTEXT = "collection"


@dataclass(frozen=True)
class ReceivableSignal:
    """The raw shape: an overdue invoice, or its Smart Collect reconciliation."""

    invoice_id: str
    counterparty_id: str  # the business, not a payer
    segment: str
    invoice_amount_paise: int
    detected_at: str
    days_overdue: int
    arm: str
    #: Set when a Smart Collect ``virtual_account.credited`` webhook has
    #: already reconciled against this invoice by detection time.
    reconciled: bool = False


def build_event(raw: ReceivableSignal) -> RiskEvent:
    """Normalise one overdue-invoice (or reconciled-invoice) signal."""
    if raw.days_overdue < 0:
        raise ValueError("days_overdue must be non-negative, got %r" % raw.days_overdue)

    cause_signal = CAUSE_RECONCILED if raw.reconciled else CAUSE_OVERDUE
    reason_class = reason_class_of(cause_signal)
    band = canonical.amount_band(raw.invoice_amount_paise)
    hour_bucket_value = canonical.hour_bucket(raw.detected_at)
    eligibility = canonical.channel_eligibility(reason_class, LEGAL_CONTEXT, hour_bucket_value)

    return RiskEvent(
        event_id="rcv_%s" % raw.invoice_id,
        source_type="receivable",
        amount_at_risk_paise=raw.invoice_amount_paise,
        counterparty=Counterparty(id=raw.counterparty_id, kind="business", segment=raw.segment),
        detected_at=raw.detected_at,
        decay_profile=canonical.DECAY_BY_SOURCE_TYPE["receivable"],
        cause_signal=cause_signal,
        legal_context=LEGAL_CONTEXT,
        # Reconciled: nothing left to do but stop (S1). Overdue: no ACT_RETRY
        # -- an invoice has no instrument to retry, only an escalation ladder
        # of contact -- so the ladder is ACT_WAIT plus whatever this window
        # and class permit.
        available_actions=base_actions(
            reason_class, eligibility, band, retry=False, stop=raw.reconciled
        ),
        arm=raw.arm,
        external_ref=raw.invoice_id,
    )
