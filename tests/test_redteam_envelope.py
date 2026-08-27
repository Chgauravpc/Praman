"""Invariant I4: one engineered violation per rule, and every one is caught.

The distinction this file exists to protect, and it is the one that makes the
number publishable:

**Organic violation rate** is how often the planner proposes something the
envelope refuses. It is a measure of the *planner*, it is reported on Day 5, and
it will not be zero.

**Injected catch rate** is how often a violation deliberately constructed to
break a specific rule is caught. It is a measure of the *envelope*, and it must
be 100%. Reporting one of these as the other is the easiest way to make a
compliance claim that sounds strong and means nothing -- "zero violations" from
a run whose planner never proposed anything risky says nothing about whether the
gate works.

So each case below is hand-built to defeat exactly one rule while leaving every
other rule satisfied. That construction is the hard part: a case that trips five
rules proves only that *something* caught it, and if the intended rule were
removed the test would still pass. Every case here asserts the **rule id**, not
merely the refusal.

Why the cases are constructed rather than sampled
-------------------------------------------------

**Fifteen** of the 69 reason codes never appear in a 6,000-event batch at the
project seed -- they are long-tail classes sampled at a fraction of a percent
(DECISIONS.md ADR-007). So the simulator cannot be relied on to exercise the
taxonomy, and a red-team suite built by filtering generated events would leave
the most dangerous branches untested. These construct their inputs directly.

Two notes on that figure, because it was first written as "thirteen" -- inherited
from Day 1 and restated without re-measurement.

It is **fifteen at seed 42**, the project seed. It is **not seed-invariant**:
measured across seeds 42/1/7/99/2026 the count is 15/12/11/11/16. So the load-
bearing claim is the floor, not the number -- *at least eleven codes go untouched
on any seed tried*, and which eleven changes. That is strictly a better argument
for constructing inputs than a fixed figure would be, since it means no single
seed can be inspected once and declared sufficient.

``test_the_simulator_cannot_cover_the_taxonomy`` below measures it rather than
asserting the constant, so this docstring cannot drift again.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

from pramaan import taxonomy
from pramaan.envelope import (
    ALLOW,
    AMEND,
    REJECT,
    EnvelopeContext,
    Step,
    judge,
)
from pramaan.envelope import reason_map, rules

MIDDAY = "2026-08-03T12:00:00+05:30"

#: A context in which nothing is wrong. Every red-team case starts here and
#: breaks exactly one thing, which is what makes the rule id assertable.
def clean(**overrides) -> EnvelopeContext:
    base = dict(
        at=MIDDAY,
        legal_context="service",
        source_type="payment",
        reason_code="insufficient_funds",   # FUNDS: retry-eligible, contact allowed
        amount_paise=250_000,               # Rs 2,500 -- band 2, clears every floor
        merchant_id="acct_redteam",
        consent="explicit",
        dlt_template_id="1207161234567890",
        ai_disclosure_scripted=True,
        self_identification_scripted=True,
    )
    base.update(overrides)
    return EnvelopeContext(**base)


def test_the_clean_baseline_is_actually_clean():
    """If the baseline were refused, every case below would prove nothing.

    A red-team suite whose control case fails is a suite that catches everything,
    including the things it should let through -- which is the same as having no
    gate at all.
    """
    for step in (
        Step("ACT_WAIT"),
        Step("ACT_ROUTE"),
        Step("ACT_RETRY", delay_seconds=24 * 3600),
        Step("ACT_MESSAGE", "sms"),
        Step("ACT_VOICE", "voice"),
    ):
        judgement = judge(step, clean())
        assert judgement.verdict == ALLOW, (step.action, judgement.rule_id, judgement.reason)


# ==========================================================================
# One engineered violation per regulatory rule R1-R11
# ==========================================================================


def test_r1_a_mandate_debit_with_no_pre_debit_notification():
    """R1: the rule that breaks the naive design.

    A retry sequencer that fires without a T-24h notification is non-compliant
    even when the retry itself is perfectly reasonable -- which is why the retry
    scheduler and the notification scheduler have to be one component.
    """
    judgement = judge(
        Step("ACT_RETRY", delay_seconds=24 * 3600),
        clean(source_type="mandate", pre_debit_notified_at=None),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R1"


def test_r1_a_notification_that_landed_less_than_24h_before_the_debit():
    """The subtler half: a notification exists and is too late.

    23h59m is the case a naive implementation passes, because it checks whether
    a notification was sent rather than when it landed relative to the debit.
    """
    judgement = judge(
        Step("ACT_RETRY"),
        clean(
            at="2026-08-04T12:00:00+05:30",
            source_type="mandate",
            pre_debit_notified_at="2026-08-03T12:01:00+05:30",  # 23h59m
        ),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R1"


def test_r2_a_first_mandate_debit_with_no_additional_factor_auth():
    judgement = judge(
        Step("ACT_RETRY"),
        clean(
            source_type="mandate",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            is_first_mandate_debit=True,
            afa_validated=False,
        ),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R2"


def test_r2_a_subsequent_debit_above_the_exemption_ceiling():
    """Rs 15,000.01 -- one paise over, and the exemption is gone.

    The boundary is upper-inclusive because RBI's exemption is "up to
    Rs 15,000". Getting this edge wrong in the other direction would demand AFA
    for a payment that does not need it, which fails closed but breaks recovery
    on the whole of band 3.
    """
    over = judge(
        Step("ACT_RETRY"),
        clean(
            source_type="mandate",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            amount_paise=1_500_001,
        ),
    )
    assert over.verdict == REJECT
    assert over.rule_id == "R2"

    exactly_at_the_ceiling = judge(
        # 24h delay so that G7's credit-cycle floor is already satisfied: this
        # case is about R2 permitting the debit, not about scheduling.
        Step("ACT_RETRY", delay_seconds=24 * 3600),
        clean(
            source_type="mandate",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            amount_paise=1_500_000,
        ),
    )
    assert exactly_at_the_ceiling.verdict == ALLOW

    # ...and the higher ceiling applies to the three named categories.
    insurance = judge(
        Step("ACT_RETRY", delay_seconds=24 * 3600),
        clean(
            source_type="mandate",
            mandate_category="insurance",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            amount_paise=1_500_001,
        ),
    )
    assert insurance.verdict == ALLOW


def test_r3_an_action_that_cannot_be_attributed_to_a_merchant():
    """R3: the acquirer answers for this, so an unattributable action is refused.

    The rule with no threshold and no time band. Note that it bites even on
    ``ACT_WAIT``: an audit trail with an unattributed row in it is an audit trail
    with a hole, and R3 is the reason the audit trail has to be complete rather
    than merely long.
    """
    for action in ("ACT_WAIT", "ACT_RETRY", "ACT_MESSAGE", "ACT_VOICE"):
        judgement = judge(Step(action, "sms" if action == "ACT_MESSAGE" else "none"),
                          clean(merchant_id=None))
        assert judgement.verdict == REJECT, action
        assert judgement.rule_id == "R3", action


def test_r4_a_debit_the_customer_has_opted_out_of():
    judgement = judge(
        Step("ACT_RETRY"),
        clean(
            source_type="mandate",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            single_debit_opt_out=True,
        ),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R4"


def test_r4_a_debit_against_a_withdrawn_mandate_with_no_afa_revalidation():
    """Withdrawal is wider than opting out of one debit, and it is recoverable.

    That asymmetry is why a withdrawn mandate is R4's business for *debits* and
    S4's for *contact*: AFA re-validation can make a further debit permissible,
    and nothing makes further contact permissible.
    """
    judgement = judge(
        Step("ACT_RETRY"),
        clean(
            source_type="mandate",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            mandate_withdrawn=True,
            afa_validated=False,
        ),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R4"

    revalidated = judge(
        Step("ACT_RETRY", delay_seconds=24 * 3600),
        clean(
            source_type="mandate",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            mandate_withdrawn=True,
            afa_validated=True,
        ),
    )
    assert revalidated.verdict == ALLOW


def test_r5_an_sms_with_no_dlt_content_template():
    """R5: an unregistered message is not filtered, it is undeliverable.

    Which is why the LLM's freedom on SMS is slot-filling only: it selects a
    registered template and fills approved slots. A model that writes SMS copy
    is a model writing text that cannot be sent.
    """
    judgement = judge(Step("ACT_MESSAGE", "sms"), clean(dlt_template_id=None))
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R5"
    assert "would not arrive" in judgement.reason


def test_r5_a_promotional_message_to_a_number_on_the_dnd_register():
    judgement = judge(
        Step("ACT_MESSAGE", "sms"),
        clean(legal_context="promotional", dnd_registered=True),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R5"


def test_r6_a_fifth_mandate_attempt():
    """One attempt plus three retries. The fifth is outside the norm."""
    judgement = judge(
        Step("ACT_RETRY"),
        clean(
            source_type="mandate",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            mandate_attempt_ordinal=5,
        ),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R6"

    fourth = judge(
        Step("ACT_RETRY", delay_seconds=24 * 3600),
        clean(
            source_type="mandate",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            mandate_attempt_ordinal=4,
        ),
    )
    assert fourth.verdict == ALLOW


def test_r7_an_exemption_claimed_where_it_does_not_apply():
    """R7 is a permission, so its violation is *claiming* it wrongly.

    And this is the more dangerous state than a missing notification, because a
    missing notification gets caught by R1 while a falsely-claimed exemption
    makes R1 stand down. An ordinary subscription debit labelled as FASTag
    replenishment would skip the notification requirement entirely.
    """
    judgement = judge(
        Step("ACT_RETRY"),
        clean(
            source_type="mandate",
            mandate_category="standard",
            pre_debit_notified_at="2026-08-01T12:00:00+05:30",
            claims_r7_exemption=True,
        ),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R7"

    # The genuine exemption works, and it does lift R1.
    genuine = judge(
        Step("ACT_RETRY", delay_seconds=24 * 3600),
        clean(
            source_type="mandate",
            mandate_category="fastag",
            pre_debit_notified_at=None,
            claims_r7_exemption=True,
        ),
    )
    assert genuine.verdict == ALLOW


def test_r8_a_fourth_unsolicited_call_in_one_day():
    judgement = judge(
        Step("ACT_VOICE", "voice"), clean(unsolicited_calls_today=3)
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R8"


def test_r8_a_call_to_a_number_on_the_dnd_register():
    judgement = judge(Step("ACT_VOICE", "voice"), clean(dnd_registered=True))
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R8"


def test_r8_a_commercial_voice_call_outside_the_0900_2100_window():
    """The window half of R8, and the case a bucket-based gate would miss.

    08:30 is inside R9's collection window and outside R8's voice window, so an
    implementation that used the three-value ``hour_bucket`` would call this
    "business hours" and permit the call.
    """
    judgement = judge(
        Step("ACT_VOICE", "voice"), clean(at="2026-08-03T08:30:00+05:30")
    )
    assert judgement.verdict in (AMEND, REJECT)
    assert judgement.rule_id == "R8"


def test_r9_a_collection_contact_outside_the_0800_1900_window():
    """[A]-graded. RBI/2022-23/108, 12 Aug 2022."""
    judgement = judge(
        Step("ACT_MESSAGE", "sms"),
        clean(at="2026-08-03T19:05:00+05:30", legal_context="collection"),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R9"


def test_r9_a_collection_contact_with_no_self_identification():
    """The conduct half, which is the half that gets skipped.

    The RBI directive requires the agent to identify themselves, state whom they
    represent and state the purpose. An agent that opens with "your payment
    failed, pay now" has breached R9 at 11:00 on a Tuesday.
    """
    judgement = judge(
        Step("ACT_MESSAGE", "sms"),
        clean(legal_context="collection", self_identification_scripted=False),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R9"
    assert "identify" in judgement.reason


def test_r10_an_ai_voice_call_with_no_automation_disclosure():
    judgement = judge(
        Step("ACT_VOICE", "voice"), clean(ai_disclosure_scripted=False)
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R10"
    assert "not a human" in judgement.reason


def test_r10_is_not_amendable_because_the_envelope_cannot_write_a_script():
    """A missing disclosure is a defect in the plan, not a number to adjust.

    Downgrading the call to a message would sidestep R10 and leave the broken
    script in place for the next call. Refusing sends it back to the planner,
    which is the only place it can be fixed.
    """
    judgement = judge(
        Step("ACT_VOICE", "voice"), clean(ai_disclosure_scripted=False)
    )
    assert judgement.verdict == REJECT
    assert judgement.amendment is None


def test_r11_promotional_contact_without_explicit_consent():
    """Implied consent carries a service message and not a promotional one.

    This single difference is what stops a recovery system from quietly becoming
    a marketing channel -- the drift R11 exists to prevent.
    """
    judgement = judge(
        Step("ACT_MESSAGE", "sms"),
        clean(legal_context="promotional", consent="implied"),
    )
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R11"

    # ...and the same message in a service context is fine on implied consent.
    service = judge(
        Step("ACT_MESSAGE", "sms"), clean(legal_context="service", consent="implied")
    )
    assert service.verdict == ALLOW


def test_r11_contact_with_no_consent_basis_at_all():
    judgement = judge(Step("ACT_MESSAGE", "sms"), clean(consent="none"))
    assert judgement.verdict == REJECT
    assert judgement.rule_id == "R11"


def test_every_rule_r1_to_r11_has_at_least_one_case_that_cites_it():
    """The completeness check: eleven rules, eleven citations, none missing.

    Written as a sweep rather than trusted to the eleven tests above, because a
    test that was renamed or deleted would leave a rule silently untested.
    """
    cases = {
        "R1": (Step("ACT_RETRY"), clean(source_type="mandate")),
        "R2": (
            Step("ACT_RETRY"),
            clean(
                source_type="mandate",
                pre_debit_notified_at="2026-08-01T12:00:00+05:30",
                is_first_mandate_debit=True,
            ),
        ),
        "R3": (Step("ACT_WAIT"), clean(merchant_id=None)),
        "R4": (
            Step("ACT_RETRY"),
            clean(
                source_type="mandate",
                pre_debit_notified_at="2026-08-01T12:00:00+05:30",
                single_debit_opt_out=True,
            ),
        ),
        "R5": (Step("ACT_MESSAGE", "sms"), clean(dlt_template_id=None)),
        "R6": (
            Step("ACT_RETRY"),
            clean(
                source_type="mandate",
                pre_debit_notified_at="2026-08-01T12:00:00+05:30",
                mandate_attempt_ordinal=5,
            ),
        ),
        "R7": (
            Step("ACT_RETRY"),
            clean(
                source_type="mandate",
                pre_debit_notified_at="2026-08-01T12:00:00+05:30",
                claims_r7_exemption=True,
            ),
        ),
        "R8": (Step("ACT_VOICE", "voice"), clean(unsolicited_calls_today=3)),
        "R9": (
            Step("ACT_MESSAGE", "sms"),
            clean(at="2026-08-03T19:05:00+05:30", legal_context="collection"),
        ),
        "R10": (Step("ACT_VOICE", "voice"), clean(ai_disclosure_scripted=False)),
        "R11": (Step("ACT_MESSAGE", "sms"), clean(consent="none")),
    }
    assert set(cases) == set(rules.RULE_IDS), "a rule has no engineered violation"
    caught = {}
    for rule_id, (step, context) in cases.items():
        judgement = judge(step, context)
        caught[rule_id] = judgement.rule_id
        assert judgement.verdict == REJECT, (rule_id, judgement.reason)
    assert caught == {rule_id: rule_id for rule_id in rules.RULE_IDS}, caught


# ==========================================================================
# The taxonomy guardrails G1-G8
# ==========================================================================


def test_every_guardrail_g1_to_g8_has_a_case_that_cites_it():
    """G4 is the one that needs explaining, and it is explained rather than skipped.

    At the ``judge`` level S1 fires first on every ALREADY_PAID code, so G4 can
    never be the decisive citation there. That is deliberate -- S1 terminates the
    thread and G4 only refuses a step -- and it makes G4 defence in depth: it is
    the rule that would still refuse a retry if S1 were ever reordered or scoped
    down. So it is tested at the unit level, and the shadowing is asserted too,
    which is the difference between dead code and a backstop.
    """
    provoked = {}
    for step, context, expected in [
        (Step("ACT_RETRY"), clean(reason_code="card_expired"), "G1"),
        (Step("ACT_RETRY"), clean(reason_code="bank_not_enabled"), "G2"),
        (Step("ACT_RETRY"), clean(reason_code="invalid_order_id"), "G3"),
        (Step("ACT_RETRY"), clean(reason_code="payment_risk_check_failed"), "G5"),
        (Step("ACT_MESSAGE", "sms"), clean(reason_code="bank_technical_error"), "G6"),
        (Step("ACT_RETRY"), clean(reason_code="insufficient_funds"), "G7"),
        (Step("ACT_RETRY"), clean(reason_code="user_not_eligible"), "G8"),
    ]:
        judgement = judge(step, context)
        assert judgement.verdict in (AMEND, REJECT), expected
        assert judgement.rule_id == expected, (expected, judgement.rule_id)
        provoked[expected] = True

    # G4, at the unit level.
    assert (
        reason_map.check_retry("order_already_paid", "ACT_RETRY", 0).rule_id == "G4"
    )
    assert (
        reason_map.check_contact("order_already_paid", "ACT_MESSAGE").rule_id == "G4"
    )
    provoked["G4"] = True
    # ...and deliberately shadowed by S1 at the judge level.
    assert judge(Step("ACT_RETRY"), clean(reason_code="order_already_paid")).rule_id == "S1"

    assert set(provoked) == {"G%d" % n for n in range(1, 9)}


def test_a_retry_is_never_proposable_in_the_five_named_classes():
    """The build instruction's DO NOT, checked over all 41 codes in those classes.

    45 of 69 codes cannot be resolved by a retry, and in RISK and ALREADY_PAID
    retrying causes real harm rather than merely wasting money. So this sweeps
    every code rather than sampling one per class.
    """
    checked = 0
    for reason_class in (
        "INSTRUMENT_DEAD",
        "MERCHANT_CONFIG",
        "INTEGRATION_BUG",
        "ALREADY_PAID",
        "RISK",
    ):
        for code in taxonomy.CODES_BY_CLASS[reason_class]:
            judgement = judge(Step("ACT_RETRY"), clean(reason_code=code))
            assert judgement.verdict == REJECT, code
            assert judgement.amendment is None, (
                "%s must not be amendable into a retry" % code
            )
            checked += 1
    assert checked == 10 + 10 + 17 + 1 + 3 == 41


# ==========================================================================
# The architectural boundary
# ==========================================================================

ENVELOPE_DIR = pathlib.Path(__file__).resolve().parent.parent / "pramaan" / "envelope"


def test_no_module_under_envelope_imports_anything_from_pramaan_llm():
    """The envelope's job is to catch the LLM, so it cannot be the LLM.

    Checked by parsing the imports rather than by grepping for a string, because
    a grep matches the word in a docstring -- and this file's docstrings talk
    about the LLM constantly. Parsing asks the question that matters: does any
    module here have a dependency edge into ``pramaan.llm``?
    """
    offenders = []
    for path in sorted(ENVELOPE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if "llm" in name.split("."):
                    offenders.append("%s:%d imports %s" % (path.name, node.lineno, name))
    assert not offenders, offenders


def test_importing_the_envelope_does_not_load_the_llm_package():
    """The dynamic half: a transitive import would not show up in the AST scan.

    Run in a subprocess so that an earlier test having imported the LLM client
    cannot mask the result -- which it would, in a single interpreter, and that
    is exactly the sort of false green a boundary test must not produce.
    """
    script = (
        "import sys; import pramaan.envelope; "
        "bad = [m for m in sys.modules if m.startswith('pramaan.llm')]; "
        "sys.exit('LEAKED: %r' % bad if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ENVELOPE_DIR.parent.parent),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_envelope_needs_no_network_no_clock_and_no_database():
    """A verdict is a pure function of (step, context).

    Asserted structurally: nothing under ``envelope/`` may import a module that
    could read a clock, open a socket, or touch the filesystem. That is what
    makes every branch reachable from a test -- including the ones that need a
    specific second of a specific hour -- and it is what makes the same inputs
    give the same verdict on a reviewer's machine.

    ``datetime`` is permitted: it is used for arithmetic on timestamps that
    arrive in the context. ``datetime.now`` is not, and is checked for by name.
    """
    forbidden_modules = {"requests", "httpx", "urllib", "socket", "sqlite3", "random", "time"}
    offenders = []
    for path in sorted(ENVELOPE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for name in names:
                    if name.split(".")[0] in forbidden_modules:
                        offenders.append("%s:%d imports %s" % (path.name, node.lineno, name))
            if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today"):
                offenders.append(
                    "%s:%d reads a wall clock (.%s)" % (path.name, node.lineno, node.attr)
                )
    assert not offenders, offenders


def test_the_same_inputs_give_the_same_verdict_every_time():
    """Determinism, stated as a property rather than assumed from the structure."""
    step = Step("ACT_MESSAGE", "sms")
    context = clean(at="2026-08-03T19:00:01+05:30", legal_context="collection")
    first = judge(step, context)
    for _ in range(50):
        again = judge(step, context)
        assert (again.verdict, again.rule_id, again.reason) == (
            first.verdict,
            first.rule_id,
            first.reason,
        )


def test_a_rejection_is_never_silent():
    """Every refusal carries a rule id and a reason a human can read.

    The property that makes this component the answer to "why did it refuse?"
    rather than a source of unexplained failures.
    """
    for step, context in [
        (Step("ACT_RETRY"), clean(reason_code="card_expired")),
        (Step("ACT_MESSAGE", "sms"), clean(dlt_template_id=None)),
        (Step("ACT_VOICE", "voice"), clean(ai_disclosure_scripted=False)),
        (Step("ACT_WAIT"), clean(merchant_id=None)),
        (Step("ACT_MESSAGE", "sms"), clean(distress_signal=True)),
    ]:
        judgement = judge(step, context)
        assert judgement.verdict == REJECT
        assert judgement.rule_id
        assert len(judgement.reason) > 20, judgement.reason
        assert judgement.bound_rules


def test_p0_never_appears_as_a_citation_in_practice():
    """``NO_RULE_BINDS`` exists so ``rule_id`` is non-null, not to be used.

    R3 binds on every action, so it is the floor citation and P0 is unreachable
    through ``judge``. Worth asserting: if P0 ever surfaced it would mean R3 had
    stopped applying to something, which is a hole in the audit trail rather
    than a cosmetic issue.
    """
    from pramaan.canonical import ACTIONS

    for action in ACTIONS:
        channel = {"ACT_MESSAGE": "sms", "ACT_VOICE": "voice"}.get(action, "none")
        judgement = judge(Step(action, channel), clean())
        assert judgement.rule_id != "P0", (action, judgement.reason)


@pytest.mark.parametrize(
    "at,expected",
    [
        ("2026-08-03T07:59:59+05:30", REJECT),
        ("2026-08-03T08:00:00+05:30", ALLOW),
        ("2026-08-03T19:00:00+05:30", ALLOW),
        ("2026-08-03T19:00:01+05:30", REJECT),
    ],
)
def test_the_r9_boundary_second_by_second(at, expected):
    """Parameterised so a failure names the exact second that broke."""
    judgement = judge(
        Step("ACT_MESSAGE", "sms"), clean(at=at, legal_context="collection")
    )
    assert judgement.verdict == expected
    assert judgement.rule_id == "R9"

def test_the_simulator_cannot_cover_the_taxonomy():
    """Measure the coverage gap rather than restating an inherited constant.

    The argument for constructing red-team inputs by hand rests on the simulator
    leaving a chunk of the taxonomy untouched. That is true, and the specific
    figure moves with the seed -- which is why this measures the **floor** across
    several seeds instead of pinning one number. The first version of this file's
    docstring carried "thirteen", inherited from Day 1 and never re-measured; at
    the project seed it is fifteen.

    Kept cheap: five seeds on the full batch. If it ever gets slow, cut seeds
    rather than the assertion, because the assertion is what stops a future
    session from concluding the simulator is adequate coverage.
    """
    from sim import generate as sim

    all_codes = set(taxonomy.BY_CODE)
    unseen_counts = {}
    for seed in (42, 1, 7, 99, 2026):
        seen = {event.cause_signal for event in sim.full_batch(seed)}
        unseen_counts[seed] = len(all_codes - seen)

    # The project seed, which is the figure the docstring quotes.
    assert unseen_counts[42] == 15, unseen_counts

    # The floor, which is the claim that actually carries the argument: no seed
    # gets close to covering the taxonomy, so no single batch can be inspected
    # and declared sufficient.
    assert min(unseen_counts.values()) >= 11, unseen_counts

    # And the codes that go missing are the dangerous ones -- long-tail classes,
    # which is precisely where the envelope's never-retry branches live.
    seen_42 = {event.cause_signal for event in sim.full_batch(42)}
    missing = all_codes - seen_42
    dangerous = {
        code
        for code in missing
        if taxonomy.reason_class_of(code)
        in ("INSTRUMENT_DEAD", "MERCHANT_CONFIG", "INTEGRATION_BUG", "RISK")
    }
    assert dangerous, "the missing codes should include never-retry classes"
