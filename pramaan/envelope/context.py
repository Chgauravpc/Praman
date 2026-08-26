"""The envelope's inputs and outputs, and the rule-ID namespace.

Two objects and one convention live here, and the convention is the load-bearing
part.

``EnvelopeContext``
    Everything a rule may read. Flat, frozen, and explicitly enumerated: a rule
    cannot reach into a database, call a service, or read a clock, so a verdict
    is a pure function of (step, context) and is therefore reproducible and
    testable. Every field has a default, so a test states only what it is
    actually about.

``Ruling`` / ``Judgement``
    A ruling is one rule's opinion. A judgement is the envelope's decision, and
    it carries **every** ruling that was evaluated -- not just the decisive one.
    That is what makes the ledger row answerable to "why was this allowed?"
    rather than only to "why was this refused?".

The rule-ID namespace -- and why it has four prefixes rather than one
---------------------------------------------------------------------

It would be tidier to number every constraint R1, R2, R3... It would also be
dishonest, because the four kinds of constraint in this system have very
different authority, and a reviewer is entitled to tell them apart at a glance:

======  =========================================================  ============
prefix  what it is                                                 authority
======  =========================================================  ============
``R``   A regulatory rule. R1-R11, PRD 6.5. Each cites a named     regulator
        instrument, and the grade of that citation is recorded
        next to it in ``rules.py``.
``G``   A guardrail derived from Razorpay's own documented          vendor docs
        decline reasons (PRD Appendix A). Not law -- physics.
        Retrying ``card_expired`` is not illegal, it is futile.
``S``   A stopping rule. S1-S7, PRD 6.6. Product and conduct        us
        decisions, several of them backed by a regulation but
        none of them *identical* to one.
``P``   A house policy with no regulation behind it. Named so       us
        that nobody can mistake a threshold we chose for a
        threshold somebody imposed on us.
======  =========================================================  ============

The rule that matters: **never cite an ``R`` id for something a regulator did
not say.** A promotional email at 03:00 is bad practice, not a TRAI offence, so
it is refused by ``P1`` and not by ``R5``. Padding the regulatory namespace with
house rules would make the compliance story look stronger and be worth less,
because the first time a lawyer checked one citation and found a preference, they
would stop trusting the other ten.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# -- verdicts ---------------------------------------------------------------
#
# Three values, never collapsed to a boolean. A boolean cannot express "this
# action is fine but not in this form", which is the majority of what a planner
# gets wrong, and it cannot carry the rule id, which is the whole point.

ALLOW = "ALLOW"
AMEND = "AMEND"
REJECT = "REJECT"

VERDICTS: Tuple[str, ...] = (ALLOW, AMEND, REJECT)

#: The id used when nothing in the envelope constrains the action. It is a real
#: id rather than ``None`` so that ``rule_id`` is a non-null column: a GATE row
#: with no rule is indistinguishable from a GATE row whose rule was lost.
NO_RULE_BINDS = "P0"


@dataclass(frozen=True)
class Ruling:
    """One rule's opinion about one proposed step."""

    rule_id: str
    verdict: str
    reason: str
    #: True when this rule looked at the step and had jurisdiction over it. A
    #: rule that does not apply returns ``ALLOW`` with ``bound=False``, which is
    #: how ``judge`` knows R9 is worth citing on an 18:55 collection contact and
    #: not worth citing on a silent retry.
    bound: bool = True

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError("unknown verdict %r" % self.verdict)

    @property
    def blocks(self) -> bool:
        return self.verdict in (AMEND, REJECT)


def passes(rule_id: str, reason: str, *, bound: bool = True) -> Ruling:
    return Ruling(rule_id=rule_id, verdict=ALLOW, reason=reason, bound=bound)


def refuses(rule_id: str, reason: str) -> Ruling:
    return Ruling(rule_id=rule_id, verdict=REJECT, reason=reason, bound=True)


