"""The envelope's test matrix. Invariant I3, plus the boundaries that matter.

I3: **the envelope returns a verdict and a rule ID for every action x context.**
Not "for every case we thought of" -- the matrix is enumerated from the
vocabularies themselves (``canonical.ACTIONS`` x ``LEGAL_CONTEXTS`` x
``CHANNELS`` x representative hours x every reason class), so adding an action or
a channel without judging it becomes a test failure rather than a silent hole.

Four groups of tests here, and the order is deliberate:

1. **Total coverage.** Every combination judges, with a rule id, and no
   exceptions escape. This is I3.
2. **The boundaries.** 19:00:00 versus 19:00:01, 08:00:00 versus 07:59:59. These
   are the assertions the whole ``windows.py`` interval convention exists for.
3. **The definition of done**, stated as five tests that read like the
   requirement they came from.
4. **The two tables verifying each other**, and the one place the coarse
   signature feature is knowingly more permissive than the gate.

``tests/test_redteam_envelope.py`` is the adversarial half: one engineered
violation per rule R1-R11.
"""
from __future__ import annotations

import itertools

import pytest

from pramaan import canonical, taxonomy
from pramaan.envelope import (
    ALLOW,
    AMEND,
    REJECT,
    VERDICTS,
    EnvelopeContext,
    Step,
    judge,
    judge_plan,
)
from pramaan.envelope import reason_map, rules, stopping, tiers, windows

# Representative instants. Not "morning/evening": every one of these is a
# regulatory edge or one second off one.
REPRESENTATIVE_HOURS = (
    "2026-08-03T00:30:00+05:30",  # night -- everything shut
    "2026-08-03T07:59:59+05:30",  # one second before R9 opens
    "2026-08-03T08:00:00+05:30",  # R9 opens; R5/R8 still shut
    "2026-08-03T08:30:00+05:30",  # the state three hour_buckets cannot hold
    "2026-08-03T09:00:00+05:30",  # R5/R8 open -- everything open
    "2026-08-03T12:00:00+05:30",  # the uncontroversial middle of the day
    "2026-08-03T18:55:00+05:30",  # five minutes before R9 shuts
    "2026-08-03T19:00:00+05:30",  # R9's last permitted second
    "2026-08-03T19:00:01+05:30",  # the first prohibited second
    "2026-08-03T20:00:00+05:30",  # the failure peak; collection already shut
    "2026-08-03T21:00:00+05:30",  # R5/R8's last permitted second
    "2026-08-03T22:00:00+05:30",  # silent recovery only
)

#: One code per class, so the matrix exercises all ten rows of Appendix A.
ONE_CODE_PER_CLASS = tuple(
    taxonomy.CODES_BY_CLASS[cls][0] for cls in taxonomy.REASON_CLASSES
)


# ==========================================================================
# 1. Total coverage -- invariant I3
# ==========================================================================


def test_every_action_x_context_produces_a_verdict_and_a_rule_id():
    """I3. Enumerated from the vocabularies, so a new action cannot slip past.

    The assertion is deliberately weak on *which* verdict and absolute on
    *whether there is one*. What is being pinned is that the envelope is total:
    there is no (action, context) for which it shrugs, raises, or returns a
    verdict with no citation. A gate with an unhandled case is not a gate.
    """
    seen_verdicts = set()
    combinations = 0
    for action, legal_context, at, code in itertools.product(
        canonical.ACTIONS, canonical.LEGAL_CONTEXTS, REPRESENTATIVE_HOURS,
        ONE_CODE_PER_CLASS,
    ):
        channel = "sms" if action == "ACT_MESSAGE" else (
            "voice" if action == "ACT_VOICE" else "none"
        )
        judgement = judge(
            Step(action=action, channel=channel),
            EnvelopeContext(at=at, legal_context=legal_context, reason_code=code),
        )
        combinations += 1
        assert judgement.verdict in VERDICTS
        assert judgement.rule_id, (action, legal_context, at, code)
        assert judgement.reason
        seen_verdicts.add(judgement.verdict)

    # 10 actions x 3 contexts x 12 hours x 10 classes
    assert combinations == 10 * 3 * 12 * 10 == 3600
    # Both outcomes must actually occur, or the matrix is testing one branch.
    assert {ALLOW, REJECT} <= seen_verdicts


