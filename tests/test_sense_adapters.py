"""Day 6, block B: the four adapters, and the taxonomy extension under them.

BUILD-PLAN Day 6's own definition of done drives most of this file directly:
a receivable must route to the 08:00-19:00 collection window while a payment
message stays unrestricted, and a reconciled receivable must never be chased
(S1 fires on the virtual-account credit). Both are exercised end to end
through the real envelope, not asserted against the adapter's own output --
an adapter that merely *claims* the right ``legal_context`` proves nothing if
nothing downstream reads it.
"""
from __future__ import annotations

import pytest

from pramaan import canonical, taxonomy
from pramaan.envelope import Step, judge
from pramaan.eval.arms import arm_step, envelope_context
from pramaan.sense.adapters import checkout, mandate, receivable, subscription

AT_MORNING = "2026-08-03T08:30:00+05:30"   # inside every window
AT_EVENING = "2026-08-03T19:30:00+05:30"   # R9 has shut; R5/R8 have not


# --------------------------------------------------------------------------
# taxonomy.py -- the non-payment cause-class map
# --------------------------------------------------------------------------


def test_non_payment_cause_signals_are_disjoint_from_the_69_payment_codes():
    shadowed = set(taxonomy.NON_PAYMENT_CAUSE_CLASS) & set(taxonomy.BY_CODE)
    assert not shadowed


def test_reason_class_of_falls_back_to_the_non_payment_map():
    assert taxonomy.reason_class_of("checkout_stage_otp_entry") == "AUTH_DROPOFF"
    assert taxonomy.reason_class_of("checkout_stage_method_selection") == "ELIGIBILITY"
    assert taxonomy.reason_class_of("checkout_stage_processing") == "TECH_TRANSIENT"
    assert taxonomy.reason_class_of("subscription_pending") == "FUNDS"
    assert taxonomy.reason_class_of("mandate_debit_due") == "FUNDS"
    assert taxonomy.reason_class_of("receivable_overdue") == "FUNDS"
    assert taxonomy.reason_class_of("receivable_reconciled") == "ALREADY_PAID"


def test_an_unmapped_signal_still_fails_closed_to_risk():
    assert taxonomy.reason_class_of("no_adapter_has_ever_emitted_this") == "RISK"


# --------------------------------------------------------------------------
# Checkout -- the abandonment stage
# --------------------------------------------------------------------------


def test_checkout_stage_drives_reason_class_and_action_set():
    otp = checkout.build_event(
        checkout.CheckoutBeacon(
            order_id="order_1", counterparty_id="cp_001", segment="metro",
            amount_paise=250_000, detected_at=AT_MORNING,
            stage="checkout_stage_otp_entry", arm="B",
        )
    )
    assert otp.reason_class == "AUTH_DROPOFF"
    assert otp.legal_context == "service"
    assert otp.decay_profile == canonical.DECAY_BY_SOURCE_TYPE["checkout"]
    assert "ACT_RETRY" not in otp.available_actions
    assert "ACT_MESSAGE" in otp.available_actions

    processing = checkout.build_event(
        checkout.CheckoutBeacon(
            order_id="order_2", counterparty_id="cp_002", segment="metro",
            amount_paise=250_000, detected_at=AT_MORNING,
            stage="checkout_stage_processing", arm="B",
        )
    )
    assert processing.reason_class == "TECH_TRANSIENT"
    # Contact is waste for TECH_TRANSIENT (G6) -- the adapter must not even
    # offer it, matching the payment adapter's own behaviour for this class.
    assert "ACT_MESSAGE" not in processing.available_actions


def test_checkout_never_offers_a_retry_at_any_stage():
    for stage in checkout.STAGES:
        event = checkout.build_event(
            checkout.CheckoutBeacon(
                order_id="order_x", counterparty_id="cp_x", segment="metro",
                amount_paise=250_000, detected_at=AT_MORNING, stage=stage, arm="B",
            )
        )
        assert "ACT_RETRY" not in event.available_actions