def not_applicable(rule_id: str, reason: str = "does not apply to this step") -> Ruling:
    return Ruling(rule_id=rule_id, verdict=ALLOW, reason=reason, bound=False)


# -- consent, mandate and promise vocabularies ------------------------------

#: DPDPA consent states (R11). ``implied`` covers the service-message case: a
#: customer whose payment just failed has a transactional relationship, and a
#: retry link is arguably implied-consent traffic. It is *not* enough for a
#: promotional touch, and R11's evaluator enforces that difference.
CONSENT_STATES: Tuple[str, ...] = ("explicit", "implied", "none", "withdrawn")

#: R2's exemption ceiling depends on the category, and R7's exemption from R1
#: depends on it too, so it is one field rather than two booleans.
MANDATE_CATEGORIES: Tuple[str, ...] = (
    "standard",           # AFA-exempt to Rs 15,000 (R2)
    "insurance",          # AFA-exempt to Rs 1,00,000 (R2)
    "mutual_fund",        # AFA-exempt to Rs 1,00,000 (R2)
    "credit_card_bill",   # AFA-exempt to Rs 1,00,000 (R2)
    "fastag",             # exempt from pre-debit notification (R7)
    "ncmc",               # exempt from pre-debit notification (R7)
)

PROMISE_STATES: Tuple[str, ...] = ("none", "promised", "kept", "partial", "broken")


@dataclass(frozen=True)
class EnvelopeContext:
    """Everything the rules may read. No clock, no network, no database.

    Defaults describe the ordinary, benign case -- a service-context payment
    failure for a consenting customer with no contact history -- so that a test
    for one rule sets one field and the reader can see exactly what the test is
    about.
    """

    # -- the event -------------------------------------------------------
    #: Event time, ISO-8601 with an explicit offset. The window rules are
    #: evaluated at *second* precision against this, never against a bucket.
    at: str = "2026-08-03T10:00:00+05:30"
    legal_context: str = "service"           # service | collection | promotional
    source_type: str = "payment"             # PRD 3.1 SOURCE_TYPES
    reason_code: str = "bank_technical_error"
    amount_paise: int = 250_000              # Rs 2,500 -- band 2
    counterparty_id: str = "cp_demo"
    #: R3: the acquirer answers for actions taken on a merchant's behalf, so an
    #: unattributable action is refused. Defaulted, because almost every test is
    #: about something else.
    merchant_id: Optional[str] = "acct_demo"

    # -- e-mandate state (R1, R2, R4, R6, R7) ----------------------------
    #: When the T-24h pre-debit notification actually reached the customer.
    #: ``None`` means it did not. R1 is the rule that breaks the naive design.
    pre_debit_notified_at: Optional[str] = None
    #: 1 = the original attempt. R6 permits one attempt plus three retries.
    mandate_attempt_ordinal: int = 1
    is_first_mandate_debit: bool = False
    afa_validated: bool = False
    mandate_category: str = "standard"
    mandate_withdrawn: bool = False
    #: R4's narrower right: the customer may decline *this* debit without
    #: withdrawing the mandate.
    single_debit_opt_out: bool = False

    # -- consent and registers (R5, R8, R11, S4) -------------------------
    consent: str = "implied"
    #: TRAI's NCPR / DND register. Enforced by R5 for messages and R8 for voice,
    #: because that is the regulator that owns the register -- see stopping.py
    #: for why it is deliberately not S4's job.
    dnd_registered: bool = False

    # -- contact history (S2, R8) ----------------------------------------
    contacts_last_24h: int = 0
    contacts_last_7d: int = 0
    #: R8's hard ceiling is three unsolicited calls per day per number per
    #: company. Counted separately from ``contacts_last_24h`` because the
    #: regulator counts calls, not touches.
    unsolicited_calls_today: int = 0

    # -- promise state (S3) -----------------------------------------------
    promise_state: str = "none"
    #: Days until the promised date. Positive means the promise is still live.
    promise_due_in_days: Optional[int] = None

    # -- merchant health (S5) ---------------------------------------------
    #: Share of this merchant's campaign that complained / unsubscribed. S5
    #: protects the merchant's DLT header, which if blocked ends their ability
    #: to message anyone at all.
    merchant_complaint_rate: float = 0.0
    merchant_unsubscribe_rate: float = 0.0

    # -- conversation signals (S7) ----------------------------------------
    distress_signal: bool = False
    dispute_signal: bool = False
    legal_threat_signal: bool = False

    # -- already-paid tripwire (S1) ---------------------------------------
    #: Smart Collect reconciliation: a virtual-account credit matching this
    #: order. The second half of S1, alongside the ``order_already_paid`` code.
    virtual_account_credited: bool = False

    # -- channel plumbing (R5, R9, R10) -----------------------------------
    #: R5: only template-matching messages deliver on a DLT-registered header.
    #: A message with no template is not "slightly non-compliant", it is
    #: undeliverable.
    dlt_template_id: Optional[str] = None
    #: R10: an AI voice call must say so at the outset.
    ai_disclosure_scripted: bool = False
    #: R9: a collection contact must identify the agent and whom they represent.
    self_identification_scripted: bool = False
    #: R7 is an exemption, so its failure mode is *claiming* it wrongly.
    claims_r7_exemption: bool = False

    # -- T4 ---------------------------------------------------------------
    human_approved: bool = False

    def __post_init__(self) -> None:
        if self.consent not in CONSENT_STATES:
            raise ValueError("unknown consent state %r" % self.consent)
        if self.mandate_category not in MANDATE_CATEGORIES:
            raise ValueError("unknown mandate category %r" % self.mandate_category)
        if self.promise_state not in PROMISE_STATES:
            raise ValueError("unknown promise state %r" % self.promise_state)
        if isinstance(self.amount_paise, bool) or not isinstance(self.amount_paise, int):
            raise TypeError("amount_paise must be integer paise")


