"""The RiskEvent -- the abstraction that makes the whole brief fit in eight days.

PRD 3.1. The track brief lists seven directions; read as a feature list that is
seven products and an impossible week. Read properly it is **five event types,
one channel and one cross-cutting state machine** over a single loop. So
everything normalises into one shape, and the loop is written once.

Two fields do the real work:

``decay_profile``
    How fast this money dies. It is what lets one policy handle a UPI PIN error
    (half-life: minutes) and a 45-day-overdue invoice (half-life: weeks) without
    special-casing either.

``legal_context``
    Whether this contact is a service message, debt collection, or promotional.
    Those three have different legal calling windows (PRD 7) -- same loop,
    radically different envelope. It is the field most builds will not have at
    all.

And one field is deliberately quarantined: ``latent``. See below.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

from pramaan import canonical
from pramaan.taxonomy import reason_class_of


@dataclass(frozen=True)
class Counterparty:
    """Who is on the other side. A payer, or a business for receivables.

    ``segment`` lives here rather than on the event because it is a property of
    the counterparty, and it is one of the seven signature fields (F7).
    """

    id: str
    kind: str      # payer | business
    segment: str   # metro | tier2 | tier3

    def __post_init__(self) -> None:
        if self.kind not in ("payer", "business"):
            raise ValueError("counterparty kind must be payer|business, got %r" % self.kind)
        if self.segment not in canonical.SEGMENTS:
            raise ValueError("unknown segment %r" % self.segment)


@dataclass(frozen=True)
class LatentTruth:
    """Simulator ground truth. **Never visible to the agent.**

    PRD 8.2: in production the counterfactual is unobservable; in simulation it
    is known. That asymmetry is the whole measurement design --

    1. the holdout estimates the effect *the way production would*, and
    2. this ground truth then verifies that the estimator is unbiased
       (tests/test_estimator_unbiased.py, invariant I6).

    Which makes this the most dangerous object in the codebase. If it ever
    reaches a diagnosis, a plan, or a prompt, the agent is reading the answer key
    and every number downstream is fiction. Three structural defences:

    - it is stored in a **separate SQLite table**, so the investigator's
      read-only SQL tool belt (Day 4) cannot join to it by accident;
    - ``RiskEvent.canonical_features()`` cannot reach it -- features are built
      from the seven frozen fields, and this is not one of them;
    - ``tests/test_prompt_canonical.py`` asserts that an event carrying latent
      truth produces byte-identical prompt bytes to one without it.

    ``self_recovers_at`` is the counterfactual proper: when this payment would
    have succeeded on its own, with no intervention at all. ``None`` means it
    never would. Razorpay's own webhook docs warn that payment.failed is often
    followed by payment.captured for the same transaction -- customers fix a
    wrong UPI PIN and retry inside their banking app -- so a large share of
    "recovered" revenue was never lost. This field is how that share gets
    subtracted instead of claimed.
    """

    self_recovers_at: Optional[str] = None

    @property
    def self_recovers(self) -> bool:
        return self.self_recovers_at is not None


@dataclass(frozen=True)
class RiskEvent:
    """Revenue at risk, normalised. Immutable: events are facts, not records."""

    event_id: str
    source_type: str              # payment | checkout | subscription | mandate | receivable
    amount_at_risk_paise: int     # integer paise; never a float, never a rupee string
    counterparty: Counterparty
    detected_at: str              # ISO-8601 with an explicit offset. EVENT time.
    decay_profile: str
    cause_signal: str             # raw error code | abandonment stage | days overdue
    legal_context: str            # service | collection | promotional
    available_actions: Tuple[str, ...]
    arm: str                      # A | B | C -- assigned once, at detection

    #: The provider-side identifier: payment_id, order_id, subscription_id.
    #: Named "external_ref" rather than "payment_id" so that its one rule is
    #: legible at every use site: it travels *alongside* prompts for logging and
    #: reconciliation, and never *inside* one (PRD 9.1, anti-pattern A3).
    external_ref: Optional[str] = None

    #: Simulator ground truth, or None in production. Quarantined -- see
    #: LatentTruth.
    latent: Optional[LatentTruth] = None

    def __post_init__(self) -> None:
        if self.source_type not in canonical.SOURCE_TYPES:
            raise ValueError("unknown source_type %r" % self.source_type)
        if self.decay_profile not in canonical.DECAY_PROFILES:
            raise ValueError("unknown decay_profile %r" % self.decay_profile)
        if self.legal_context not in canonical.LEGAL_CONTEXTS:
            raise ValueError("unknown legal_context %r" % self.legal_context)
        if self.arm not in canonical.ARMS:
            raise ValueError("arm must be one of %r, got %r" % (canonical.ARMS, self.arm))
        if isinstance(self.amount_at_risk_paise, bool) or not isinstance(
            self.amount_at_risk_paise, int
        ):
            raise TypeError("amount_at_risk_paise must be an int (paise)")
        if self.amount_at_risk_paise < 0:
            raise ValueError("amount_at_risk_paise must be non-negative")
        unknown = [a for a in self.available_actions if a not in canonical.ACTIONS]
        if unknown:
            raise ValueError("unknown actions %r" % unknown)
        # Fail loudly on a naive timestamp rather than silently mis-bucketing it.
        canonical.parse_iso(self.detected_at)

    # -- derived, all low-cardinality ------------------------------------

    @property
    def reason_class(self) -> str:
        """Reason class, for a payment-shaped event.

        Non-payment adapters (Day 6) carry a different cause_signal vocabulary --
        an abandonment stage, a days-overdue count -- and will map to their own
        classes. reason_class_of defaults unknown signals to RISK, the class that
        permits nothing, so an unmapped adapter fails closed rather than getting
        retried.
        """
        return reason_class_of(self.cause_signal)

    @property
    def amount_band(self) -> int:
        return canonical.amount_band(self.amount_at_risk_paise)

    @property
    def hour_bucket(self) -> str:
        return canonical.hour_bucket(self.detected_at)

    @property
    def channel_eligibility(self) -> str:
        return canonical.channel_eligibility(
            self.reason_class, self.legal_context, self.hour_bucket
        )

    def canonical_features(self, diagnosis_class: str = "undiagnosed") -> Dict[str, Any]:
        """The seven frozen signature fields, and nothing else.

        This is the only sanctioned road from an event to a prompt. Note what is
        absent by construction: event_id, external_ref, counterparty.id,
        amount_at_risk_paise, detected_at. Every one of those is
        high-cardinality, and any one of them would take the memoisation ratio
        from ~30x to 1x (PRD 9.1).
        """
        return canonical.validate_features(
            {
                "reason_class": self.reason_class,
                "diagnosis_class": diagnosis_class,
                "amount_band": self.amount_band,
                "segment": self.counterparty.segment,
                "legal_context": self.legal_context,
                "channel_eligibility": self.channel_eligibility,
                "hour_bucket": self.hour_bucket,
            }
        )

    def signature(self, diagnosis_class: str = "undiagnosed") -> str:
        return canonical.planner_signature(self.canonical_features(diagnosis_class))

    # -- persistence -----------------------------------------------------

    def to_row(self) -> Dict[str, Any]:
        """Flatten for SQLite. Latent truth is excluded on purpose.

        The store writes it to a separate table; see LatentTruth.
        """
        return {
            "event_id": self.event_id,
            "source_type": self.source_type,
            "amount_at_risk_paise": self.amount_at_risk_paise,
            "counterparty_id": self.counterparty.id,
            "counterparty_kind": self.counterparty.kind,
            "segment": self.counterparty.segment,
            "detected_at": self.detected_at,
            "decay_profile": self.decay_profile,
            "cause_signal": self.cause_signal,
            "legal_context": self.legal_context,
            "available_actions": canonical.canonical_json(list(self.available_actions)),
            "arm": self.arm,
            "external_ref": self.external_ref,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any], latent: Optional[LatentTruth] = None) -> "RiskEvent":
        import json

        return cls(
            event_id=row["event_id"],
            source_type=row["source_type"],
            amount_at_risk_paise=int(row["amount_at_risk_paise"]),
            counterparty=Counterparty(
                id=row["counterparty_id"],
                kind=row["counterparty_kind"],
                segment=row["segment"],
            ),
            detected_at=row["detected_at"],
            decay_profile=row["decay_profile"],
            cause_signal=row["cause_signal"],
            legal_context=row["legal_context"],
            available_actions=tuple(json.loads(row["available_actions"])),
            arm=row["arm"],
            external_ref=row["external_ref"],
            latent=latent,
        )

    def without_latent(self) -> "RiskEvent":
        """The agent's view of this event. Used wherever ground truth must not go."""
        return replace(self, latent=None)

    def detect_payload(self) -> Dict[str, Any]:
        """What a DETECT ledger row records.

        Identifiers are present here and that is correct: the ledger is the audit
        trail, and a finance controller has to reconcile a row against the
        settlement report. The prompt path is the one that must stay clean, and
        it goes through canonical_features() instead.
        """
        return {
            "event_id": self.event_id,
            "source_type": self.source_type,
            "external_ref": self.external_ref,
            "counterparty_id": self.counterparty.id,
            "amount_at_risk_paise": self.amount_at_risk_paise,
            "amount_band": self.amount_band,
            "cause_signal": self.cause_signal,
            "reason_class": self.reason_class,
            "legal_context": self.legal_context,
            "decay_profile": self.decay_profile,
            "segment": self.counterparty.segment,
            "hour_bucket": self.hour_bucket,
            "channel_eligibility": self.channel_eligibility,
            "available_actions": list(self.available_actions),
            "signature": self.signature(),
        }