def test_every_channel_is_judged_in_every_legal_context():
    """The other axis: the matrix is (context x channel x hour), so sweep channels."""
    for legal_context, channel, at in itertools.product(
        canonical.LEGAL_CONTEXTS, windows.CHANNELS, REPRESENTATIVE_HOURS
    ):
        ruling = windows.check_window(legal_context, channel, at)
        assert ruling.verdict in (ALLOW, REJECT)
        assert ruling.rule_id


def test_the_matrix_has_no_holes():
    """A missing cell raises rather than defaulting to permitted."""
    for legal_context, channel in itertools.product(
        canonical.LEGAL_CONTEXTS, windows.CHANNELS
    ):
        assert windows.window_for(legal_context, channel) is not None
    with pytest.raises(KeyError):
        windows.window_for("marketing_but_spelled_wrong", "sms")


def test_every_action_has_a_reversibility_tier():
    for action in canonical.ACTIONS:
        assert tiers.tier_of(action) in tiers.TIERS
    with pytest.raises(KeyError):
        tiers.tier_of("ACT_INVENTED")


def test_a_judgement_always_produces_a_ledger_row():
    """The GATE row is what makes any of this auditable, so it is part of I3."""
    judgement = judge(
        Step("ACT_MESSAGE", "sms"),
        EnvelopeContext(at="2026-08-03T19:05:00+05:30", legal_context="collection"),
    )
    fields = judgement.ledger_fields()
    assert fields["decision"] in VERDICTS
    assert fields["rule_fired"]
    assert fields["payload"]["bound_rules"]
    # canonical_json must be able to serialise it, or the hash chain cannot.
    canonical.canonical_json(fields["payload"])


# ==========================================================================
# 2. The boundaries
# ==========================================================================


def test_the_1900_boundary_is_exact_to_the_second():
    """R9 prohibits contact "after 7:00 p.m.", so 19:00:00 is still permitted.

    The whole reason ``windows.py`` uses closed intervals at second precision.
    A half-open implementation refuses a lawful contact at exactly 19:00:00; an
    hour-rounded one permits an unlawful one at 19:59. Both are the same bug --
    a boundary asserted rather than read.
    """
    window = windows.window_for("collection", "sms")
    assert window.is_open(19 * 3600) is True, "19:00:00 is not 'after 7:00 p.m.'"
    assert window.is_open(19 * 3600 + 1) is False, "19:00:01 is"
    assert window.is_open(8 * 3600) is True, "08:00:00 is not 'before 8:00 a.m.'"
    assert window.is_open(8 * 3600 - 1) is False, "07:59:59 is"


def test_the_0800_to_0900_hour_is_a_distinct_legal_state():
    """R9 is open and R5/R8 are shut. Four states, and three buckets cannot hold it.

    This is the gap STATE.md recorded on Day 1 as a frozen-field problem. It is
    not fixed by adding a bucket -- it is fixed by the gate not using buckets.
    """
    at = "2026-08-03T08:30:00+05:30"
    # A collection message is lawful: R9 opened at 08:00.
    assert windows.check_window("collection", "sms", at).verdict == ALLOW
    # A collection call is not: R8 does not open until 09:00, and it is R8 that
    # is cited -- not R9, which is open.
    call = windows.check_window("collection", "voice", at)
    assert call.verdict == REJECT
    assert call.rule_id == "R8"
    # And a service call is equally refused, for the same reason.
    assert windows.check_window("service", "voice", at).rule_id == "R8"


def test_the_intersection_names_whichever_rule_actually_shut_the_door():
    """Collection voice is R9 n R8, and the citation must distinguish them.

    Collapsing the intersection into a single 09:00-19:00 constant would give the
    right verdict and the wrong attribution, and the attribution is what a
    compliance report is made of.
    """
    voice = windows.window_for("collection", "voice")
    assert voice.evaluate(8 * 3600 + 1800).rule_id == "R8"    # 08:30 -- R8 shut
    assert voice.evaluate(19 * 3600 + 1800).rule_id == "R9"   # 19:30 -- R9 shut
    assert voice.evaluate(12 * 3600).rule_id == "R9"          # midday -- R9 binds first