def test_an_unknown_checkout_stage_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        checkout.build_event(
            checkout.CheckoutBeacon(
                order_id="order_bad", counterparty_id="cp_x", segment="metro",
                amount_paise=250_000, detected_at=AT_MORNING,
                stage="checkout_stage_teleportation", arm="B",
            )
        )


# --------------------------------------------------------------------------
# Subscription -- the pending window
# --------------------------------------------------------------------------


def test_subscription_pending_is_funds_and_offers_a_scheduled_retry():
    event = subscription.build_event(
        subscription.SubscriptionPendingEvent(
            subscription_id="sub_1", counterparty_id="cp_010", segment="tier2",
            instalment_amount_paise=500_000, detected_at=AT_MORNING,
            retry_attempt=1, arm="C",
        )
    )
    assert event.reason_class == "FUNDS"
    assert event.source_type == "subscription"
    assert event.decay_profile == "days"
    assert "ACT_RETRY" in event.available_actions
    assert "ACT_MESSAGE" in event.available_actions

    # A subscription retry is a mandate debit too (rules.py's own
    # MANDATE_SOURCE_TYPES already names "subscription", from Day 2 -- this
    # adapter is the thing that was anticipated), so R1's notification floor
    # outranks G7's and fires first on an un-notified retry.
    ctx = envelope_context(event)
    unnotified = judge(Step(action="ACT_RETRY", channel="none", delay_seconds=0), ctx)
    assert unnotified.verdict == "REJECT"
    assert unnotified.rule_id == "R1"

    # Past R1 (notified >=24h ago), an immediate retry is G7's own amendable
    # case -- unchanged envelope behaviour, exercised here on a subscription
    # instead of a payment to prove the adapter's reason class reaches G7.
    from dataclasses import replace

    notified = replace(ctx, pre_debit_notified_at="2026-08-02T08:00:00+05:30")
    verdict = judge(Step(action="ACT_RETRY", channel="none", delay_seconds=0), notified)
    assert verdict.verdict == "AMEND"
    assert verdict.rule_id == "G7"


# --------------------------------------------------------------------------
# Mandate -- schedule -> notify -> attempt
# --------------------------------------------------------------------------


def test_mandate_debit_due_carries_the_hard_notification_floor_decay_profile():
    event = mandate.build_event(
        mandate.MandateDebitDue(
            mandate_id="mnd_1", counterparty_id="cp_020", segment="tier3",
            debit_amount_paise=1_200_000, detected_at=AT_MORNING, arm="A",
        )
    )
    assert event.reason_class == "FUNDS"
    assert event.decay_profile == "days_hard_floor"
    assert "ACT_RETRY" in event.available_actions
    assert "ACT_MESSAGE" in event.available_actions  # the T-24h notify


def test_an_immediate_mandate_debit_with_no_notification_is_rejected_by_r1():
    """R1, unchanged since Day 2 -- exercised here on a mandate event."""
    event = mandate.build_event(
        mandate.MandateDebitDue(
            mandate_id="mnd_2", counterparty_id="cp_021", segment="metro",
            debit_amount_paise=1_200_000, detected_at=AT_MORNING, arm="A",
        )
    )
    ctx = envelope_context(event)
    assert ctx.pre_debit_notified_at is None
    verdict = judge(Step(action="ACT_RETRY", channel="none", delay_seconds=0), ctx)
    assert verdict.verdict == "REJECT"
    assert verdict.rule_id == "R1"


# --------------------------------------------------------------------------
# Receivable -- the definition-of-done items, end to end
# --------------------------------------------------------------------------


def test_receivable_is_debt_collection_not_a_service_message():
    event = receivable.build_event(
        receivable.ReceivableSignal(
            invoice_id="inv_1", counterparty_id="biz_001", segment="metro",
            invoice_amount_paise=5_000_000, detected_at=AT_MORNING,
            days_overdue=20, arm="C",
        )
    )
    assert event.legal_context == "collection"
    assert event.reason_class == "FUNDS"
    assert "ACT_RETRY" not in event.available_actions  # no instrument to retry


