"""Invariant I2 -- two events sharing a signature produce identical prompt bytes.

PRD 9.1 calls this the only thing standing between the plan and a 20x cost
overrun, and HANDOFF 9 says a broken canonicalisation invariant is a budget
emergency rather than a bug. It is a Day 1 test on purpose: if prompts are not
canonical, that is not something to discover on Day 5 with four days of work
already sitting on top of it.

The events constructed here are maximally different in every way that must not
matter -- different ids, different payment references, different counterparties,
different exact amounts, different timestamps, one carrying simulator ground
truth and one not -- and identical in the seven fields that must.
"""
from __future__ import annotations

import pytest

from pramaan import canonical
from pramaan.llm.prompts import (
    FORBIDDEN_PATTERNS,
    PromptCanonicalityError,
    assert_no_identifiers,
    build_investigator_prompt,
    build_planner_prompt,
    render_features,
)
from pramaan.schemas import PlannerFeatures
from pramaan.sense.models import Counterparty, LatentTruth, RiskEvent
from sim import generate as sim


def _event(
    event_id: str,
    amount_paise: int,
    detected_at: str,
    counterparty_id: str,
    external_ref: str,
    latent=None,
) -> RiskEvent:
    return RiskEvent(
        event_id=event_id,
        source_type="payment",
        amount_at_risk_paise=amount_paise,
        counterparty=Counterparty(id=counterparty_id, kind="payer", segment="metro"),
        detected_at=detected_at,
        decay_profile="minutes",
        cause_signal="insufficient_funds",
        legal_context="service",
        available_actions=("ACT_WAIT", "ACT_RETRY", "ACT_MESSAGE"),
        arm="C",
        external_ref=external_ref,
        latent=latent,
    )


def test_same_signature_gives_byte_identical_prompt():
    """The invariant itself.

    Both events are FUNDS class, both land in band 2 (Rs 500.01-5,000), both
    metro, both inside business hours -- but they differ in every identifier and
    in the exact rupee amount, and one carries latent ground truth.
    """
    left = _event(
        "evt_0042_000001",
        249_900,  # Rs 2,499.00
        "2026-08-03T11:15:00+05:30",
        "cp_00001",
        "pay_aaaaaaaaaaaaaa",
    )
    right = _event(
        "evt_9999_123456",
        49_950_1 // 1,  # Rs 4,995.01 -- a different amount in the same band
        "2026-08-27T16:42:37+05:30",
        "cp_04242",
        "pay_zzzzzzzzzzzzzz",
        latent=LatentTruth(self_recovers_at="2026-08-27T18:00:00+05:30"),
    )

    assert left.signature() == right.signature()
    assert left.amount_at_risk_paise != right.amount_at_risk_paise
    assert left.event_id != right.event_id

    left_prompt = build_planner_prompt(left.canonical_features())
    right_prompt = build_planner_prompt(right.canonical_features())

    assert left_prompt == right_prompt
    # Bytes, not just equal strings: the cache key hashes encoded bytes.
    assert left_prompt.encode("utf-8") == right_prompt.encode("utf-8")


def test_latent_ground_truth_cannot_reach_a_prompt():
    """The answer key must not be readable from the prompt path.

    An event carrying simulator ground truth has to produce exactly the prompt it
    would produce without it. If this ever fails, the agent is reading the
    counterfactual and every number downstream is fiction (PRD 8.2).
    """
    base = _event(
        "evt_0042_000007",
        120_000,
        "2026-08-04T10:00:00+05:30",
        "cp_00007",
        "pay_bbbbbbbbbbbbbb",
    )
    with_truth = _event(
        "evt_0042_000007",
        120_000,
        "2026-08-04T10:00:00+05:30",
        "cp_00007",
        "pay_bbbbbbbbbbbbbb",
        latent=LatentTruth(self_recovers_at="2026-08-04T10:04:00+05:30"),
    )
    assert build_planner_prompt(base.canonical_features()) == build_planner_prompt(
        with_truth.canonical_features()
    )
    assert "self_recovers" not in build_planner_prompt(with_truth.canonical_features())