def test_the_evening_failure_peak_is_shut_to_collection_and_open_to_silence():
    """PRD 7's payoff, asserted. 20:00 is inside the failure peak.

    The peak is where the money is. Collection cannot touch it, and a silent
    retry can -- which is the entire argument for ``ACT_WAIT`` and
    ``ACT_RETRY_SCHEDULED`` being first-class rather than fallbacks.
    """
    peak = "2026-08-03T20:00:00+05:30"
    assert windows.check_window("collection", "sms", peak).verdict == REJECT
    assert windows.check_window("collection", "voice", peak).verdict == REJECT
    # Silent actions are untouched, in every context.
    for legal_context in canonical.LEGAL_CONTEXTS:
        assert windows.check_window(legal_context, "none", peak).verdict == ALLOW
    # And a service-classified message is permitted -- this is the classification
    # decision that buys access to the peak, and the one PRD 7 says to flag.
    assert windows.check_window("service", "sms", peak).verdict == ALLOW


def test_after_2100_only_silent_recovery_remains():
    late = "2026-08-03T22:00:00+05:30"
    for legal_context in canonical.LEGAL_CONTEXTS:
        for channel in ("voice",):
            assert windows.check_window(legal_context, channel, late).verdict == REJECT
    assert windows.check_window("promotional", "sms", late).verdict == REJECT
    assert windows.check_window("collection", "sms", late).verdict == REJECT
    assert windows.check_window("service", "none", late).verdict == ALLOW


def test_a_delayed_step_is_judged_where_it_lands_not_where_it_was_decided():
    """``delay_seconds`` is an offset, so a plan can queue itself into a shut window.

    PRD 7: receivables work must be front-loaded into business hours and never
    queued into the evening. A step decided at 18:00 with a 70-minute delay is a
    19:10 contact, and judging it at 18:00 would approve it.
    """
    context = EnvelopeContext(
        at="2026-08-03T18:00:00+05:30",
        legal_context="collection",
        reason_code="insufficient_funds",
        dlt_template_id="1207x",
        self_identification_scripted=True,
    )
    now = judge(Step("ACT_MESSAGE", "sms"), context)
    assert now.verdict == ALLOW

    later = judge(Step("ACT_MESSAGE", "sms", delay_seconds=70 * 60), context)
    assert later.verdict == REJECT
    assert later.rule_id == "R9"


def test_a_rejected_contact_reports_when_its_window_reopens():
    """Advisory, never a permission. The queued step is judged again when it fires."""
    judgement = judge(
        Step("ACT_MESSAGE", "sms"),
        EnvelopeContext(
            at="2026-08-03T19:30:00+05:30",
            legal_context="collection",
            reason_code="insufficient_funds",
            dlt_template_id="1207x",
            self_identification_scripted=True,
        ),
    )
    assert judgement.verdict == REJECT
    # 19:30 -> 08:00 the next morning is 12.5 hours.
    assert judgement.reopens_in_seconds == int(12.5 * 3600)


# ==========================================================================
# 3. The definition of done, one test per line
# ==========================================================================


def test_dod_judge_returns_a_three_valued_verdict_with_a_rule_id():
    """All three verdicts must be reachable, or the envelope is a boolean."""
    allowed = judge(Step("ACT_WAIT"), EnvelopeContext())
    assert allowed.verdict == ALLOW and allowed.rule_id

    rejected = judge(Step("ACT_RETRY"), EnvelopeContext(reason_code="card_expired"))
    assert rejected.verdict == REJECT and rejected.rule_id

    amended = judge(Step("ACT_RETRY"), EnvelopeContext(reason_code="insufficient_funds"))
    assert amended.verdict == AMEND and amended.rule_id
    assert amended.amendment == {"delay_seconds": 24 * 3600}


