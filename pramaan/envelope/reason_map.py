"""The reason-code guardrail: G1-G8, the envelope's view of PRD Appendix A.

**This file deliberately does not re-transcribe the 69 codes.** They already live
in ``pramaan/taxonomy.py``, which the simulator also imports, and two copies of a
69-row table is exactly how the eval ends up measuring a workload the envelope
does not police. Day 2's job here is the other half: turning that table into
*named, citable guardrails* the envelope can enforce against a planner, and
checking at import that the projection still covers all ten classes.

Why these are ``G`` and not ``R``
---------------------------------

Nothing in this file is law. It is derived from Razorpay's own documented decline
reasons **[A]**, and it describes what is *possible*, not what is *permitted*.
Retrying ``card_expired`` breaks no regulation -- it simply cannot succeed, and
the money spent finding that out is gone. Filing that under R-for-regulation
would make the compliance story look broader and be worth less; see the
namespace note in ``context.py``.

Two of the eight are the ones that carry real correctness, and they carry it for
opposite reasons:

``G1`` **INSTRUMENT_DEAD**  -- a retry is *futile*. The failure is a wasted API
    call plus, usually, a wasted contact on top of it. Cost: money and patience.

``G5`` **RISK** -- a retry is *harmful*. Retrying a risk decline is how a
    merchant gets penalised, and no amount of it will produce a success. Cost:
    the merchant's standing with the network.

And ``G4``/``S1`` **ALREADY_PAID** is the anti-humiliation tripwire, which is the
single most important branch in the system: everything else here costs money, and
this one costs the customer relationship.

The trap this file is built around
----------------------------------

By **code count**, 45 of 69 codes cannot be resolved by a retry. By **event
volume**, most failures can be -- PRD 5.1 puts bank timeouts, wrong PIN and
insufficient balance at 70-100% of real traffic and all three are retry-eligible.
A builder who reasons from volume ships something that is right most of the time
and harmful in the tail. Hence a hard guardrail rather than a heuristic: the
planner may propose whatever it likes, and these eight rules are what it has to
get past.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Optional, Tuple

from pramaan.envelope.context import Ruling, not_applicable, passes, refuses
from pramaan.taxonomy import (
    BY_CODE,
    REASON_CLASSES,
    REASON_CLASS_POLICY,
    reason_class_of,
)

# -- which actions are which ------------------------------------------------
#
# Two coarse sets, because every guardrail here answers one of exactly two
# questions: may this money move, and may this customer be contacted.

#: Actions that attempt to move money on the existing instrument. ``ACT_ROUTE``
#: is not one of them: switching rail is the *sanctioned response* to a LIMIT
#: failure, so treating it as a retry would refuse the correct action.
RETRY_ACTIONS: FrozenSet[str] = frozenset({"ACT_RETRY"})

#: Actions that put something in front of the customer. The asymmetry PRD 6.5
#: insists on: a retry is refundable, a message is not, and a phone call really
#: is not -- so this is the set that gets the strict treatment, not the one
#: above.
CONTACT_ACTIONS: FrozenSet[str] = frozenset(
    {"ACT_MESSAGE", "ACT_VOICE", "ACT_CONCESSION"}
)

#: Actions that reach nobody outside the merchant's own systems. Always
#: available: an envelope that cannot say "alert the merchant" for a
#: misconfiguration has no way to be useful about the 20 codes that are the
#: merchant's own fault.
INTERNAL_ACTIONS: FrozenSet[str] = frozenset(
    {"ACT_WAIT", "ACT_ROUTE", "ACT_ALERT_MERCHANT", "ACT_PAGE_ENGINEER", "ACT_STOP"}
)

# -- the guardrails, one per row of Appendix A -----------------------------

#: The five classes in which a retry must never be proposable. Named as data
#: because it is the single list a reviewer will want to check, and because the
#: build instruction for this day names exactly these five.
RETRY_FORBIDDEN_CLASSES: Dict[str, Tuple[str, str]] = {
    "INSTRUMENT_DEAD": (
        "G1",
        "the instrument cannot succeed -- an expired card, a dead VPA, a "
        "netbanking user who was never enrolled. Re-collect the instrument; a "
        "retry is a guaranteed failure plus, usually, a wasted contact",
    ),
    "MERCHANT_CONFIG": (
        "G2",
        "this is the merchant's own configuration. A retry hits the same "
        "disabled method; the fix is a settings change, and the addressee is "
        "the merchant, never the customer",
    ),
    "INTEGRATION_BUG": (
        "G3",
        "the merchant's integration is wrong -- a mismatched order amount, an "
        "invalid currency. No customer-facing action can fix a defect in code",
    ),
    "ALREADY_PAID": (
        "G4",
        "the money has arrived. A retry would double-charge somebody who has "
        "already paid, which is the one failure that is worse than not "
        "recovering the money at all (see S1)",
    ),
    "RISK": (
        "G5",
        "a risk or compliance decline. Auto-retry is not merely useless here, "
        "it is harmful: retrying a risk decline is how a merchant gets "
        "penalised. Human review only",
    ),
}

#: Classes where contacting the *customer* is wrong at any hour, for any amount,
#: through any channel. Note MERCHANT_CONFIG and INTEGRATION_BUG: the failure is
#: real and the customer can do precisely nothing about it, so a message is
#: passing the merchant's own bug to the person who was trying to pay them.
CONTACT_FORBIDDEN_CLASSES: Dict[str, Tuple[str, str]] = {
    "MERCHANT_CONFIG": (
        "G2",
        "the customer cannot fix the merchant's configuration. Contacting them "
        "converts the merchant's mistake into the customer's problem",
    ),
    "INTEGRATION_BUG": (
        "G3",
        "the customer cannot fix the merchant's code. Page an engineer",
    ),
    "ALREADY_PAID": (
        "G4",
        "chasing somebody who has already paid. This is the humiliation case, "
        "and it is the tripwire the whole envelope is built around (S1)",
    ),
    "RISK": (
        "G5",
        "a risk decline is not a conversation to have with the customer. It "
        "goes to a human reviewer",
    ),
}

#: G6. Contact is *permitted* here and still refused, because it has no expected
#: return. TECH_TRANSIENT means the bank is degraded, not the customer: they can
#: do nothing, their UPI app is very likely retrying already, and the message is
#: pure spend against the merchant's DLT reputation. This is the rule that stops
#: an agent from being busy instead of useful.
CONTACT_WASTE_CLASSES: Dict[str, Tuple[str, str]] = {
    "TECH_TRANSIENT": (
        "G6",
        "the bank or the PSP is degraded, not the customer -- there is nothing "
        "for them to act on, and their app is probably retrying already. A "
        "message here is spend with no expected return",
    ),
}

#: G7. The retry is legitimate; the *timing* is not. An immediate same-rail
#: retry on insufficient funds fails for exactly the reason the first attempt
#: did, and on a daily cap it re-hits the same cap. This is the envelope's one
#: genuinely amendable finding -- see ``judge.py``.
SCHEDULED_ONLY_CLASSES: Dict[str, Tuple[str, str]] = {
    "FUNDS": (
        "G7",
        "insufficient balance. An immediate retry fails for exactly the same "
        "reason the first attempt did; time it to the credit cycle",
    ),
    "LIMIT": (
        "G7",
        "a per-transaction, daily or frequency cap. An immediate same-rail "
        "retry re-hits the same cap; switch rail or wait out the window",
    ),
}

#: The smallest delay that can clear the condition. One day, because both the
#: credit cycle and a daily cap turn over on a date boundary, and anything
#: shorter is optimism. A house number: no regulator specifies it.
MIN_SCHEDULED_RETRY_DELAY_SECONDS = 24 * 3600

#: G8. Not resolvable by a retry, and not for a reason the class captures.
#: ELIGIBILITY needs a different *method*, not a different *time*, and
#: ``amount_less_than_minimum_amount`` is the single per-code exception in the
#: taxonomy -- a LIMIT-class code that no schedule and no rail switch can fix.
NOT_RETRYABLE_BY_CLASS: Dict[str, Tuple[str, str]] = {
    "ELIGIBILITY": (
        "G8",
        "the customer is not eligible for this method or plan. No retry "
        "resolves that; offer an alternate method",
    ),
}

NOT_RETRYABLE_CODES: Dict[str, Tuple[str, str]] = {
    "amount_less_than_minimum_amount": (
        "G8",
        "the amount is below the method's floor. No schedule and no rail "
        "switch makes an under-minimum payment acceptable -- this is the one "
        "per-code exception in the taxonomy",
    ),
}

#: S1's first trigger. Kept as a set rather than a literal so that a second
#: already-paid code joining the taxonomy is picked up here rather than needing
#: S1 to be edited.
THREAD_TERMINATING_CODES: FrozenSet[str] = frozenset(
    code for code, entry in BY_CODE.items() if entry.reason_class == "ALREADY_PAID"
)


# -- the evaluators ---------------------------------------------------------


def check_retry_futility(reason_code: str, action: str) -> Ruling:
    """Can a retry on this code *ever* succeed? G1-G5, G8.

    Split from the timing check on purpose, and the split is an ordering
    decision rather than a stylistic one. These rules are about physics -- the
    card is expired, the merchant has the method disabled, the risk engine said
    no -- so they must be evaluated before any regulation: telling a planner that
    a ``card_expired`` retry falls outside the collection window is true and
    useless.

    G7, the timing rule, is the opposite case and therefore runs *after* the
    regulatory group. Its 24-hour minimum is a house number about credit cycles;
    R1's 24-hour minimum is a legal obligation. When both bind -- an immediate
    mandate retry on insufficient funds with no pre-debit notification -- the
    citation has to be R1, because that is the one with a circular behind it. An
    earlier revision of this file evaluated them together and cited G7, which
    reported a regulatory breach as a scheduling preference.
    """
    if action not in RETRY_ACTIONS:
        return not_applicable("G1", "not a retry")

    reason_class = reason_class_of(reason_code)

    if reason_code in NOT_RETRYABLE_CODES:
        rule_id, why = NOT_RETRYABLE_CODES[reason_code]
        return refuses(rule_id, "%s: %s" % (reason_code, why))

    if reason_class in RETRY_FORBIDDEN_CLASSES:
        rule_id, why = RETRY_FORBIDDEN_CLASSES[reason_class]
        return refuses(rule_id, "%s is %s -- %s" % (reason_code, reason_class, why))

    if reason_class in NOT_RETRYABLE_BY_CLASS:
        rule_id, why = NOT_RETRYABLE_BY_CLASS[reason_class]
        return refuses(rule_id, "%s is %s -- %s" % (reason_code, reason_class, why))

    return passes(
        "G1",
        "%s is %s -- retry-eligible under Appendix A" % (reason_code, reason_class),
    )


def check_retry_timing(reason_code: str, action: str, delay_seconds: int) -> Ruling:
    """G7 -- the retry is right, is it soon enough to be wrong?"""
    if action not in RETRY_ACTIONS:
        return not_applicable("G7", "not a retry")

    reason_class = reason_class_of(reason_code)
    if reason_class not in SCHEDULED_ONLY_CLASSES:
        return passes(
            "G7",
            "%s carries no scheduling floor" % reason_class,
            bound=False,
        )

    rule_id, why = SCHEDULED_ONLY_CLASSES[reason_class]
    if delay_seconds < MIN_SCHEDULED_RETRY_DELAY_SECONDS:
        return refuses(
            rule_id,
            "%s is %s and this retry is scheduled %ds out, inside the %ds "
            "minimum -- %s"
            % (
                reason_code,
                reason_class,
                delay_seconds,
                MIN_SCHEDULED_RETRY_DELAY_SECONDS,
                why,
            ),
        )
    return passes(
        rule_id,
        "%s is %s and the retry is deferred %ds, past the %ds minimum"
        % (reason_code, reason_class, delay_seconds, MIN_SCHEDULED_RETRY_DELAY_SECONDS),
    )


def check_retry(reason_code: str, action: str, delay_seconds: int) -> Ruling:
    """Futility then timing, as one call. Used for counting and for tests.

    ``judge`` deliberately does *not* use this: it interleaves the regulatory
    group between the two halves. Kept because "is this code retryable at this
    delay" is the question the published 45/49 counts are about.
    """
    futility = check_retry_futility(reason_code, action)
    if futility.blocks:
        return futility
    timing = check_retry_timing(reason_code, action, delay_seconds)
    if timing.blocks:
        return timing
    return futility


def check_contact(reason_code: str, action: str) -> Ruling:
    """May this action put something in front of the customer?"""
    if action not in CONTACT_ACTIONS:
        return not_applicable("G2", "not a customer contact")

    reason_class = reason_class_of(reason_code)

    if reason_class in CONTACT_FORBIDDEN_CLASSES:
        rule_id, why = CONTACT_FORBIDDEN_CLASSES[reason_class]
        return refuses(rule_id, "%s is %s -- %s" % (reason_code, reason_class, why))

    if reason_class in CONTACT_WASTE_CLASSES:
        rule_id, why = CONTACT_WASTE_CLASSES[reason_class]
        return refuses(rule_id, "%s is %s -- %s" % (reason_code, reason_class, why))

    return passes(
        "G6",
        "%s is %s -- a contact here has a plausible expected return"
        % (reason_code, reason_class),
    )


def scheduled_retry_delay(reason_code: str) -> Optional[int]:
    """The delay G7 would accept for this code, or ``None`` if G7 does not apply.

    Used by the amendment path: an immediate retry on ``insufficient_funds`` is
    not a rejection waiting to happen, it is a step with the wrong number in it.
    """
    if reason_class_of(reason_code) in SCHEDULED_ONLY_CLASSES:
        return MIN_SCHEDULED_RETRY_DELAY_SECONDS
    return None


def terminates_thread(reason_code: str) -> bool:
    return reason_code in THREAD_TERMINATING_CODES


# -- self-check -------------------------------------------------------------


def _self_check() -> None:
    """Assert the projection still covers Appendix A, at import.

    The failure this catches: a class is added to ``taxonomy.py`` -- or an
    existing one has its policy changed -- and the envelope silently keeps
    judging it under a default. The taxonomy is the authority on *what the
    classes are*; this file is the authority on *what the envelope does about
    them*, and every class must appear in at least one of the two answers.
    """
    for reason_class in REASON_CLASSES:
        policy = REASON_CLASS_POLICY[reason_class]

        # Retry: every class whose taxonomy retry_mode is never/human_only must
        # be refused by a named G rule, and no class may be both.
        forbidden = reason_class in RETRY_FORBIDDEN_CLASSES
        not_retryable = reason_class in NOT_RETRYABLE_BY_CLASS
        scheduled = reason_class in SCHEDULED_ONLY_CLASSES
        if policy.retry_mode in ("never", "human_only") and not (
            forbidden or not_retryable
        ):
            raise AssertionError(
                "taxonomy says %s retry_mode=%s, but no G rule refuses a retry "
                "for it" % (reason_class, policy.retry_mode)
            )
        if policy.retry_mode == "scheduled" and not scheduled:
            raise AssertionError(
                "taxonomy says %s needs a scheduled retry, but G7 does not "
                "cover it" % reason_class
            )
        if sum((forbidden, not_retryable, scheduled)) > 1:
            raise AssertionError(
                "%s is claimed by more than one retry guardrail; the rule_id "
                "cited would depend on evaluation order" % reason_class
            )

        # Contact: prohibited -> a G rule must refuse it; waste -> likewise.
        if policy.contact == "prohibited" and reason_class not in CONTACT_FORBIDDEN_CLASSES:
            raise AssertionError(
                "taxonomy prohibits contact for %s, but no G rule refuses it"
                % reason_class
            )
        if policy.contact == "waste" and reason_class not in CONTACT_WASTE_CLASSES:
            raise AssertionError(
                "taxonomy calls contact waste for %s, but no G rule refuses it"
                % reason_class
            )

    # The published counts, restated as guardrail coverage. If Appendix A's
    # 45-of-69 claim is quoted in the README, this is the assertion behind it.
    #
    # Note the two different numbers, because the first version of this check
    # asserted the wrong one and failed. 45 codes cannot be resolved by a retry
    # *at any delay* -- that is Appendix A's published figure and the claim the
    # README makes. **49** codes refuse an *immediate* retry, because G7 adds
    # the four FUNDS and LIMIT codes: those are retry-eligible but not right
    # now. Conflating the two would either overstate the futility claim by four
    # codes or understate the never-retry claim by four, and both are the kind
    # of off-by-a-class error a panel can find by counting.
    forever = [
        code
        for code in BY_CODE
        if check_retry(code, "ACT_RETRY", MIN_SCHEDULED_RETRY_DELAY_SECONDS).verdict
        == "REJECT"
    ]
    if len(forever) != 45:
        raise AssertionError(
            "Appendix A publishes 45 of 69 codes as not resolvable by a retry at "
            "any delay; the guardrails refuse %d" % len(forever)
        )
    immediate = [
        code for code in BY_CODE if check_retry(code, "ACT_RETRY", 0).verdict == "REJECT"
    ]
    if len(immediate) != 49:
        raise AssertionError(
            "45 codes are never retryable plus the 4 FUNDS/LIMIT codes that "
            "cannot be retried immediately = 49; the guardrails refuse %d"
            % len(immediate)
        )

    # The five classes the build instruction names by hand.
    named = {
        "INSTRUMENT_DEAD",
        "MERCHANT_CONFIG",
        "INTEGRATION_BUG",
        "ALREADY_PAID",
        "RISK",
    }
    if set(RETRY_FORBIDDEN_CLASSES) != named:
        raise AssertionError(
            "RETRY_FORBIDDEN_CLASSES must be exactly %r" % sorted(named)
        )


_self_check()