def test_amounts_in_the_same_band_collapse_but_across_bands_do_not():
    """Banding must actually band -- and must still distinguish where it matters.

    A test that only checked collapse would pass on a function returning a
    constant. The AFA boundary is the case that has to stay visible: Rs 15,000
    exactly and Rs 15,000.01 are one paisa apart and on opposite sides of R2.
    """
    def prompt_for(paise: int) -> str:
        event = _event(
            "evt_0042_000009",
            paise,
            "2026-08-04T10:00:00+05:30",
            "cp_00009",
            "pay_cccccccccccccc",
        )
        return build_planner_prompt(event.canonical_features())

    assert prompt_for(50_001) == prompt_for(499_999)      # both band 2
    assert prompt_for(1_500_000) != prompt_for(1_500_001)  # R2 boundary, one paisa
    assert canonical.amount_band(1_500_000) == 3
    assert canonical.amount_band(1_500_001) == 4


def test_no_prompt_in_a_whole_dev_batch_contains_an_identifier():
    """The screen, exercised against every situation the batch actually produces.

    Stronger than a hand-built case: it covers all ten reason classes, every
    band, every hour bucket and every eligibility state that the seeded batch
    generates, and it is the check that would have caught an identifier leaking
    in through a field's value rather than through a template edit.
    """
    events = sim.dev_batch(42)
    assert events, "dev batch must not be empty"
    prompts = set()
    for event in events:
        prompt = build_planner_prompt(event.canonical_features())
        assert_no_identifiers(prompt)
        assert event.event_id not in prompt
        assert event.external_ref not in prompt
        assert event.counterparty.id not in prompt
        assert str(event.amount_at_risk_paise) not in prompt
        assert event.detected_at not in prompt
        prompts.add(prompt)

    # And the payoff: far fewer distinct prompts than events.
    assert len(prompts) < len(events)
    assert len(prompts) == len({e.signature() for e in events})


def test_investigator_prompts_are_canonical_too():
    events = sim.dev_batch(42)[:40]
    for event in events:
        prompt = build_investigator_prompt(
            event.canonical_features(), canonical.DIAGNOSIS_CLASSES
        )
        assert_no_identifiers(prompt, context="investigator prompt")


@pytest.mark.parametrize(
    "poisoned",
    [
        "the payment pay_JK4519lmnop failed",
        "detected at 2026-08-01 in the evening",
        "the amount was Rs 2437",
        "amount_at_risk_paise 243700",
        "call id 9f2b1c4d5e6a7b8c9d0e1f2a3b4c5d6e",
        "reach them at someone@example.com",
        "their number is +91 9876543210",
        "at 14:32:07 IST",
    ],
)
def test_the_screen_catches_each_forbidden_shape(poisoned: str):
    """Every pattern in the screen has a case, so none of them can rot silently."""
    with pytest.raises(PromptCanonicalityError):
        assert_no_identifiers(poisoned)


def test_the_screen_permits_the_legitimate_prompt_vocabulary():
    """The screen must not be so strict that a real prompt cannot be written.

    A screen nobody can satisfy gets disabled, and a disabled screen protects
    nothing -- so the vocabulary the prompts actually use is pinned here.
    """
    for benign in (
        "amount_band: 3 (5k-15k)",
        "rules R1 through R11 apply",
        "the window closes at 21:00",
        "step_index 0, delay_seconds 420",
        "channel_eligibility: silent_and_message",
        "hour_bucket: evening_peak",
    ):
        assert assert_no_identifiers(benign) == benign


def test_signature_field_set_is_frozen_at_seven():
    """F7. Adding a field can multiply the signature space."""
    assert len(canonical.PLANNER_SIGNATURE_FIELDS) == 7
    assert canonical.PLANNER_SIGNATURE_FIELDS == (
        "reason_class",
        "diagnosis_class",
        "amount_band",
        "segment",
        "legal_context",
        "channel_eligibility",
        "hour_bucket",
    )