def test_a_receivable_message_routes_to_the_0800_1900_window_a_payment_message_does_not():
    """The Day 6 DoD line, verbatim: legal_context routes a receivable to
    08:00-19:00 and a payment retry link to unrestricted."""
    receivable_evening = receivable.build_event(
        receivable.ReceivableSignal(
            invoice_id="inv_2", counterparty_id="biz_002", segment="metro",
            invoice_amount_paise=5_000_000, detected_at=AT_EVENING,
            days_overdue=20, arm="B",
        )
    )
    ctx = envelope_context(receivable_evening)
    verdict = judge(Step(action="ACT_MESSAGE", channel="whatsapp"), ctx)
    assert verdict.verdict == "REJECT"
    assert verdict.rule_id == "R9"

    receivable_morning = receivable.build_event(
        receivable.ReceivableSignal(
            invoice_id="inv_3", counterparty_id="biz_003", segment="metro",
            invoice_amount_paise=5_000_000, detected_at=AT_MORNING,
            days_overdue=20, arm="B",
        )
    )
    ctx2 = envelope_context(receivable_morning)
    verdict2 = judge(Step(action="ACT_MESSAGE", channel="whatsapp"), ctx2)
    assert verdict2.verdict == "ALLOW"
    assert verdict2.rule_id == "R9"

    # A payment failure's own service message, same evening hour: unrestricted.
    from pramaan.sense.models import Counterparty, RiskEvent

    payment_evening = RiskEvent(
        event_id="evt_pay_1", source_type="payment", amount_at_risk_paise=250_000,
        counterparty=Counterparty(id="cp_pay", kind="payer", segment="metro"),
        detected_at=AT_EVENING, decay_profile="minutes",
        cause_signal="incorrect_pin", legal_context="service",
        available_actions=("ACT_WAIT", "ACT_MESSAGE"), arm="B",
    )
    ctx3 = envelope_context(payment_evening)
    verdict3 = judge(Step(action="ACT_MESSAGE", channel="whatsapp"), ctx3)
    assert verdict3.verdict == "ALLOW"
    assert verdict3.rule_id == "R5"


def test_a_reconciled_receivable_is_never_chased():
    """S1 fires on the virtual-account credit -- the Day 6 DoD's S1 item."""
    event = receivable.build_event(
        receivable.ReceivableSignal(
            invoice_id="inv_9", counterparty_id="biz_9", segment="metro",
            invoice_amount_paise=5_000_000, detected_at=AT_MORNING,
            days_overdue=20, arm="B", reconciled=True,
        )
    )
    assert event.cause_signal == "receivable_reconciled"
    assert event.available_actions == ("ACT_WAIT", "ACT_STOP")

    ctx = envelope_context(event)
    assert ctx.virtual_account_credited is True

    # Arm B's own default action is what actually gets proposed and judged --
    # not a hand-built Step -- so this is the real path, not a synthetic one.
    step = arm_step("B", event)
    assert step.action == "ACT_STOP"
    verdict = judge(step, ctx)
    assert verdict.verdict == "ALLOW"  # ACT_STOP is always permitted

    # And if anything ever proposed a *contact* action against this event
    # instead, S1 must still terminate the thread rather than let a window
    # or futility check answer first.
    contact_verdict = judge(Step(action="ACT_MESSAGE", channel="whatsapp"), ctx)
    assert contact_verdict.verdict == "REJECT"
    assert contact_verdict.rule_id == "S1"
    assert contact_verdict.terminates_thread is True


def test_an_unreconciled_receivable_carries_no_false_virtual_account_credit():
    event = receivable.build_event(
        receivable.ReceivableSignal(
            invoice_id="inv_10", counterparty_id="biz_10", segment="metro",
            invoice_amount_paise=5_000_000, detected_at=AT_MORNING,
            days_overdue=20, arm="B",
        )
    )
    ctx = envelope_context(event)
    assert ctx.virtual_account_credited is False


def test_days_overdue_must_be_non_negative():
    with pytest.raises(ValueError):
        receivable.build_event(
            receivable.ReceivableSignal(
                invoice_id="inv_bad", counterparty_id="biz_x", segment="metro",
                invoice_amount_paise=5_000_000, detected_at=AT_MORNING,
                days_overdue=-1, arm="B",
            )
        )