@dataclass(frozen=True)
class Judgement:
    """The envelope's decision about one step, with its whole reasoning."""

    verdict: str
    rule_id: str
    reason: str
    rulings: Tuple[Ruling, ...] = ()
    #: Populated only for AMEND: the step the envelope would permit instead.
    #: Typed as a plain dict of changed fields so this module needs no schema
    #: import and stays free of anything that could pull in a model.
    amendment: Optional[Dict[str, Any]] = None
    #: S1, S4 and S7 do not refuse a step, they end the thread. The distinction
    #: matters downstream: a refused step may be re-proposed in another form, a
    #: terminated thread may not be re-proposed at all.
    terminates_thread: bool = False
    #: Advisory only, never a permission: the number of seconds until the window
    #: that closed this step opens again. ``None`` when it never will.
    reopens_in_seconds: Optional[int] = None

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError("unknown verdict %r" % self.verdict)
        if not self.rule_id:
            raise ValueError(
                "a judgement without a rule_id is exactly what this component "
                "exists to prevent (invariant I3)"
            )

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOW

    @property
    def bound_rules(self) -> Tuple[str, ...]:
        """Every rule that had jurisdiction, in evaluation order."""
        return tuple(r.rule_id for r in self.rulings if r.bound)

    def ledger_fields(self) -> Dict[str, Any]:
        """The GATE row this judgement produces (PRD 12.2).

        ``rule_fired`` and ``decision`` are dedicated ledger columns precisely
        so that "how often did R9 refuse an evening collection attempt?" is a
        ``GROUP BY``, not a log grep.
        """
        return {
            "decision": self.verdict,
            "rule_fired": self.rule_id,
            "payload": {
                "verdict": self.verdict,
                "rule_id": self.rule_id,
                "reason": self.reason,
                "bound_rules": list(self.bound_rules),
                "terminates_thread": self.terminates_thread,
                "amendment": self.amendment,
            },
        }
