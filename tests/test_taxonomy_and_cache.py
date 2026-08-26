"""The taxonomy counts, the amount bands, and the LLM cache plumbing.

Three things are being pinned here.

**The published counts.** PRD Appendix A states 69 / 24 / 38 / 45 as facts, the
README will quote them, and a panel may well count. So they are asserted rather
than trusted.

**The amount bands.** F6 freezes five bands whose upper edges are RBI's AFA
thresholds. The boundary behaviour is the whole reason the bands are
upper-inclusive, so it gets its own case.

**The cache key.** It is also the ``llm_call_id`` recorded in the ledger, so it
has to depend on exactly the three things PRD 9 names -- model, params, prompt --
and on nothing else.
"""
from __future__ import annotations

import pytest

from pramaan import canonical, taxonomy
from pramaan.llm.cache import CacheMiss, LLMCache, cache_key
from pramaan.llm.client import MODELS, TIERS, LLMClient
from pramaan.llm.prompts import PromptCanonicalityError
from pramaan.config import Config

# -- the taxonomy -----------------------------------------------------------


def test_the_published_counts_hold():
    codes = taxonomy.REASON_CODES
    assert len(codes) == 69
    assert sum(1 for c in codes if c.retry_eligible) == 24
    assert 69 - 24 == 45


def test_there_are_exactly_ten_classes():
    assert len(taxonomy.REASON_CLASSES) == 10
    assert set(taxonomy.CODES_BY_CLASS) == set(taxonomy.REASON_CLASSES)
    assert set(taxonomy.REASON_CLASS_POLICY) == set(taxonomy.REASON_CLASSES)


def test_no_duplicate_codes():
    codes = [c.code for c in taxonomy.REASON_CODES]
    assert len(codes) == len(set(codes))


def test_the_hard_never_retry_classes_total_38_codes():
    hard = ("INSTRUMENT_DEAD", "MERCHANT_CONFIG", "INTEGRATION_BUG", "ALREADY_PAID")
    assert sum(len(taxonomy.CODES_BY_CLASS[c]) for c in hard) == 38


def test_amount_less_than_minimum_is_the_one_per_code_exception():
    """A LIMIT-class code that no retry can fix.

    Without the per-code override the retry-eligible count would be 25, not 24,
    and PRD Appendix A's published figure would be wrong.
    """
    code = taxonomy.BY_CODE["amount_less_than_minimum_amount"]
    assert code.reason_class == "LIMIT"
    assert code.retry_eligible is False
    assert code.retry_mode == "never"

    siblings = [
        c
        for c in taxonomy.REASON_CODES
        if c.reason_class == "LIMIT" and c.code != code.code
    ]
    assert siblings and all(c.retry_eligible for c in siblings)


@pytest.mark.parametrize(
    "code",
    [
        "card_expired",
        "payment_risk_check_failed",
        "order_already_paid",
        "bank_not_enabled",
        "invalid_order_id",
    ],
)
def test_anti_pattern_a5_codes_are_never_retryable(code):
    """A5: retrying any of these is waste at best and harmful at worst."""
    assert not taxonomy.is_retry_eligible(code)


def test_risk_is_human_only_not_merely_never():
    """The distinction matters: never-retry is useless, human-only is prohibited.

    Retrying a risk decline is how a merchant gets penalised, so the envelope has
    to be able to say something stronger than "this will not work".
    """
    assert taxonomy.REASON_CLASS_POLICY["RISK"].retry_mode == "human_only"
    assert taxonomy.REASON_CLASS_POLICY["INSTRUMENT_DEAD"].retry_mode == "never"


