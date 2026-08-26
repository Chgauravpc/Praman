"""The Razorpay decline-reason taxonomy -- 69 codes collapsing into 10 classes.

This is not the brain. The planner decides; this table is the **guardrail the
envelope enforces against the planner** (PRD Appendix A).

The single most important fact encoded here is a divergence, and it is the trap
this project is built around:

- By **event volume**, most failures are recoverable. PRD 5.1 puts bank
  timeouts, wrong-PIN and insufficient-balance at 70-100% of real traffic, and
  all three are retry-eligible.
- By **code count**, 45 of these 69 codes cannot be resolved by a retry at all.

A builder who reasons from event volume concludes "retry basically always
applies" and ships something that is right most of the time and actively harmful
in the tail: retrying a risk decline is how a merchant gets penalised, and
chasing someone who has already paid is the humiliation case. The long tail of
codes is precisely where the damage lives, which is why this lives in a hard
guardrail rather than in a heuristic.

Both the simulator (sim/generate.py) and the envelope (Day 2) import from here.
One table, one source of truth -- if these two ever disagreed, the eval would
measure a workload the envelope does not police.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple

# --------------------------------------------------------------------------
# The ten classes
# --------------------------------------------------------------------------

REASON_CLASSES: Tuple[str, ...] = (
    "TECH_TRANSIENT",
    "AUTH_DROPOFF",
    "FUNDS",
    "LIMIT",
    "INSTRUMENT_DEAD",
    "MERCHANT_CONFIG",
    "INTEGRATION_BUG",
    "ALREADY_PAID",
    "RISK",
    "ELIGIBILITY",
)

#: How a retry may be attempted, if at all.
#:
#: immediate   -- a silent same-rail retry is legitimate
#: scheduled   -- retry, but not now and possibly not on this rail
#: never       -- structurally cannot succeed; the envelope rejects it
#: human_only  -- automated retry is prohibited, not merely useless
RETRY_MODES: Tuple[str, ...] = ("immediate", "scheduled", "never", "human_only")

#: Whether contacting the *customer* is defensible.
#:
#: allowed     -- a message or call can be justified
#: waste       -- permitted but pointless; the envelope rejects it as spend with
#:                no expected return. TECH_TRANSIENT is the big one: the bank is
#:                down, the customer cannot do anything about it, and the UPI app
#:                is probably already retrying on its own.
#: prohibited  -- contacting the customer is wrong at any hour, for any amount
CONTACT_VERDICTS: Tuple[str, ...] = ("allowed", "waste", "prohibited")


@dataclass(frozen=True)
class ReasonClassPolicy:
    """The guardrail for one class. Deterministic, and it names its own rule."""

    name: str
    retry_mode: str
    contact: str
    envelope_note: str


REASON_CLASS_POLICY: Dict[str, ReasonClassPolicy] = {
    "TECH_TRANSIENT": ReasonClassPolicy(
        "TECH_TRANSIENT",
        "immediate",
        "waste",
        "Silent retry only; contact actions rejected as waste. The bank is "
        "degraded, not the customer.",
    ),
    "AUTH_DROPOFF": ReasonClassPolicy(
        "AUTH_DROPOFF",
        "immediate",
        "allowed",
        "Longest settle window -- the highest organic self-recovery class. The "
        "customer is very likely fixing their PIN inside their banking app "
        "right now, so acting early is how you pay to message someone who has "
        "already paid.",
    ),
    "FUNDS": ReasonClassPolicy(
        "FUNDS",
        "scheduled",
        "allowed",
        "Retry timed to the credit cycle. An immediate retry fails for exactly "
        "the same reason the first attempt did.",
    ),
    "LIMIT": ReasonClassPolicy(
        "LIMIT",
        "scheduled",
        "allowed",
        "Rail switch or scheduled retry; no immediate same-rail retry, which "
        "would hit the same cap.",
    ),
    "INSTRUMENT_DEAD": ReasonClassPolicy(
        "INSTRUMENT_DEAD",
        "never",
        "allowed",
        "Retry is structurally rejected -- it cannot succeed. Re-collect the "
        "instrument. A retry here is guaranteed waste *plus* a wasted contact.",
    ),
    "MERCHANT_CONFIG": ReasonClassPolicy(
        "MERCHANT_CONFIG",
        "never",
        "prohibited",
        "Customer contact rejected. This is the merchant's own configuration -- "
        "alert them, and never make it the customer's problem.",
    ),
    "INTEGRATION_BUG": ReasonClassPolicy(
        "INTEGRATION_BUG",
        "never",
        "prohibited",
        "Page an engineer. The merchant's code is wrong; no customer-facing "
        "action can fix it.",
    ),
    "ALREADY_PAID": ReasonClassPolicy(
        "ALREADY_PAID",
        "never",
        "prohibited",
        "S1 -- terminate the entire thread. The tripwire against chasing "
        "someone who has already paid, and the single most important rule in "
        "the system.",
    ),
    "RISK": ReasonClassPolicy(
        "RISK",
        "human_only",
        "prohibited",
        "Auto-retry is structurally impossible. Retrying a risk decline is how "
        "a merchant gets penalised.",
    ),
    "ELIGIBILITY": ReasonClassPolicy(
        "ELIGIBILITY",
        "never",
        "allowed",
        "Not resolvable by retry. Offer an alternate method.",
    ),
}

#: Classes where customer contact is not defensible -- either prohibited
#: outright, or pure waste. Consumed by canonical.channel_eligibility, so it
#: feeds the planner signature directly.
NO_CONTACT_CLASSES: FrozenSet[str] = frozenset(
    name for name, p in REASON_CLASS_POLICY.items() if p.contact != "allowed"
)

#: The 24 retry-eligible codes live in these classes -- but note that class
#: membership is not sufficient. See amount_less_than_minimum_amount below.
RETRY_CAPABLE_CLASSES: FrozenSet[str] = frozenset(
    name
    for name, p in REASON_CLASS_POLICY.items()
    if p.retry_mode in ("immediate", "scheduled")
)


# --------------------------------------------------------------------------
# The 69 codes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReasonCode:
    code: str
    reason_class: str
    #: Per-code override of the class default. Exists for exactly one code
    #: today, and that one code is the reason this field is not derived: the
    #: LIMIT class *can* be retried, but a payment below the minimum amount
    #: cannot -- no schedule and no rail switch makes an under-minimum payment
    #: acceptable. Collapsing this into the class would silently make the
    #: retry-eligible count 25 instead of 24.
    retry_eligible: bool

    @property
    def policy(self) -> ReasonClassPolicy:
        return REASON_CLASS_POLICY[self.reason_class]

    @property
    def retry_mode(self) -> str:
        return "never" if not self.retry_eligible else self.policy.retry_mode

    @property
    def contact(self) -> str:
        return self.policy.contact


def _codes(reason_class: str, *codes: str) -> Tuple[ReasonCode, ...]:
    retryable = reason_class in RETRY_CAPABLE_CLASSES
    return tuple(ReasonCode(c, reason_class, retryable) for c in codes)


REASON_CODES: Tuple[ReasonCode, ...] = (
    # -- retry-eligible: 24 codes across four classes ----------------------
    *_codes(
        "TECH_TRANSIENT",
        "bank_technical_error",
        "upi_app_technical_error",
        "server_error",
        "payment_timed_out",
        "verification_failed",
        "payment_failed",
    ),
    *_codes(
        "AUTH_DROPOFF",
        "authentication_failed",
        "incorrect_otp",
        "otp_expired",
        "otp_attempts_exceeded",
        "incorrect_cvv",
        "incorrect_pin",
        "incorrect_atm_pin",
        "pin_attempts_exceeded",
        "payment_cancelled",
        "incorrect_card_details",
        "incorrect_card_expiry_date",
        "incorrect_cardholder_name",
        "card_number_invalid",
        "card_type_invalid",
    ),
    *_codes("FUNDS", "insufficient_funds"),
    *_codes(
        "LIMIT",
        "transaction_limit_exceeded",
        "transaction_daily_limit_exceeded",
        "transaction_frequency_limit_exceeded",
    ),
    # -- the one per-code exception ----------------------------------------
    # A LIMIT-class code that no retry can resolve. Counted by PRD Appendix A
    # under "not resolvable by retry", not under retry-eligible.
    ReasonCode("amount_less_than_minimum_amount", "LIMIT", False),
    # -- hard never-retry: 38 codes ----------------------------------------
    *_codes(
        "INSTRUMENT_DEAD",
        "card_expired",
        "bank_account_invalid",
        "bank_account_validation_failed",
        "debit_instrument_blocked",
        "transaction_on_vpa_restricted",
        "invalid_vpa",
        "card_not_enrolled",
        "pin_not_set",
        "user_not_registered_for_netbanking",
        "upi_autopay_not_supported_on_psp",
    ),
    *_codes(
        "MERCHANT_CONFIG",
        "bank_not_enabled",
        "card_network_not_enabled",
        "payment_method_not_enabled",
        "upi_collect_not_enabled",
        "upi_intent_not_enabled",
        "recurring_payment_not_enabled",
        "live_mode_not_enabled",
        "merchant_not_activated",
        "international_transaction_not_allowed",
        "refund_limit_crossed",
    ),
    *_codes(
        "INTEGRATION_BUG",
        "invalid_order_id",
        "order_amount_mismatch",
        "order_payment_method_mismatch",
        "input_validation_failed",
        "invalid_request",
        "invalid_amount",
        "invalid_currency",
        "duplicate_request",
        "duplicate_refund_id",
        "mismatch_in_transaction_details",
        "record_not_found",
        "invalid_device",
        "invalid_email",
        "invalid_mobile_number",
        "mobile_number_invalid",
        "invalid_user_details",
        "capture_failed",
    ),
    *_codes("ALREADY_PAID", "order_already_paid"),
    # -- never auto-retry, human only: 3 codes -----------------------------
    *_codes(
        "RISK",
        "payment_risk_check_failed",
        "compliance_violation",
        "payment_pending_approval",
    ),
    # -- not resolvable by retry: 3 codes ----------------------------------
    *_codes(
        "ELIGIBILITY",
        "user_not_eligible",
        "emi_plan_unavailable",
        "emi_greater_than_max_amount",
    ),
)

BY_CODE: Dict[str, ReasonCode] = {rc.code: rc for rc in REASON_CODES}

CODES_BY_CLASS: Dict[str, Tuple[str, ...]] = {
    cls: tuple(rc.code for rc in REASON_CODES if rc.reason_class == cls)
    for cls in REASON_CLASSES
}


def reason_class_of(code: str) -> str:
    """Class for a raw Razorpay error reason.

    Unknown codes are a real production case (Razorpay adds reasons), and the
    safe default is the class that permits nothing: an unrecognised failure gets
    escalated, never retried and never messaged.
    """
    entry = BY_CODE.get(code)
    return entry.reason_class if entry is not None else "RISK"


def is_retry_eligible(code: str) -> bool:
    entry = BY_CODE.get(code)
    return bool(entry) and entry.retry_eligible


def contact_verdict(code: str) -> str:
    return REASON_CLASS_POLICY[reason_class_of(code)].contact


# --------------------------------------------------------------------------
# The deterministic reason-class default action
# --------------------------------------------------------------------------
#
# Two consumers, and naming both explains why a lookup table sits in an
# LLM-forward codebase on purpose:
#
# 1. **Arm B** (Day 3) is exactly this map and nothing else. It is the control
#    that answers "does a lookup table already solve this?", so the C-B contrast
#    is only meaningful if arm B is a *fair* table rather than a strawman. This
#    is the strongest table the taxonomy supports.
# 2. **NFR-2**: on a planner cache miss the LLM cannot answer inside the 2s
#    decision-compute SLO, so the first action is taken from here while the
#    planner runs asynchronously. That makes this a live path exercised on every
#    cold signature -- not dead fallback code.

DEFAULT_ACTION_BY_CLASS: Dict[str, str] = {
    "TECH_TRANSIENT": "ACT_WAIT",             # they are probably already retrying
    "AUTH_DROPOFF": "ACT_WAIT",               # highest organic self-recovery
    "FUNDS": "ACT_RETRY",                     # scheduled to the credit cycle
    "LIMIT": "ACT_ROUTE",                     # switch rail rather than re-hit the cap
    "INSTRUMENT_DEAD": "ACT_MESSAGE",         # re-collect the instrument
    "MERCHANT_CONFIG": "ACT_ALERT_MERCHANT",  # not the customer's problem
    "INTEGRATION_BUG": "ACT_PAGE_ENGINEER",   # the merchant's code
    "ALREADY_PAID": "ACT_STOP",               # S1
    "RISK": "ACT_ESCALATE_HUMAN",             # never automate a risk decline
    "ELIGIBILITY": "ACT_MESSAGE",             # offer an alternate method
}


def default_action(code: str) -> str:
    return DEFAULT_ACTION_BY_CLASS[reason_class_of(code)]


# --------------------------------------------------------------------------
# Self-check -- the counts PRD Appendix A publishes
# --------------------------------------------------------------------------
#
# These run at import. They are cheap, and they are the difference between a
# transcribed table and a *verified* one: the PRD states 69 / 24 / 38 / 45 as
# facts, the README will quote them, and a panel may well count. If a code is
# ever added to the wrong class or duplicated, this fails at import rather than
# quietly shifting a published number.

TOTAL_CODES = 69
RETRY_ELIGIBLE_CODES = 24
NOT_RETRYABLE_CODES = 45  # 69 - 24; the number that makes the tail dangerous

_expected_class_sizes = {
    "TECH_TRANSIENT": 6,
    "AUTH_DROPOFF": 14,
    "FUNDS": 1,
    "LIMIT": 4,
    "INSTRUMENT_DEAD": 10,
    "MERCHANT_CONFIG": 10,
    "INTEGRATION_BUG": 17,
    "ALREADY_PAID": 1,
    "RISK": 3,
    "ELIGIBILITY": 3,
}


def _self_check() -> None:
    codes = [rc.code for rc in REASON_CODES]
    duplicates = sorted({c for c in codes if codes.count(c) > 1})
    if duplicates:
        raise AssertionError("duplicate reason codes: %r" % duplicates)
    if len(codes) != TOTAL_CODES:
        raise AssertionError(
            "PRD Appendix A publishes %d reason codes, table holds %d"
            % (TOTAL_CODES, len(codes))
        )
    for cls, expected in _expected_class_sizes.items():
        actual = len(CODES_BY_CLASS[cls])
        if actual != expected:
            raise AssertionError(
                "class %s should hold %d codes, holds %d" % (cls, expected, actual)
            )
    retryable = sum(1 for rc in REASON_CODES if rc.retry_eligible)
    if retryable != RETRY_ELIGIBLE_CODES:
        raise AssertionError(
            "PRD Appendix A publishes %d retry-eligible codes, table holds %d"
            % (RETRY_ELIGIBLE_CODES, retryable)
        )
    if TOTAL_CODES - retryable != NOT_RETRYABLE_CODES:
        raise AssertionError("the 45-of-69 claim no longer holds")
    if set(DEFAULT_ACTION_BY_CLASS) != set(REASON_CLASSES):
        raise AssertionError("every reason class needs a deterministic default action")


_self_check()