def test_dod_mandate_retry_without_a_t24h_notification_is_rejected_citing_r1():
    judgement = judge(
        Step("ACT_RETRY", delay_seconds=24 * 3600),
        EnvelopeContext(source_type="mandate", reason_code="insufficient_funds"),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R1"
    assert "pre-debit notification" in judgement.reason


def test_r1_measures_the_gap_to_the_debit_not_to_the_decision():
    """A notification an hour old is fine for tomorrow's retry and not for now.

    This is the assertion behind "the retry scheduler and the notification
    scheduler are one component". If R1 were evaluated against decision time,
    both of these would come out the same.
    """
    base = dict(
        at="2026-08-03T10:00:00+05:30",
        source_type="mandate",
        reason_code="insufficient_funds",
        pre_debit_notified_at="2026-08-03T09:00:00+05:30",
    )
    immediate = judge(Step("ACT_RETRY", delay_seconds=24 * 3600), EnvelopeContext(**base))
    assert immediate.verdict == ALLOW and immediate.rule_id == "R1"

    too_soon = judge(
        Step("ACT_RETRY", delay_seconds=22 * 3600), EnvelopeContext(**base)
    )
    assert too_soon.verdict == REJECT and too_soon.rule_id == "R1"


def test_dod_collection_contact_at_1905_is_rejected_citing_r9_and_at_1855_allowed():
    """The headline pair. Same step, same customer, ten minutes apart."""
    base = dict(
        legal_context="collection",
        reason_code="insufficient_funds",
        dlt_template_id="1207x",
        self_identification_scripted=True,
    )
    late = judge(
        Step("ACT_MESSAGE", "sms"),
        EnvelopeContext(at="2026-08-03T19:05:00+05:30", **base),
    )
    assert late.verdict == REJECT
    assert late.rule_id == "R9"
    assert "8:00 a.m. and after 7:00 p.m." in late.reason

    early = judge(
        Step("ACT_MESSAGE", "sms"),
        EnvelopeContext(at="2026-08-03T18:55:00+05:30", **base),
    )
    assert early.verdict == ALLOW
    # The allow cites R9 too. A permitted action whose citation is "nothing
    # stopped me" is not auditable; this one says which rule was checked.
    assert early.rule_id == "R9"


def test_dod_a_retry_on_card_expired_is_rejected_as_structurally_futile():
    judgement = judge(Step("ACT_RETRY"), EnvelopeContext(reason_code="card_expired"))
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "G1"
    assert "cannot succeed" in judgement.reason
    # And no delay rescues it. This is futility, not timing.
    for delay in (0, 3600, 86_400, 30 * 86_400):
        assert (
            judge(
                Step("ACT_RETRY", delay_seconds=delay),
                EnvelopeContext(reason_code="card_expired"),
            ).verdict
            == REJECT
        )


def test_dod_order_already_paid_terminates_the_whole_thread_via_s1():
    plan = [
        Step("ACT_RETRY"),
        Step("ACT_MESSAGE", "sms"),
        Step("ACT_VOICE", "voice"),
    ]
    judgements = judge_plan(
        plan, EnvelopeContext(reason_code="order_already_paid")
    )
    # One judgement, not three: the rest are moot, not rejected.
    assert len(judgements) == 1
    assert judgements[0].verdict == REJECT
    assert judgements[0].rule_id == "S1"
    assert judgements[0].terminates_thread is True


def test_s1_also_fires_on_a_smart_collect_virtual_account_credit():
    """The other road the money arrives by, and the one a subscriptions-only
    design misses: a B2B invoice paid by NEFT while the ladder is still running.
    """
    judgement = judge(
        Step("ACT_MESSAGE", "sms"),
        EnvelopeContext(
            reason_code="insufficient_funds", virtual_account_credited=True
        ),
    )
    assert judgement.rule_id == "S1"
    assert judgement.terminates_thread is True


# ==========================================================================
# 4. The tables verifying each other, and the deliberate disagreement
# ==========================================================================


def test_the_hourly_grid_agrees_with_the_window_bounds():
    """Two hand-written representations of the same regulation, cross-checked.

    ``windows._self_check`` already asserts this at import. It is asserted again
    here because an import-time check that nobody runs deliberately is a check
    that can be disabled by accident.
    """
    for cell, row in windows.HOURLY_GRID.items():
        assert len(row) == 24, cell
        window = windows.LEGAL_WINDOWS[cell]
        for hour, mark in enumerate(row):
            whole_hour_open = window.is_open(hour * 3600) and window.is_open(
                hour * 3600 + 3599
            )
            assert (mark == "#") == whole_hour_open, (cell, hour, mark)


def test_the_grid_reads_the_way_the_regulation_reads():
    """Spot-check the picture, so a wholesale mistranscription is visible."""
    assert windows.HOURLY_GRID[("collection", "sms")].count("#") == 11   # 08-19
    assert windows.HOURLY_GRID[("promotional", "sms")].count("#") == 12  # 09-21
    assert windows.HOURLY_GRID[("collection", "voice")].count("#") == 10  # 09-19
    assert windows.HOURLY_GRID[("service", "sms")].count("#") == 24       # no band
    for legal_context in canonical.LEGAL_CONTEXTS:
        assert windows.HOURLY_GRID[(legal_context, "none")].count("#") == 24


def test_the_coarse_signature_feature_is_knowingly_more_permissive_than_the_gate():
    """The 08:30 case: the cache key says ``full``, the envelope refuses the call.

    Pinned as a test rather than fixed, because the two are answering different
    questions. ``channel_eligibility`` is one of the seven frozen signature
    fields (F7) and feeds a cache key; the envelope is the gate. The
    disagreement is safe in exactly one direction, and that direction is
    asserted here: the *feature* may be permissive, the *gate* may not.
    """
    at = "2026-08-03T08:30:00+05:30"
    bucket = canonical.hour_bucket(at)
    assert bucket == "business"  # three buckets cannot hold the fourth state

    feature = canonical.channel_eligibility("FUNDS", "service", bucket)
    assert feature == "full", "the coarse feature offers voice"

    gate = judge(
        Step("ACT_VOICE", "voice"),
        EnvelopeContext(
            at=at,
            reason_code="insufficient_funds",
            consent="explicit",
            ai_disclosure_scripted=True,
            amount_paise=250_000,
        ),
    )
    assert gate.verdict in (AMEND, REJECT), "the gate does not"
    assert gate.rule_id == "R8"


def test_canonical_delegates_to_the_envelope_without_changing_any_value():
    """The handover was value-preserving, and that is checked rather than claimed.

    If it were not, every cached plan would be invalidated -- which costs nothing
    today at zero cached calls and would cost the token budget on Day 6.
    """
    for reason_class, legal_context, bucket in itertools.product(
        taxonomy.REASON_CLASSES, canonical.LEGAL_CONTEXTS, canonical.HOUR_BUCKETS
    ):
        assert canonical.channel_eligibility(
            reason_class, legal_context, bucket
        ) == windows.channel_eligibility_for_bucket(
            reason_class, legal_context, bucket
        )
        assert (
            canonical.channel_eligibility(reason_class, legal_context, bucket)
            in canonical.CHANNEL_ELIGIBILITY
        )


# ==========================================================================
# The reason-code guardrail, and the tiers
# ==========================================================================


def test_the_five_classes_where_a_retry_must_never_be_proposable():
    """The list the build instruction names by hand, checked code by code."""
    for reason_class in (
        "INSTRUMENT_DEAD",
        "MERCHANT_CONFIG",
        "INTEGRATION_BUG",
        "ALREADY_PAID",
        "RISK",
    ):
        for code in taxonomy.CODES_BY_CLASS[reason_class]:
            for delay in (0, 86_400, 90 * 86_400):
                judgement = judge(
                    Step("ACT_RETRY", delay_seconds=delay),
                    EnvelopeContext(reason_code=code),
                )
                assert judgement.verdict == REJECT, (code, delay)
                assert judgement.rule_id in ("G1", "G2", "G3", "G4", "G5", "S1")


def test_forty_five_of_sixty_nine_codes_cannot_be_resolved_by_a_retry():
    """Appendix A's published figure, as the envelope actually enforces it.

    And the second number, because they are different and the difference is the
    subtle part: 45 codes are futile at *any* delay, 49 refuse an *immediate*
    retry -- the extra four being FUNDS and LIMIT, which are retryable but not
    yet.
    """
    forever = [
        code
        for code in taxonomy.BY_CODE
        if reason_map.check_retry(code, "ACT_RETRY", 86_400).verdict == REJECT
    ]
    immediate = [
        code
        for code in taxonomy.BY_CODE
        if reason_map.check_retry(code, "ACT_RETRY", 0).verdict == REJECT
    ]
    assert len(taxonomy.BY_CODE) == 69
    assert len(forever) == 45
    assert len(immediate) == 49


def test_contact_is_refused_where_it_is_prohibited_and_where_it_is_waste():
    """Two different refusals, and conflating them would lose real information."""
    prohibited = judge(
        Step("ACT_MESSAGE", "sms"), EnvelopeContext(reason_code="bank_not_enabled")
    )
    assert prohibited.verdict == REJECT and prohibited.rule_id == "G2"
    assert "merchant" in prohibited.reason

    waste = judge(
        Step("ACT_MESSAGE", "sms"),
        EnvelopeContext(reason_code="bank_technical_error"),
    )
    assert waste.verdict == REJECT and waste.rule_id == "G6"
    assert "no expected return" in waste.reason


def test_an_unknown_reason_code_fails_closed():
    """Razorpay adds reason codes. An unmapped one must permit nothing.

    ``taxonomy.reason_class_of`` defaults to RISK, the class that permits
    nothing, so a new decline reason is escalated rather than retried. The
    alternative -- defaulting to retryable -- would auto-retry a code nobody has
    read yet.
    """
    for action, channel in (("ACT_RETRY", "none"), ("ACT_MESSAGE", "sms"), ("ACT_VOICE", "voice")):
        judgement = judge(
            Step(action, channel),
            EnvelopeContext(reason_code="some_reason_invented_next_quarter"),
        )
        assert judgement.verdict == REJECT
        assert judgement.rule_id == "G5"


def test_the_reversibility_asymmetry_is_structural():
    """A retry is refundable; a message is not; a call really is not.

    Most dunning systems gate retries hard and messaging loosely. These
    assertions are what stops a later "simplification" from doing that here.
    """
    assert tiers.TIER_SPEC[tiers.tier_of("ACT_RETRY")].reversible is True
    assert tiers.TIER_SPEC[tiers.tier_of("ACT_MESSAGE")].reversible is False
    assert tiers.tier_of("ACT_VOICE") > tiers.tier_of("ACT_MESSAGE")
    # A silent retry is not bound by the legal window -- which is what makes the
    # evening failure peak recoverable at all.
    assert tiers.needs_legal_window("ACT_RETRY") is False
    assert tiers.needs_legal_window("ACT_MESSAGE") is True


def test_a_voice_call_below_the_value_floor_is_amended_to_a_message():
    """P2 -- a house threshold, cited under a house id.

    And the amendment materialises only because the amended step passes on its
    own: it needs a DLT template, and without one the envelope refuses rather
    than proposing something it would not have allowed.
    """
    base = dict(
        at="2026-08-03T12:00:00+05:30",
        reason_code="insufficient_funds",
        amount_paise=10_000,  # Rs 100 -- band 1, below the Rs 500 floor
        consent="explicit",
        ai_disclosure_scripted=True,
    )
    with_template = judge(
        Step("ACT_VOICE", "voice"), EnvelopeContext(dlt_template_id="1207x", **base)
    )
    assert with_template.verdict == AMEND
    assert with_template.rule_id == "P2"
    assert with_template.amendment == {"action": "ACT_MESSAGE", "channel": "sms"}

    without_template = judge(Step("ACT_VOICE", "voice"), EnvelopeContext(**base))
    assert without_template.verdict == REJECT
    assert without_template.rule_id == "P2"


def test_the_envelope_never_amends_into_something_it_would_refuse():
    """The property that stops AMEND from being a hole in the gate.

    Every amendment offered anywhere in the matrix is re-judged, and must come
    back ALLOW. Asserted by sweeping the matrix and re-running each amendment.
    """
    checked = 0
    for action, code, at, legal_context in itertools.product(
        canonical.ACTIONS, ONE_CODE_PER_CLASS, REPRESENTATIVE_HOURS,
        canonical.LEGAL_CONTEXTS,
    ):
        channel = "sms" if action == "ACT_MESSAGE" else (
            "voice" if action == "ACT_VOICE" else "none"
        )
        context = EnvelopeContext(
            at=at,
            legal_context=legal_context,
            reason_code=code,
            consent="explicit",
            dlt_template_id="1207x",
            ai_disclosure_scripted=True,
            self_identification_scripted=True,
        )
        judgement = judge(Step(action, channel), context)
        if judgement.verdict != AMEND:
            continue
        amended = Step(action, channel)
        for field, value in judgement.amendment.items():
            amended = type(amended)(
                **{**amended.__dict__, field: value}
            )
        assert judge(amended, context).verdict == ALLOW, (action, code, at)
        checked += 1
    assert checked > 0, "no amendment was exercised -- the sweep proves nothing"


def test_amend_is_never_collapsed_into_a_boolean():
    """The three-valued verdict is not optional (build instruction, DO NOT #2)."""
    assert set(VERDICTS) == {"ALLOW", "AMEND", "REJECT"}
    assert len(VERDICTS) == 3


# ==========================================================================
# The stopping rules, as independent predicates
# ==========================================================================


def test_all_seven_stopping_rules_fire_independently():
    """Each one, on its own, with everything else benign.

    Independence is the property under test. A stopping rule that only fires as
    a clause inside a larger condition cannot be cited in a ledger row, and
    cannot be shown to a judge as a thing that exists.
    """
    cases = [
        ("S1", Step("ACT_MESSAGE", "sms"), dict(reason_code="order_already_paid")),
        (
            "S2",
            Step("ACT_MESSAGE", "sms"),
            dict(reason_code="insufficient_funds", contacts_last_24h=2,
                 dlt_template_id="1207x"),
        ),
        (
            "S3",
            Step("ACT_MESSAGE", "sms"),
            dict(reason_code="insufficient_funds", promise_state="promised",
                 promise_due_in_days=3, dlt_template_id="1207x"),
        ),
        ("S4", Step("ACT_MESSAGE", "sms"), dict(consent="withdrawn")),
        (
            "S5",
            Step("ACT_MESSAGE", "sms"),
            dict(reason_code="insufficient_funds", merchant_complaint_rate=0.02,
                 dlt_template_id="1207x"),
        ),
        (
            "S6",
            Step("ACT_MESSAGE", "sms", expected_value_paise=100, cost_paise=500),
            dict(reason_code="insufficient_funds", dlt_template_id="1207x"),
        ),
        ("S7", Step("ACT_MESSAGE", "sms"), dict(distress_signal=True)),
    ]
    for expected_rule, step, overrides in cases:
        judgement = judge(step, EnvelopeContext(**overrides))
        assert judgement.verdict == REJECT, expected_rule
        assert judgement.rule_id == expected_rule, (
            expected_rule,
            judgement.rule_id,
            judgement.reason,
        )


def test_s1_s4_and_s7_terminate_the_thread_and_the_others_do_not():
    """A refused step may be re-proposed; a terminated thread may not.

    Losing this distinction would inflate the planner violation rate with steps
    that were never reachable, and that rate is a published metric.
    """
    assert set(stopping.TERMINAL_RULES) == {"S1", "S4", "S7"}

    terminal = judge(Step("ACT_MESSAGE", "sms"), EnvelopeContext(consent="withdrawn"))
    assert terminal.terminates_thread is True

    budget = judge(
        Step("ACT_MESSAGE", "sms"),
        EnvelopeContext(
            reason_code="insufficient_funds",
            contacts_last_24h=2,
            dlt_template_id="1207x",
        ),
    )
    assert budget.rule_id == "S2"
    assert budget.terminates_thread is False


def test_s7_permits_exactly_two_responses_and_a_silent_retry_is_not_one():
    """Standing down is not the same as going quiet.

    Continuing to debit somebody who has just disputed the charge is not "not
    contacting them" -- it is the thing they disputed.
    """
    context = EnvelopeContext(reason_code="insufficient_funds", dispute_signal=True)
    for action in ("ACT_RETRY", "ACT_MESSAGE", "ACT_VOICE", "ACT_WAIT", "ACT_ROUTE"):
        assert judge(Step(action), context).rule_id == "S7", action
    assert judge(Step("ACT_ESCALATE_HUMAN"), context).verdict == ALLOW
    assert judge(Step("ACT_STOP"), context).verdict == ALLOW


def test_s3_pauses_contact_but_not_a_silent_retry():
    """A promise pauses *contact*. A reversible, invisible retry may just succeed."""
    context = EnvelopeContext(
        reason_code="insufficient_funds",
        promise_state="promised",
        promise_due_in_days=3,
        dlt_template_id="1207x",
    )
    assert judge(Step("ACT_MESSAGE", "sms"), context).rule_id == "S3"
    assert judge(Step("ACT_RETRY", delay_seconds=86_400), context).verdict == ALLOW


def test_the_r8_call_cap_and_the_s2_budget_are_separate_ceilings():
    """R8 is the regulator's maximum; S2 is the tighter house budget.

    Deliberately not merged: a compliance report needs to distinguish "we were
    legally barred" from "we chose not to", and merging them would let one
    number stand in for both.
    """
    assert stopping.MAX_CONTACTS_PER_24H < rules.MAX_UNSOLICITED_CALLS_PER_DAY
    at_cap = judge(
        Step("ACT_VOICE", "voice", expected_value_paise=100_000, cost_paise=500),
        EnvelopeContext(
            at="2026-08-03T12:00:00+05:30",
            reason_code="insufficient_funds",
            consent="explicit",
            ai_disclosure_scripted=True,
            unsolicited_calls_today=3,
        ),
    )
    assert at_cap.verdict == REJECT
    assert at_cap.rule_id == "R8"
    assert "ceiling is 3" in at_cap.reason


# ==========================================================================
# Sources and grades
# ==========================================================================


def test_every_rule_cites_an_instrument_and_declares_its_grade():
    assert set(rules.RULE_SOURCES) == set(rules.RULE_IDS)
    for rule_id, source in rules.RULE_SOURCES.items():
        assert source.instrument, rule_id
        assert source.grade in ("A", "B"), rule_id
        if source.grade == "A":
            assert source.operative_words, (
                "%s claims [A]; an [A] grade means the primary text was read, so "
                "the operative words must be quoted" % rule_id
            )


def test_r9_is_the_one_verified_against_a_primary_instrument():
    """Day 1 recorded all eleven as [B] and named R9 the weakest. R9 was checked.

    The circular number and the operative words are asserted because they are
    what makes the citation checkable -- a rule that cites "RBI guidelines" is
    not a citation, it is a gesture.
    """
    r9 = rules.RULE_SOURCES["R9"]
    assert r9.grade == "A"
    assert "DOR.ORG.REC.65/21.04.158/2022-23" in r9.instrument
    assert "12 August 2022" in r9.instrument
    assert "before 8:00 a.m. and after 7:00 p.m." in r9.operative_words


def test_the_r2_thresholds_are_the_frozen_amount_band_boundaries():
    """F6/ADR-002: the bands were frozen *on* these thresholds.

    So "band 4" and "needs AFA unless it is an insurance premium" are the same
    statement, and the band carries compliance meaning into the planner
    signature for free.
    """
    assert rules.AFA_EXEMPT_CEILING_PAISE in canonical.AMOUNT_BAND_CEILINGS_PAISE
    assert rules.AFA_EXEMPT_CEILING_HIGH_PAISE in canonical.AMOUNT_BAND_CEILINGS_PAISE
    assert canonical.amount_band(rules.AFA_EXEMPT_CEILING_PAISE) == 3
    assert canonical.amount_band(rules.AFA_EXEMPT_CEILING_PAISE + 1) == 4