def test_tech_transient_forbids_contact_as_waste_not_as_illegal():
    """The bank is down; the customer cannot do anything about it.

    Contact here is spend with no expected return, which is a different verdict
    from MERCHANT_CONFIG, where contacting the customer is simply the wrong
    target. Both end up in NO_CONTACT_CLASSES, and the envelope will want to cite
    different reasons.
    """
    assert taxonomy.REASON_CLASS_POLICY["TECH_TRANSIENT"].contact == "waste"
    assert taxonomy.REASON_CLASS_POLICY["MERCHANT_CONFIG"].contact == "prohibited"
    assert "TECH_TRANSIENT" in taxonomy.NO_CONTACT_CLASSES
    assert "MERCHANT_CONFIG" in taxonomy.NO_CONTACT_CLASSES
    assert "AUTH_DROPOFF" not in taxonomy.NO_CONTACT_CLASSES


def test_an_unknown_code_fails_closed():
    """Razorpay adds reason codes. An unrecognised one must permit nothing."""
    assert taxonomy.reason_class_of("some_brand_new_reason") == "RISK"
    assert not taxonomy.is_retry_eligible("some_brand_new_reason")
    assert taxonomy.contact_verdict("some_brand_new_reason") == "prohibited"


def test_every_class_has_a_deterministic_default_action():
    """Arm B is exactly this map, and NFR-2's fallback reads from it."""
    for reason_class in taxonomy.REASON_CLASSES:
        action = taxonomy.DEFAULT_ACTION_BY_CLASS[reason_class]
        assert action in canonical.ACTIONS


def test_the_default_action_never_contradicts_the_guardrail():
    """Arm B has to be a fair table, not a strawman.

    C-B is only a meaningful contrast if arm B is the strongest table the
    taxonomy supports. A default action that the envelope would immediately
    reject would make arm B look artificially bad and the LLM artificially good.
    """
    for reason_class, action in taxonomy.DEFAULT_ACTION_BY_CLASS.items():
        policy = taxonomy.REASON_CLASS_POLICY[reason_class]
        if action == "ACT_RETRY":
            assert policy.retry_mode in ("immediate", "scheduled"), reason_class
        if action in ("ACT_MESSAGE", "ACT_VOICE"):
            assert policy.contact == "allowed", reason_class


# -- amount bands -----------------------------------------------------------


def test_there_are_exactly_five_bands():
    assert canonical.AMOUNT_BANDS == (1, 2, 3, 4, 5)
    assert len(canonical.AMOUNT_BAND_CEILINGS_PAISE) == 4


@pytest.mark.parametrize(
    "paise,band",
    [
        (0, 1),
        (1, 1),
        (50_000, 1),          # Rs 500 exactly
        (50_001, 2),
        (500_000, 2),         # Rs 5,000 exactly
        (500_001, 3),
        (1_500_000, 3),       # Rs 15,000 exactly -- AFA-exempt under R2
        (1_500_001, 4),       # one paisa more -- AFA required
        (10_000_000, 4),      # Rs 1,00,000 exactly
        (10_000_001, 5),
        (99_999_999_999, 5),
    ],
)
def test_band_boundaries_are_upper_inclusive(paise, band):
    assert canonical.amount_band(paise) == band


def test_the_afa_thresholds_sit_exactly_on_band_ceilings():
    """The reason the bands carry compliance meaning for free.

    R2's exemption is "up to Rs 15,000", inclusive. If Rs 15,000.00 shared a band
    with Rs 15,000.01, the envelope would demand AFA for a payment that does not
    need it.
    """
    assert 1_500_000 in canonical.AMOUNT_BAND_CEILINGS_PAISE
    assert 10_000_000 in canonical.AMOUNT_BAND_CEILINGS_PAISE
    assert canonical.amount_band(1_500_000) in canonical.AFA_EXEMPT_BANDS
    assert canonical.amount_band(1_500_001) not in canonical.AFA_EXEMPT_BANDS


def test_a_float_amount_is_refused():
    """Money is integer paise. A float in a money field is how a ledger drifts."""
    with pytest.raises(TypeError):
        canonical.amount_band(1500.0)
    with pytest.raises(TypeError):
        canonical.amount_band(True)