def test_schema_and_canonical_field_sets_cannot_drift():
    """PlannerFeatures is the typed face of PLANNER_SIGNATURE_FIELDS.

    Two declarations of the same thing will diverge unless something checks.
    """
    assert set(PlannerFeatures.model_fields) == set(canonical.PLANNER_SIGNATURE_FIELDS)


def test_an_eighth_feature_is_rejected():
    """The whitelist half of the defence: an identifier cannot be *added*."""
    features = {
        "reason_class": "FUNDS",
        "diagnosis_class": "undiagnosed",
        "amount_band": 2,
        "segment": "metro",
        "legal_context": "service",
        "channel_eligibility": "full",
        "hour_bucket": "business",
    }
    assert render_features(features)  # the seven alone are fine

    with pytest.raises(ValueError, match="PLANNER_SIGNATURE_FIELDS"):
        render_features(dict(features, payment_id="pay_JK4519lmnop"))
    with pytest.raises(ValueError, match="PLANNER_SIGNATURE_FIELDS"):
        render_features({k: v for k, v in features.items() if k != "segment"})


def test_out_of_domain_values_are_rejected():
    """The domain half: a field cannot go high-cardinality via its value."""
    features = {
        "reason_class": "FUNDS",
        "diagnosis_class": "undiagnosed",
        "amount_band": 2,
        "segment": "metro",
        "legal_context": "service",
        "channel_eligibility": "full",
        "hour_bucket": "business",
    }
    with pytest.raises(ValueError, match="frozen domain"):
        canonical.validate_features(dict(features, segment="mumbai-andheri-west"))
    with pytest.raises(ValueError, match="frozen domain"):
        canonical.validate_features(dict(features, amount_band=7))


def test_every_forbidden_pattern_is_named():
    """Error messages are read under time pressure; unnamed patterns are useless."""
    for label, pattern in FORBIDDEN_PATTERNS:
        assert label and not label.startswith("pattern")
        assert pattern.pattern


# --------------------------------------------------------------------------
# The screen's discrimination between an identifier and an enum value (Day 4)
# --------------------------------------------------------------------------
#
# Added when the investigator first tried to put a reason code in a prompt and
# the screen refused it. The original pattern keyed on the prefix alone, so
# `order_already_paid` -- a closed-domain Razorpay reason code -- was treated as
# an order id. The correction discriminates on identifier *shape*, and these two
# tests are what stop the correction from having quietly widened the hole.


def test_real_razorpay_shaped_identifiers_are_still_refused():
    """Every identifier shape the screen exists to catch, still caught.

    This is the test that matters after loosening a guard. Listed exhaustively
    rather than sampled, and including the all-lowercase case that the shape rule
    alone would miss and the length backstop catches.
    """
    from pramaan.llm.prompts import PromptCanonicalityError, assert_no_identifiers

    shapes = [
        "pay_29QQoUBi66xm2f",       # a real Razorpay payment id
        "order_HkjF8sd9Lm2xQq",
        "sub_JK9dEwFqPl",
        "cust_1Aa2Bb3Cc4",
        "inv_MnB4vC7xZ2",
        "txn_9d8f7a6b5c4d",
        "rzp_test_abc123XY",
        "evt_0042_000123",          # this project's own event id
        "cp_00071",                 # and its counterparty id
        "pay_abcdefghijklmn",       # all lowercase: the length backstop
    ]
    for token in shapes:
        with pytest.raises(PromptCanonicalityError):
            assert_no_identifiers("context: %s" % token)


def test_every_reason_code_in_the_taxonomy_is_promptable():
    """All 69 codes must survive the screen.

    ``get_reason_taxonomy`` exists to ground the agent in real reason-code
    semantics (PRD 6.2), and ``cause_signal`` is a column in the agent's
    projection, so any query grouping by it renders codes into prompt bytes. A
    screen that refuses even one code makes that tool unusable on the incident
    where that code matters.
    """
    from pramaan.llm.prompts import assert_no_identifiers
    from pramaan.taxonomy import BY_CODE

    for code in sorted(BY_CODE):
        assert_no_identifiers("cause_signal: %s" % code, context="taxonomy code")
    assert len(BY_CODE) == 69
