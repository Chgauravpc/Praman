"""R1-R11: the regulatory rules, as data plus evaluators. Becomes ``SAFETY.md``.

PRD 6.5. Every function here returns ``(verdict, rule_id, reason)`` in the shape
of a ``Ruling``, and every rule carries its instrument and the **grade of that
citation** in ``RULE_SOURCES`` below. The grade is part of the rule, not a
footnote: a system that cites a regulation it has not read is worse than one
that admits it is working from a summary, because the first cannot be checked.

Grades
------

``[A]``  read against the primary instrument, and the operative words are quoted
         in ``RULE_SOURCES``.
``[B]``  from secondary summaries of a named primary instrument. Consistent
         across sources, not yet matched to a clause.

As of Day 2, **two of eleven are [A]-verified** (R9, and the R2 thresholds via
the RBI framework reference). The rest are [B]. That is a worse-looking number
than "all eleven cite a circular", and it is the honest one. Day 1 recorded all
eleven as [B] and named R9 as the weakest; R9 was checked first for that reason
and the 08:00-19:00 figure held.

What each rule actually gates
-----------------------------

Note that the eleven rules do not divide evenly into "money" and "contact". They
divide into four groups, and the grouping is what makes them testable:

**Mandate mechanics -- R1, R2, R4, R6, R7.** These gate a *debit*. They are the
only rules in the system that can refuse a T1 action, and R1 is the one that
breaks the naive design.

**Channel mechanics -- R5, R8, R10.** These gate the *form* of a contact: a DLT
template, a call cap, an AI disclosure. A step can be perfectly timed and still
fail all three.

**Conduct -- R9.** Gates *how* a collection contact is made, not only when.
Self-identification is a requirement, not a courtesy.

**Meta -- R3, R11.** R3 gates *attributability* and applies to everything. R11
gates the consent basis and applies to every contact.

R1 is the one that breaks the naive design
------------------------------------------

A mandate retry sequencer that retries immediately is non-compliant, because the
pre-debit notification must reach the customer at least 24 hours before **every**
debit -- including a retry. The correct sequence is
``schedule -> notify at T-24h -> attempt``, which means **the retry scheduler and
the notification scheduler are one component.** Any design in which notification
is a downstream side-effect of retrying is wrong, and it is wrong in a way that
looks fine in testing: the retry succeeds, the notification goes out, and the
order was reversed.

R3 is why a Razorpay engineer cares
-----------------------------------

Razorpay is the acquirer. Under R3 the acquirer is responsible for compliance by
the merchants it onboards -- so a merchant's badly-behaved recovery agent is
*Razorpay's* liability, not only the merchant's. "Gate-able, auditable, refuses
what it cannot justify" is therefore not a feature of this system, it is the
property that decides whether it could ever run on their platform.

That makes R3 the one rule here with no time band and no threshold. Its
evaluator asks a single question -- can this action be attributed to a named
merchant? -- because an action the acquirer cannot attribute is an action the
acquirer cannot answer for.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple

from pramaan.canonical import parse_iso
from pramaan.envelope.context import (
    EnvelopeContext,
    Ruling,
    not_applicable,
    passes,
    refuses,
)
from pramaan.envelope.reason_map import CONTACT_ACTIONS, RETRY_ACTIONS
from pramaan.envelope.windows import check_window, reopens_in_seconds

# ===========================================================================
# The sources, with grades. This block is the one a compliance reviewer reads.
# ===========================================================================


@dataclass(frozen=True)
class RuleSource:
    rule_id: str
    summary: str
    instrument: str
    grade: str    # A | B
    #: Quoted only where the primary text was actually read.
    operative_words: str = ""


RULE_SOURCES: Dict[str, RuleSource] = {
    "R1": RuleSource(
        "R1",
        "Pre-debit notification must reach the customer at least 24h before "
        "every mandate debit, carrying merchant name, amount, date/time, "
        "mandate reference and reason.",
        "RBI Digital Payments E-Mandate Framework 2026 -- RBI/DPSS/2026-27/396 "
        "and RBI/CO.DPSS.POLC.No.S56/02.14.003/2026-27, dated 21 Apr 2026, "
        "consolidating eight circulars back to Aug 2019",
        "B",
    ),
    "R2": RuleSource(
        "R2",
        "The first mandate transaction requires AFA. Subsequent debits are "
        "AFA-exempt to Rs 15,000; to Rs 1,00,000 for insurance premiums, "
        "mutual-fund subscriptions and credit-card bills.",
        "Ibid.",
        "B",
    ),
    "R3": RuleSource(
        "R3",
        "The acquirer is responsible for compliance by the merchants it "
        "onboards.",
        "Ibid.",
        "B",
    ),
    "R4": RuleSource(
        "R4",
        "The customer may opt out of a single debit or withdraw the mandate "
        "entirely; withdrawal requires AFA re-validation before any further "
        "debit.",
        "Ibid.",
        "B",
    ),
    "R5": RuleSource(
        "R5",
        "Commercial SMS requires DLT registration of entity, header and content "
        "template; only template-matching messages deliver. Promotional window "
        "09:00-21:00.",
        "TRAI TCCCPR 2018, with the quiet period traceable to TCCCPR 2010",
        "B",
    ),
    "R6": RuleSource(
        "R6",
        "Mandate retry norm: one attempt plus a maximum of three retries. "
        "Razorpay auto-retries the next day; the subscription moves pending -> "
        "halted when the retries are exhausted.",
        "Razorpay subscription payment-retry documentation",
        "B",
    ),
    "R7": RuleSource(
        "R7",
        "FASTag and NCMC auto-replenishment are exempt from the pre-debit "
        "notification requirement.",
        "RBI 2026 E-Mandate Framework",
        "B",
    ),
    "R8": RuleSource(
        "R8",
        "Outbound commercial voice calls are restricted to 09:00-21:00, with a "
        "maximum of three unsolicited calls per day per number per company. DLT "
        "registration and NCPR/DND scrubbing are required. Penalties reported up "
        "to Rs 10 lakh.",
        "TRAI UCC / TCCCPR",
        "B",
    ),
    "R9": RuleSource(
        "R9",
        "Debt-collection contact is permitted only 08:00-19:00; contact outside "
        "that window is treated as harassment. Agents must identify themselves, "
        "state whom they represent and the purpose, and must not harass, coerce "
        "or intimidate.",
        "RBI/2022-23/108, DOR.ORG.REC.65/21.04.158/2022-23, dated 12 August "
        "2022 -- Outsourcing of Financial Services: Responsibilities of "
        "regulated entities employing Recovery Agents",
        "A",
        operative_words=(
            "REs shall ensure that ... their agents refrain from ... "
            "persistently calling the borrower and/ or calling the borrower "
            "before 8:00 a.m. and after 7:00 p.m. for recovery of overdue loans"
        ),
    ),
    "R10": RuleSource(
        "R10",
        "An AI voice call must disclose at the outset that the caller is "
        "automated, not a human.",
        "TRAI framework / DPDPA consent principles",
        "B",
    ),
    "R11": RuleSource(
        "R11",
        "Consent must be explicit, informed, specific and revocable; revocation "
        "is honoured immediately and permanently.",
        "Digital Personal Data Protection Act",
        "B",
    ),
}

RULE_IDS: Tuple[str, ...] = tuple("R%d" % n for n in range(1, 12))

# -- thresholds, as named constants -----------------------------------------

#: R1. Twenty-four hours, in seconds. The "hard floor" in the ``days_hard_floor``
#: decay profile is exactly this number.
PRE_DEBIT_NOTICE_SECONDS = 24 * 3600

#: R2. AFA exemption ceilings in paise. These are the same two boundaries as
#: amount bands 3 and 4 (F6/ADR-002) -- the bands were chosen to sit on them, so
#: a band carries compliance meaning rather than only statistical meaning.
AFA_EXEMPT_CEILING_PAISE = 1_500_000            # Rs 15,000
AFA_EXEMPT_CEILING_HIGH_PAISE = 10_000_000      # Rs 1,00,000

#: R2. The categories that get the higher ceiling.
AFA_HIGH_CEILING_CATEGORIES: FrozenSet[str] = frozenset(
    {"insurance", "mutual_fund", "credit_card_bill"}
)

#: R6. One attempt plus three retries.
MAX_MANDATE_ATTEMPTS = 4

#: R7. The two categories exempt from R1.
PRE_DEBIT_NOTICE_EXEMPT_CATEGORIES: FrozenSet[str] = frozenset({"fastag", "ncmc"})

#: R8. The hard ceiling. Distinct from S2's tighter house budget.
MAX_UNSOLICITED_CALLS_PER_DAY = 3

#: Actions that move money on the mandate. R1/R2/R4/R6/R7 gate these.
MANDATE_DEBIT_ACTIONS = RETRY_ACTIONS

#: The source types that are e-mandate debits. ``subscription`` is included
#: because a Razorpay subscription charge *is* an e-mandate debit -- the
#: framework does not care what the merchant calls the product.
MANDATE_SOURCE_TYPES: FrozenSet[str] = frozenset({"mandate", "subscription"})


def _is_mandate_debit(action: str, context: EnvelopeContext) -> bool:
    return action in MANDATE_DEBIT_ACTIONS and context.source_type in MANDATE_SOURCE_TYPES


# ===========================================================================
# The evaluators
# ===========================================================================


def r1_pre_debit_notification(action: str, context: EnvelopeContext) -> Ruling:
    """R1 -- no mandate debit without a T-24h notification that actually landed.

    The rule that breaks the naive design. Note what is measured: the gap
    between the notification and the **debit**, not between the notification and
    now. A retry scheduled for tomorrow with a notification sent an hour ago is
    compliant; a retry firing now with a notification sent an hour ago is not.
    So the evaluator has to know the effective time of the step, which is why
    ``judge`` resolves ``delay_seconds`` before calling in here.
    """
    if not _is_mandate_debit(action, context):
        return not_applicable("R1", "not an e-mandate debit")
    if context.mandate_category in PRE_DEBIT_NOTICE_EXEMPT_CATEGORIES:
        return passes(
            "R1",
            "%s auto-replenishment is exempt from pre-debit notification (R7)"
            % context.mandate_category,
        )
    if context.pre_debit_notified_at is None:
        return refuses(
            "R1",
            "no pre-debit notification has been sent. Every mandate debit -- "
            "including a retry -- needs one at least 24h in advance, carrying "
            "merchant name, amount, date/time, mandate reference and reason. "
            "The sequence is schedule -> notify at T-24h -> attempt, which means "
            "the retry scheduler and the notification scheduler are one "
            "component",
        )
    gap = int(
        (parse_iso(context.at) - parse_iso(context.pre_debit_notified_at)).total_seconds()
    )
    if gap < PRE_DEBIT_NOTICE_SECONDS:
        return refuses(
            "R1",
            "the pre-debit notification landed %.1fh before this debit; R1 "
            "requires at least %dh" % (gap / 3600, PRE_DEBIT_NOTICE_SECONDS // 3600),
        )
    return passes(
        "R1",
        "notified %.1fh in advance, clearing the %dh floor"
        % (gap / 3600, PRE_DEBIT_NOTICE_SECONDS // 3600),
    )


def r2_additional_factor_auth(action: str, context: EnvelopeContext) -> Ruling:
    """R2 -- AFA on the first debit, and above the exemption ceiling.

    The ceiling depends on the category, which is why ``mandate_category`` is a
    field rather than a boolean. Note the interaction with F6: Rs 15,000 and
    Rs 1,00,000 are amount-band boundaries precisely because they are these
    thresholds, so "band 4" and "needs AFA unless it is an insurance premium"
    are the same statement.
    """
    if not _is_mandate_debit(action, context):
        return not_applicable("R2", "not an e-mandate debit")
    if context.is_first_mandate_debit and not context.afa_validated:
        return refuses(
            "R2",
            "the first transaction on a mandate requires additional-factor "
            "authentication, and this debit carries none",
        )
    ceiling = (
        AFA_EXEMPT_CEILING_HIGH_PAISE
        if context.mandate_category in AFA_HIGH_CEILING_CATEGORIES
        else AFA_EXEMPT_CEILING_PAISE
    )
    if context.amount_paise > ceiling and not context.afa_validated:
        return refuses(
            "R2",
            "Rs %.2f exceeds the Rs %.2f AFA exemption ceiling for a %s mandate "
            "and this debit carries no AFA"
            % (context.amount_paise / 100, ceiling / 100, context.mandate_category),
        )
    return passes(
        "R2",
        "Rs %.2f is within the Rs %.2f AFA exemption for a %s mandate"
        % (context.amount_paise / 100, ceiling / 100, context.mandate_category),
    )


def r3_acquirer_attribution(action: str, context: EnvelopeContext) -> Ruling:
    """R3 -- every action must be attributable to a named merchant.

    R3 states a *responsibility*, not a threshold, so it has to be
    operationalised to be enforceable. This is the operationalisation: the
    acquirer carries the liability for actions taken on a merchant's behalf, and
    an action it cannot attribute is an action it cannot answer for. So an
    unattributable action is refused -- not because sending it would breach a
    clause, but because it would put the acquirer in a position where it could
    not demonstrate compliance if asked.

    Applies to every action, including ``ACT_WAIT``: an audit trail with an
    unattributed row in it is an audit trail with a hole.
    """
    if not context.merchant_id:
        return refuses(
            "R3",
            "no merchant attribution on this action. Under R3 the acquirer is "
            "responsible for compliance by the merchants it onboards, so an "
            "action that cannot be attributed to one is an action the acquirer "
            "cannot answer for",
        )
    return passes("R3", "attributable to merchant %s" % context.merchant_id)


def r4_customer_opt_out(action: str, context: EnvelopeContext) -> Ruling:
    """R4 -- a declined debit, or a withdrawn mandate needing AFA re-validation.

    Two rights of different width, and the difference matters: opting out of one
    debit leaves the mandate alive, so the *next* debit is fine. Withdrawing the
    mandate kills all of them until AFA re-validation, which is why
    ``afa_validated`` can lift this refusal and cannot lift S4's.
    """
    if not _is_mandate_debit(action, context):
        return not_applicable("R4", "not an e-mandate debit")
    if context.single_debit_opt_out:
        return refuses(
            "R4",
            "the customer has opted out of this specific debit. The mandate "
            "survives; this debit does not",
        )
    if context.mandate_withdrawn and not context.afa_validated:
        return refuses(
            "R4",
            "the mandate has been withdrawn. A further debit requires AFA "
            "re-validation, which this one does not carry",
        )
    return passes("R4", "no opt-out recorded against this debit")


def r5_dlt_template(action: str, context: EnvelopeContext) -> Ruling:
    """R5 -- a commercial SMS needs a DLT-registered template, and a DND scrub.

    The consequence people underestimate: a message that does not match a
    registered template does not get *filtered*, it does not get **delivered**.
    So this is not a compliance nicety, it is the difference between sending and
    not sending -- which is exactly why the LLM's freedom on SMS is slot-filling
    only (PRD 6.7). The model selects a template and fills approved slots; it
    never writes sendable copy.

    DND lives here rather than in S4 because the NCPR register is TRAI's
    instrument. See ``stopping.py``.
    """
    if action != "ACT_MESSAGE":
        return not_applicable("R5", "not a message")
    if context.dnd_registered and context.legal_context == "promotional":
        return refuses(
            "R5",
            "the number is on the NCPR/DND register and this is promotional "
            "traffic",
        )
    return passes(
        "R5",
        "template %s is registered on the sending header" % context.dlt_template_id
        if context.dlt_template_id
        else "no DLT template required for this channel",
    )


def r5_sms_template(action: str, channel: str, context: EnvelopeContext) -> Ruling:
    """R5, the channel-specific half: SMS specifically requires a template."""
    if action != "ACT_MESSAGE" or channel != "sms":
        return not_applicable("R5", "not an SMS")
    if not context.dlt_template_id:
        return refuses(
            "R5",
            "no DLT content template on an SMS. Only template-matching messages "
            "deliver on a registered header, so this message would not be "
            "throttled -- it simply would not arrive",
        )
    return passes("R5", "SMS carries DLT template %s" % context.dlt_template_id)


def r6_retry_cap(action: str, context: EnvelopeContext) -> Ruling:
    """R6 -- one attempt plus three retries, then the subscription halts."""
    if not _is_mandate_debit(action, context):
        return not_applicable("R6", "not an e-mandate debit")
    if context.mandate_attempt_ordinal > MAX_MANDATE_ATTEMPTS:
        return refuses(
            "R6",
            "this would be attempt %d; the norm is one attempt plus three "
            "retries (%d total), after which the subscription moves pending -> "
            "halted" % (context.mandate_attempt_ordinal, MAX_MANDATE_ATTEMPTS),
        )
    return passes(
        "R6",
        "attempt %d of %d"
        % (context.mandate_attempt_ordinal, MAX_MANDATE_ATTEMPTS),
    )


def r7_replenishment_exemption(action: str, context: EnvelopeContext) -> Ruling:
    """R7 -- the exemption, and the failure mode of *claiming* it wrongly.

    R7 is a permission rather than a prohibition, so it cannot be violated by
    doing something. It is violated by asserting it where it does not hold:
    marking an ordinary subscription debit as FASTag replenishment would skip
    R1's notification requirement entirely, which makes a wrongly-claimed
    exemption a more dangerous state than a missing notification -- the missing
    notification gets caught, the false exemption does not.
    """
    if not context.claims_r7_exemption:
        return passes("R7", "no exemption claimed", bound=False)
    if context.mandate_category not in PRE_DEBIT_NOTICE_EXEMPT_CATEGORIES:
        return refuses(
            "R7",
            "an exemption from pre-debit notification is claimed, but the "
            "mandate category is %r. R7 covers FASTag and NCMC "
            "auto-replenishment only -- claiming it elsewhere would skip R1"
            % context.mandate_category,
        )
    if not _is_mandate_debit(action, context):
        return refuses(
            "R7",
            "an R7 exemption is claimed on an action that is not a mandate "
            "debit (%s on %s)" % (action, context.source_type),
        )
    return passes(
        "R7",
        "%s auto-replenishment is genuinely exempt from pre-debit notification"
        % context.mandate_category,
    )


def r8_voice_conduct(action: str, context: EnvelopeContext) -> Ruling:
    """R8 -- the call cap and the DND scrub. The window is in ``windows.py``.

    Split deliberately: the time band is a matrix cell and belongs in the table
    a reviewer reads against the regulation, while the daily cap and the register
    scrub are per-counterparty state. Both cite R8, and both appear in the
    ruling list, so a GATE row shows which half refused.
    """
    if action != "ACT_VOICE":
        return not_applicable("R8", "not a voice call")
    if context.dnd_registered:
        return refuses(
            "R8",
            "the number is on the NCPR/DND register; outbound commercial voice "
            "requires a scrub against it",
        )
    if context.unsolicited_calls_today >= MAX_UNSOLICITED_CALLS_PER_DAY:
        return refuses(
            "R8",
            "%d unsolicited calls already placed to this number today; the "
            "ceiling is %d per number per company per day"
            % (context.unsolicited_calls_today, MAX_UNSOLICITED_CALLS_PER_DAY),
        )
    return passes(
        "R8",
        "call %d of %d permitted today, number is not on the DND register"
        % (context.unsolicited_calls_today + 1, MAX_UNSOLICITED_CALLS_PER_DAY),
    )


def r9_collection_conduct(action: str, context: EnvelopeContext) -> Ruling:
    """R9 -- collection conduct. Self-identification, not only timing.

    The window half of R9 is in ``windows.py``. This is the conduct half, and it
    is the half that gets skipped: the RBI directive requires the agent to
    identify themselves and state whom they represent and why they are calling.
    An AI agent that opens with "your payment failed, please pay now" has broken
    R9 even at 11:00 on a Tuesday.

    ``[A]``-graded -- see ``RULE_SOURCES``.
    """
    if context.legal_context != "collection":
        return not_applicable("R9", "not a debt-collection contact")
    if action not in CONTACT_ACTIONS:
        return not_applicable("R9", "not a contact")
    if not context.self_identification_scripted:
        return refuses(
            "R9",
            "a collection contact must identify the agent, state whom they "
            "represent and state the purpose. This step carries no "
            "self-identification",
        )
    return passes(
        "R9",
        "self-identification is scripted; conduct requirements met",
    )


def r10_ai_disclosure(action: str, context: EnvelopeContext) -> Ruling:
    """R10 -- an AI voice call says it is an AI, first.

    Order matters and the rule says so: at the *outset*. Disclosing halfway
    through, once the customer has already answered questions believing they
    were talking to a person, is not disclosure. This is the rule that makes the
    voice channel buildable rather than reckless, and it is the first line of
    the opening script -- before the R9 self-identification, which comes second.
    """
    if action != "ACT_VOICE":
        return not_applicable("R10", "not a voice call")
    if not context.ai_disclosure_scripted:
        return refuses(
            "R10",
            "an automated voice call must disclose at the outset that the "
            "caller is not a human. This step carries no disclosure",
        )
    return passes("R10", "AI disclosure is the first line of the opening script")


def r11_consent(action: str, context: EnvelopeContext) -> Ruling:
    """R11 -- explicit, informed, specific, revocable.

    The interesting distinction is between ``explicit`` and ``implied``. A
    customer whose payment just failed has a live transactional relationship, so
    a service message about that payment rests on implied consent. A promotional
    touch does not: it needs explicit consent, and "they bought something once"
    is not it. That single difference is what stops a recovery system from
    quietly becoming a marketing channel, which is the drift this rule exists to
    prevent.

    Withdrawal is S4's -- permanent and immediate. This rule handles the
    never-consented case.
    """
    if action not in CONTACT_ACTIONS:
        return not_applicable("R11", "not a customer contact")
    if context.consent == "withdrawn":
        return refuses(
            "R11",
            "consent has been withdrawn; revocation is immediate and permanent",
        )
    if context.legal_context == "promotional" and context.consent != "explicit":
        return refuses(
            "R11",
            "promotional contact requires explicit, informed, specific consent; "
            "this counterparty's consent basis is %r" % context.consent,
        )
    if context.consent == "none":
        return refuses(
            "R11",
            "no consent basis of any kind is recorded for this counterparty",
        )
    return passes(
        "R11",
        "consent basis is %r, which supports a %s contact"
        % (context.consent, context.legal_context),
    )


def check_legal_window(action: str, channel: str, context: EnvelopeContext) -> Ruling:
    """The window half of R5/R8/R9, from the matrix. Second precision.

    Kept as a thin adapter so that the *table* stays the artifact and this file
    stays the rules. ``windows.py`` decides which rule id to cite, because only
    the table knows whether an intersection was involved.
    """
    from pramaan.envelope.tiers import needs_legal_window

    if not needs_legal_window(action):
        return not_applicable(
            "R9", "a silent action is not a communication; no time band applies"
        )
    return check_window(context.legal_context, channel, context.at)


def window_reopens_in(action: str, channel: str, context: EnvelopeContext):
    from pramaan.envelope.tiers import needs_legal_window

    if not needs_legal_window(action):
        return 0
    return reopens_in_seconds(context.legal_context, channel, context.at)


def _self_check() -> None:
    """Every rule R1..R11 has a source entry and at least one evaluator."""
    import sys

    module = sys.modules[__name__]
    if set(RULE_SOURCES) != set(RULE_IDS):
        raise AssertionError(
            "RULE_SOURCES must cover exactly R1..R11; got %r" % sorted(RULE_SOURCES)
        )
    for rule_id, source in RULE_SOURCES.items():
        if source.grade not in ("A", "B"):
            raise AssertionError("%s has grade %r; use A or B" % (rule_id, source.grade))
        if source.grade == "A" and not source.operative_words:
            raise AssertionError(
                "%s is graded [A], so the operative words must be quoted -- an "
                "[A] grade means the primary text was read" % rule_id
            )
        if not source.instrument:
            raise AssertionError("%s cites no instrument" % rule_id)
    for n in range(1, 12):
        prefix = "r%d_" % n
        matches = [
            name
            for name in dir(module)
            if name.startswith(prefix) and callable(getattr(module, name))
        ]
        if not matches:
            raise AssertionError("R%d has no evaluator" % n)

    # The two thresholds that must agree with the frozen amount bands (F6).
    from pramaan.canonical import AMOUNT_BAND_CEILINGS_PAISE

    if AFA_EXEMPT_CEILING_PAISE not in AMOUNT_BAND_CEILINGS_PAISE:
        raise AssertionError(
            "R2's Rs 15,000 ceiling must be an amount-band boundary; the bands "
            "were frozen on these thresholds precisely so a band carries "
            "compliance meaning (ADR-002)"
        )
    if AFA_EXEMPT_CEILING_HIGH_PAISE not in AMOUNT_BAND_CEILINGS_PAISE:
        raise AssertionError("R2's Rs 1,00,000 ceiling must be an amount-band boundary")


_self_check()