def test_a_negative_amount_is_refused():
    with pytest.raises(ValueError):
        canonical.amount_band(-1)


# -- hour buckets and channel eligibility ----------------------------------


@pytest.mark.parametrize(
    "hour,bucket",
    [
        (0, "night"), (7, "night"),
        (8, "business"), (12, "business"), (18, "business"),
        (19, "evening_peak"), (20, "evening_peak"),
        (21, "night"), (23, "night"),
    ],
)
def test_hour_buckets_follow_the_regulatory_edges(hour, bucket):
    """08:00 and 21:00 bound the union of R5/R8/R9; 19:00 is R9's ceiling."""
    assert canonical.hour_bucket("2026-08-01T%02d:30:00+05:30" % hour) == bucket


def test_hour_bucketing_happens_in_ist_not_utc():
    """The same instant, expressed two ways, must bucket identically.

    22:00 IST is 16:30 UTC. Bucketing the UTC hour would call it business hours
    when in fact every contact window is closed.
    """
    assert canonical.hour_bucket("2026-08-01T22:00:00+05:30") == "night"
    assert canonical.hour_bucket("2026-08-01T16:30:00+00:00") == "night"


def test_debt_collection_closes_two_hours_before_promotional():
    """R9 shuts at 19:00 while R5/R8 run to 21:00."""
    assert canonical.channel_eligibility("FUNDS", "collection", "evening_peak") == "silent_only"
    assert canonical.channel_eligibility("FUNDS", "service", "evening_peak") == "silent_and_message"


def test_no_contact_classes_are_silent_at_every_hour():
    for reason_class in ("MERCHANT_CONFIG", "ALREADY_PAID", "RISK", "INTEGRATION_BUG"):
        for bucket in canonical.HOUR_BUCKETS:
            for context in canonical.LEGAL_CONTEXTS:
                assert (
                    canonical.channel_eligibility(reason_class, context, bucket)
                    == "silent_only"
                )


def test_night_is_silent_for_everyone():
    assert canonical.channel_eligibility("FUNDS", "service", "night") == "silent_only"


def test_a_promotional_touch_never_earns_a_phone_call():
    assert canonical.channel_eligibility("FUNDS", "promotional", "business") == "silent_and_message"


# -- canonical json --------------------------------------------------------


def test_canonical_json_is_key_order_independent():
    assert canonical.canonical_json({"a": 1, "b": 2}) == canonical.canonical_json(
        {"b": 2, "a": 1}
    )


def test_canonical_json_is_pure_ascii():
    """So the bytes do not depend on the writer's locale."""
    encoded = canonical.canonical_json({"note": "भुगतान"})
    assert encoded.encode("ascii")  # would raise if not


def test_canonical_json_refuses_nan():
    with pytest.raises(ValueError):
        canonical.canonical_json({"x": float("nan")})


def test_parse_iso_refuses_a_naive_timestamp():
    with pytest.raises(ValueError, match="no timezone offset"):
        canonical.parse_iso("2026-08-01T10:00:00")


# -- the LLM cache ---------------------------------------------------------


def test_the_cache_key_depends_on_model_params_and_prompt():
    base = cache_key("m1", {"temperature": 0.0}, "hello")
    assert base != cache_key("m2", {"temperature": 0.0}, "hello")
    assert base != cache_key("m1", {"temperature": 0.7}, "hello")
    assert base != cache_key("m1", {"temperature": 0.0}, "hello!")
    assert base == cache_key("m1", {"temperature": 0.0}, "hello")


def test_the_cache_key_is_param_order_independent():
    assert cache_key("m", {"a": 1, "b": 2}, "p") == cache_key("m", {"b": 2, "a": 1}, "p")


