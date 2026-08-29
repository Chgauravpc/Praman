"""Day 6, block C: the promise-to-pay state machine.

PRD 6.8's own trap case drives the extraction tests: "haan haan kal dekhta
hoon" is not a promise, "Friday tak pakka kar dunga" is, and both carry a
date reference -- what has to differ is whether a real commitment sits next
to it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pramaan.converse import promises as pr
from pramaan.llm.prompts import PromptCanonicalityError
from tests.scripted import ScriptedLLM

NOW = "2026-08-03T10:00:00+05:30"  # a Monday


# --------------------------------------------------------------------------
# Extraction -- the heuristic, against the brief's own examples
# --------------------------------------------------------------------------


def test_the_briefs_own_non_promise_is_correctly_rejected():
    result = pr.extract_commitment("haan haan kal dekhta hoon", now=NOW)
    assert result.is_promise is False
    assert result.promised_date is None


def test_the_briefs_own_promise_is_correctly_extracted():
    result = pr.extract_commitment("Friday tak pakka kar dunga", now=NOW)
    assert result.is_promise is True
    assert result.promised_date is not None
    # NOW is a Monday; the next Friday is 4 days out.
    from pramaan import canonical

    assert canonical.parse_iso(result.promised_date).date().isoformat() == "2026-08-07"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("kal pakka kar dunga", True),
        ("parso tak de dunga", True),
        ("I will definitely pay by tomorrow", True),
        ("I'll pay tomorrow", True),
        ("shayad kal kar doon", False),  # "shayad" (maybe) vetoes the commitment
        ("theek hai dekh lenge", False),
        ("pata nahi kab", False),
        ("no idea, will see", False),
        ("thanks for the reminder", False),
    ],
)
def test_a_range_of_hinglish_and_english_replies(text, expected):
    assert pr.extract_commitment(text, now=NOW).is_promise is expected


def test_an_amount_is_extracted_when_present():
    result = pr.extract_commitment("Friday tak 15000 pakka de dunga", now=NOW)
    assert result.is_promise is True
    assert result.amount_paise == 1_500_000


def test_no_amount_extracted_when_none_is_stated():
    result = pr.extract_commitment("kal pakka kar dunga", now=NOW)
    assert result.is_promise is True
    assert result.amount_paise is None


# --------------------------------------------------------------------------
# Extraction -- the LLM path, scripted
# --------------------------------------------------------------------------


def test_extract_commitment_via_llm_parses_a_scripted_reply():
    client = ScriptedLLM(
        [
            {
                "is_promise": True,
                "promised_date": "2026-08-07T23:59:59+05:30",
                "amount_paise": 1_500_000,
                "confidence": 0.8,
                "rationale": "a specific date and a real commitment verb",
            }
        ]
    )
    result = pr.extract_commitment_via_llm(client, "Friday tak pakka kar dunga", now=NOW)
    assert result.is_promise is True
    assert result.amount_paise == 1_500_000
    assert client.calls == 1


def test_the_llm_path_uses_screen_false_and_a_screened_call_would_have_refused_this_text():
    """Proves the bypass is load-bearing, not decorative: the same prompt
    through the default-screened path raises on the date and the amount."""
    from pramaan.llm.prompts import assert_no_identifiers

    prompt = pr.build_promise_extraction_prompt("Friday tak 15000 pakka kar dunga", now=NOW)
    with pytest.raises(PromptCanonicalityError):
        assert_no_identifiers(prompt)

    # And the real client.call() with the default screen=True must refuse it
    # too -- not just the standalone screen function.
    from pramaan.llm.client import LLMClient

    client = LLMClient.__new__(LLMClient)  # no config/cache needed for this check
    with pytest.raises(PromptCanonicalityError):
        LLMClient.call(client, prompt, tier="fast")


# --------------------------------------------------------------------------
# Architecture: the screen=False bypass stays confined to pramaan/converse
# --------------------------------------------------------------------------


def test_screen_false_is_used_only_under_pramaan_converse():
    """ADR-038: the one named exception to the canonicality screen. A future
    caller under plan/ or investigate/ passing screen=False would silently
    reopen the exact hole PRD 9.1 warns about, so this is a repo-wide grep,
    not a unit test of one file."""
    repo_root = Path(__file__).resolve().parent.parent
    # The definition site is expected to *mention* the flag in its own
    # docstring/comments explaining the exception; what must not exist is a
    # second *caller* passing it outside pramaan/converse.
    definition_site = repo_root / "pramaan" / "llm" / "client.py"
    offenders = []
    for path in (repo_root / "pramaan").rglob("*.py"):
        if "converse" in path.parts or path == definition_site:
            continue
        text = path.read_text(encoding="utf-8")
        if "screen=False" in text or "screen = False" in text:
            offenders.append(str(path.relative_to(repo_root)))
    assert offenders == []


def test_pramaan_converse_actually_uses_the_bypass_it_is_allowed():
    """The other half: the exception exists to be used, not to sit unused."""
    path = Path(__file__).resolve().parent.parent / "pramaan" / "converse" / "promises.py"
    assert "screen=False" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------


def test_a_non_promise_makes_no_promise():
    extracted = pr.extract_commitment("thanks", now=NOW)
    promise = pr.make_promise("cp_1", extracted, channel="whatsapp")
    assert promise.state == pr.NONE_STATE


def test_none_to_promised():
    extracted = pr.extract_commitment("Friday tak pakka kar dunga", now=NOW)
    promise = pr.make_promise("cp_1", extracted, channel="whatsapp")
    assert promise.state == pr.PROMISED
    assert promise.counterparty_id == "cp_1"
    assert promise.promised_date is not None


def test_promised_to_kept_on_full_payment():
    promise = pr.Promise(counterparty_id="cp_1", state=pr.PROMISED, amount_paise=1_500_000,
                          promised_date="2026-08-07T23:59:59+05:30")
    resolved = pr.resolve_promise(promise, paid_amount_paise=1_500_000, now="2026-08-06T10:00:00+05:30")
    assert resolved.state == pr.KEPT


def test_overpayment_still_counts_as_kept():
    promise = pr.Promise(counterparty_id="cp_1", state=pr.PROMISED, amount_paise=1_500_000,
                          promised_date="2026-08-07T23:59:59+05:30")
    resolved = pr.resolve_promise(promise, paid_amount_paise=2_000_000, now="2026-08-06T10:00:00+05:30")
    assert resolved.state == pr.KEPT


def test_promised_to_partial_on_a_shortfall_then_a_second_shortfall_breaks_it():
    promise = pr.Promise(counterparty_id="cp_1", state=pr.PROMISED, amount_paise=1_500_000,
                          promised_date="2026-08-07T23:59:59+05:30")
    first = pr.resolve_promise(promise, paid_amount_paise=500_000, now="2026-08-06T10:00:00+05:30")
    assert first.state == pr.PARTIAL
    assert first.renegotiated is True

    # A second shortfall after the one allowed re-negotiation: broken, not a
    # second PARTIAL (PRD 6.8: "never a third chase").
    second = pr.resolve_promise(first, paid_amount_paise=200_000, now="2026-08-10T10:00:00+05:30")
    assert second.state == pr.BROKEN


def test_promised_to_broken_when_the_date_passes_with_nothing_paid():
    promise = pr.Promise(counterparty_id="cp_1", state=pr.PROMISED, amount_paise=1_500_000,
                          promised_date="2026-08-07T23:59:59+05:30")
    resolved = pr.resolve_promise(promise, paid_amount_paise=0, now="2026-08-08T00:00:01+05:30")
    assert resolved.state == pr.BROKEN


def test_promised_stays_live_before_its_date_with_nothing_paid_yet():
    promise = pr.Promise(counterparty_id="cp_1", state=pr.PROMISED, amount_paise=1_500_000,
                          promised_date="2026-08-07T23:59:59+05:30")
    resolved = pr.resolve_promise(promise, paid_amount_paise=0, now="2026-08-05T10:00:00+05:30")
    assert resolved.state == pr.PROMISED


@pytest.mark.parametrize("terminal_state", [pr.NONE_STATE, pr.KEPT, pr.BROKEN])
def test_resolving_a_terminal_state_is_a_no_op(terminal_state):
    promise = pr.Promise(counterparty_id="cp_1", state=terminal_state)
    resolved = pr.resolve_promise(promise, paid_amount_paise=999_999, now="2099-01-01T00:00:00+05:30")
    assert resolved == promise


def test_an_invalid_state_is_refused_at_construction():
    with pytest.raises(ValueError):
        pr.Promise(counterparty_id="cp_1", state="not_a_real_state")


# --------------------------------------------------------------------------
# Reliability and calibration
# --------------------------------------------------------------------------


def test_reliability_with_no_history_is_the_neutral_prior():
    stats = pr.reliability_for([], "cp_new")
    assert stats.total_resolved == 0
    assert stats.reliability == pytest.approx(0.5)


def test_reliability_moves_toward_evidence():
    history = [
        pr.Promise(counterparty_id="cp_1", state=pr.KEPT),
        pr.Promise(counterparty_id="cp_1", state=pr.KEPT),
        pr.Promise(counterparty_id="cp_1", state=pr.KEPT),
        pr.Promise(counterparty_id="cp_1", state=pr.KEPT),
        pr.Promise(counterparty_id="cp_1", state=pr.KEPT),
        pr.Promise(counterparty_id="cp_1", state=pr.BROKEN),
    ]
    reliable = pr.reliability_for(history, "cp_1")
    assert reliable.reliability > 0.6

    unreliable_history = [pr.Promise(counterparty_id="cp_2", state=pr.BROKEN)] * 5
    unreliable = pr.reliability_for(unreliable_history, "cp_2")
    assert unreliable.reliability < reliable.reliability


def test_reliability_only_counts_the_named_counterparty():
    history = [
        pr.Promise(counterparty_id="cp_1", state=pr.KEPT),
        pr.Promise(counterparty_id="cp_2", state=pr.BROKEN),
    ]
    stats = pr.reliability_for(history, "cp_1")
    assert stats.kept == 1
    assert stats.broken == 0


def test_brier_score_is_zero_for_perfect_calibration():
    assert pr.brier_score([(1.0, True), (0.0, False)]) == pytest.approx(0.0)


def test_brier_score_penalises_confident_wrongness():
    confident_wrong = pr.brier_score([(0.95, False)])
    unsure_wrong = pr.brier_score([(0.55, False)])
    assert confident_wrong > unsure_wrong


def test_brier_score_of_no_predictions_is_zero_not_an_error():
    assert pr.brier_score([]) == 0.0


def test_reliability_curve_buckets_and_reports_empty_bins_as_none():
    predictions = [(0.05, True), (0.05, False), (0.95, True)]
    curve = pr.reliability_curve(predictions, bins=5)
    assert len(curve) == 5
    low_bucket = curve[0]
    assert low_bucket[1] == pytest.approx(0.5)  # one True, one False
    assert low_bucket[2] == 2
    middle_bucket = curve[2]
    assert middle_bucket[1] is None  # nothing landed here
    assert middle_bucket[2] == 0
