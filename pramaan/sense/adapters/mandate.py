"""The mandate adapter -- ``schedule -> notify at T-24h -> attempt``.

PRD 5 / 12: an e-mandate debit failure, R1-compliant by construction: RBI's
2026 e-mandate framework needs a pre-debit notification reaching the customer
at least 24 hours before an attempt, and R1 (``pramaan.envelope.rules``,
unchanged since Day 2) is what actually enforces that floor once a plan
proposes an action. This adapter's job is narrower and upstream of R1: turn a
raw mandate-debit-due signal into a ``RiskEvent`` so the envelope has
something to judge in the first place.

``FUNDS`` is the reason class, for the same reason it is the subscription
adapter's: a mandate instalment failure is "the money is not here yet, time it
to the cycle", and the scheduling floor R1 imposes is a *legal* one on top of
the same *economic* shape ``sim.latent.WORLD["FUNDS"]`` already models.

What this adapter deliberately does not do: simulate the notify step's own
delivery, or thread ``pre_debit_notified_at``/``mandate_attempt_ordinal``
into the envelope context per event. R1's mechanics are exercised directly
against ``EnvelopeContext`` in Day 2's own rule tests, which is the right
place for them; modelling a full notify-then-attempt sequence per simulated
event here would be scope this adapter does not need to carry to be a
correct, thin normaliser, and the seam is named rather than hidden -- see
STATE.md, Day 6.
"""
from __future__ import annotations

from dataclasses import dataclass

from pramaan import canonical
from pramaan.sense.adapters import base_actions
from pramaan.sense.models import Counterparty, RiskEvent
from pramaan.taxonomy import reason_class_of

CAUSE_SIGNAL = "mandate_debit_due"
LEGAL_CONTEXT = "service"


@dataclass(frozen=True)
class MandateDebitDue:
    """The raw shape of a scheduled e-mandate debit that needs recovery."""

    mandate_id: str
    counterparty_id: str
    segment: str
    debit_amount_paise: int
    detected_at: str
    arm: str


def build_event(raw: MandateDebitDue) -> RiskEvent:
    """Normalise one scheduled-debit signal into a ``RiskEvent``."""
    reason_class = reason_class_of(CAUSE_SIGNAL)
    band = canonical.amount_band(raw.debit_amount_paise)
    hour_bucket_value = canonical.hour_bucket(raw.detected_at)
    eligibility = canonical.channel_eligibility(reason_class, LEGAL_CONTEXT, hour_bucket_value)

    return RiskEvent(
        event_id="mnd_%s" % raw.mandate_id,
        source_type="mandate",
        amount_at_risk_paise=raw.debit_amount_paise,
        counterparty=Counterparty(id=raw.counterparty_id, kind="payer", segment=raw.segment),
        detected_at=raw.detected_at,
        decay_profile=canonical.DECAY_BY_SOURCE_TYPE["mandate"],
        cause_signal=CAUSE_SIGNAL,
        legal_context=LEGAL_CONTEXT,
        # The attempt (ACT_RETRY) and the T-24h notify (ACT_MESSAGE) are both
        # on the table; R1 decides, at judgement time, whether the attempt is
        # actually permitted yet.
        available_actions=base_actions(reason_class, eligibility, band, retry=True),
        arm=raw.arm,
        external_ref=raw.mandate_id,
    )