def test_a_cache_round_trip_preserves_the_response(tmp_path):
    cache = LLMCache(tmp_path)
    key = cache_key("m", {"temperature": 0.0}, "prompt text")
    assert cache.get(key) is None
    assert cache.stats.misses == 1

    cache.put(
        key,
        model="m",
        tier="fast",
        provider="groq",
        params={"temperature": 0.0},
        prompt="prompt text",
        response='{"steps": []}',
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )
    record = cache.get(key)
    assert record is not None
    assert record["response"] == '{"steps": []}'
    assert cache.stats.hits == 1
    assert cache.stats.tokens_saved == 120


def test_cache_files_contain_nothing_time_varying(tmp_path):
    """They are committed, so a timestamp would produce a diff on every run."""
    cache = LLMCache(tmp_path)
    key = cache_key("m", {}, "p")
    path = cache.put(
        key, model="m", tier="fast", provider="groq", params={}, prompt="p",
        response="r", usage={"total_tokens": 1},
    )
    text = path.read_text(encoding="utf-8")
    for forbidden in ("created_at", "timestamp", "latency", "elapsed"):
        assert forbidden not in text
    assert set(__import__("json").loads(text)) == {
        "key", "model", "tier", "provider", "params", "prompt", "response", "usage",
    }


def test_the_cache_stores_the_prompt_in_full(tmp_path):
    """A reviewer must be able to read what the model was actually asked."""
    cache = LLMCache(tmp_path)
    key = cache_key("m", {}, "the full prompt text")
    cache.put(key, model="m", tier="fast", provider="groq", params={},
              prompt="the full prompt text", response="r", usage={})
    assert cache.get(key)["prompt"] == "the full prompt text"


def test_peek_does_not_move_the_counters(tmp_path):
    cache = LLMCache(tmp_path)
    cache.peek(cache_key("m", {}, "absent"))
    assert cache.stats.lookups == 0


# -- the client ------------------------------------------------------------


def _offline_config() -> Config:
    return Config(seed=42, mode="shadow", llm_offline=True)


def test_a_keyless_client_is_offline():
    """NFR-4 by construction, not by remembering to set a flag."""
    client = LLMClient(Config(seed=42, mode="shadow", llm_offline=False))
    assert client.offline is True


@pytest.mark.parametrize(
    "poisoned",
    [
        "recover payment pay_JK4519lmnop",
        "the amount was Rs 2437",
        "it failed at 2026-08-01 14:32:07",
        "reach them at someone@example.com",
    ],
)
def test_the_client_screens_prompts_it_did_not_build(tmp_path, poisoned):
    """A3 must hold at the chokepoint, not only where prompts are built.

    build_planner_prompt and build_investigator_prompt screen their own output,
    but they are not the only way a string can reach a provider. Any caller
    added on Day 4 or later that assembles a prompt by hand would otherwise
    bypass the screen entirely -- and the failure is silent: the call succeeds,
    every call becomes a cache miss, and the budget goes 800K -> 15M without an
    error anywhere. Screening in ``call`` is what makes the guarantee structural
    rather than a habit, so it is pinned here.

    Note this fires *before* the cache is consulted, so it cannot be dodged by a
    warm cache either.
    """
    client = LLMClient(_offline_config(), cache_dir=tmp_path)
    with pytest.raises(PromptCanonicalityError):
        client.call(poisoned, tier="fast")


def test_the_client_still_accepts_a_properly_built_prompt(tmp_path):
    """The screen must not be so strict that the real prompts cannot pass.

    A guard nobody can satisfy gets removed, so the actual planner prompt for a
    real situation is asserted to survive it.
    """
    from pramaan.llm.prompts import build_planner_prompt

    prompt = build_planner_prompt(
        {
            "reason_class": "FUNDS",
            "diagnosis_class": "undiagnosed",
            "amount_band": 2,
            "segment": "metro",
            "legal_context": "service",
            "channel_eligibility": "full",
            "hour_bucket": "business",
        }
    )
    client = LLMClient(_offline_config(), cache_dir=tmp_path)
    # Offline with an empty cache, so a CacheMiss means it got past the screen.
    with pytest.raises(CacheMiss):
        client.call(prompt, tier="fast")


def test_an_offline_cache_miss_raises_an_actionable_error(tmp_path):
    """The message is for a reviewer who just cloned the repo."""
    client = LLMClient(_offline_config(), cache_dir=tmp_path)
    with pytest.raises(CacheMiss) as excinfo:
        client.call("a prompt with no cached answer", tier="fast")
    message = str(excinfo.value)
    assert "demo-live" in message
    assert "PRAMAAN_LLM_OFFLINE" in message


def test_an_offline_hit_is_served_from_cache(tmp_path):
    client = LLMClient(_offline_config(), cache_dir=tmp_path)
    model = MODELS["fast"][0][1]
    params = {"temperature": 0.0, "max_tokens": 1024}
    client.cache.put(
        cache_key(model, params, "p"),
        model=model, tier="fast", provider="groq", params=params,
        prompt="p", response='{"ok": true}', usage={"total_tokens": 42},
    )
    response = client.call("p", tier="fast")
    assert response.cache_hit is True
    assert response.json() == {"ok": True}
    assert client.tokens.network_calls == 0


def test_a_fallback_models_cached_answer_is_still_found(tmp_path):
    """A mid-run failover must not leave a hole in the reproducible demo.

    If the live batch failed over to OpenRouter for one call, the committed cache
    holds that entry under the OpenRouter model. Replay asks for the Groq model
    first, misses, and must then find it rather than declaring the demo broken.
    """
    client = LLMClient(_offline_config(), cache_dir=tmp_path)
    fallback_model = MODELS["fast"][1][1]
    params = {"temperature": 0.0, "max_tokens": 1024}
    client.cache.put(
        cache_key(fallback_model, params, "p"),
        model=fallback_model, tier="fast", provider="openrouter", params=params,
        prompt="p", response="from the fallback", usage={"total_tokens": 7},
    )
    response = client.call("p", tier="fast")
    assert response.cache_hit is True
    assert response.text == "from the fallback"


def test_model_ids_live_in_exactly_one_table():
    """HANDOFF 7: never scatter model IDs. Free lineups change without notice."""
    assert set(MODELS) == set(TIERS)
    for tier, candidates in MODELS.items():
        assert len(candidates) >= 2, "%s needs a failover provider" % tier
        providers = [p for p, _ in candidates]
        assert providers[0] == "groq", "Groq is primary"
        assert "openrouter" in providers


def test_an_unknown_tier_is_rejected(tmp_path):
    client = LLMClient(_offline_config(), cache_dir=tmp_path)
    with pytest.raises(ValueError, match="tier must be one of"):
        client.call("p", tier="cheapest")


def test_retry_after_is_honoured_up_to_the_cap():
    """A provider asking for ten minutes is telling us the day is over."""

    class FakeResponse:
        def __init__(self, value):
            self.headers = {"Retry-After": value}

    assert LLMClient._retry_after(FakeResponse("2"), 0) == 2.0
    assert LLMClient._retry_after(FakeResponse("600"), 0) is None
    # A malformed header falls back to exponential backoff rather than crashing.
    assert LLMClient._retry_after(FakeResponse("soon"), 2) == 4.0


def test_a_live_mode_razorpay_key_is_refused():
    """NFR-7 / F13. A live key here would be an incident, not an inconvenience."""
    with pytest.raises(RuntimeError, match="test-mode"):
        Config(
            seed=42, mode="shadow", llm_offline=True, razorpay_key_id="rzp_live_abc123"
        ).guard_test_mode()

    Config(
        seed=42, mode="shadow", llm_offline=True, razorpay_key_id="rzp_test_abc123"
    ).guard_test_mode()


def test_secrets_are_not_in_the_config_repr():
    """A config printed into a log must not leak a key."""
    text = repr(
        Config(seed=42, mode="shadow", llm_offline=True, groq_api_key="gsk_secret_value")
    )
    assert "gsk_secret_value" not in text
