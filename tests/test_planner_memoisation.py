"""Day 5, block A: the planner, and the memoisation invariant it is built on.

BUILD-PLAN 1.7 rule 2 and PRD 9.1 both say the same thing from different
directions: signature memoisation is not an optimisation layered on later, it
is what makes this day possible on a free tier at all. So the tests here are
not "does the cache help" -- they are "is the cache actually keyed on the
seven frozen fields and nothing else", because a memoisation layer that is
almost right (keyed on something that happens to correlate with the signature
today) is worse than none: it looks correct until the correlation breaks.
"""
from __future__ import annotations

import pytest

from pramaan import canonical
from pramaan.plan.planner import (
    DEFAULT_STOP_CONDITIONS,
    Planner,
    PlannerStats,
    default_plan_for,
)
from pramaan.schemas import RecoveryPlan
from tests.scripted import ScriptedLLM

FEATURES = {
    "reason_class": "INSTRUMENT_DEAD",
    "diagnosis_class": "undiagnosed",
    "amount_band": 2,
    "segment": "metro",
    "legal_context": "service",
    "channel_eligibility": "full",
    "hour_bucket": "business",
}


def _reply(action="ACT_MESSAGE", channel="sms", delay=60):
    return {
        "steps": [
            {
                "step_index": 0,
                "action": action,
                "channel": channel,
                "delay_seconds": delay,
                "expected_value_paise": 1000,
                "cost_paise": 15,
                "stop_conditions": ["paid"],
                "rationale": "recollect the instrument",
            }
        ],
        "expected_value_paise": 1000,
        "rationale": "top-level rationale",
    }


# --------------------------------------------------------------------------
# Signature memoisation -- the mandatory part
# --------------------------------------------------------------------------


def test_two_calls_with_the_same_signature_build_the_plan_once():
    """The core invariant. A second call must not touch the LLM client again."""
    client = ScriptedLLM([_reply()])
    planner = Planner(client)

    first = planner.plan_for(FEATURES)
    second = planner.plan_for(FEATURES)

    assert client.calls == 1
    assert first is second  # the identical cached object, not an equal copy
    assert planner.stats.signatures_seen == 2
    assert planner.stats.cache_hits == 1
    assert planner.stats.distinct_signatures == 1
    assert planner.stats.memoisation_ratio == 2.0


def test_two_different_events_sharing_a_signature_get_the_identical_plan():
    """Different identifiers, same signature -> the memoisation actually fires.

    Mirrors ``tests/test_prompt_canonical.py``'s own invariant at the plan
    layer: the thing that must not vary with an identifier is not just the
    prompt bytes, it is the plan object built from them.
    """
    client = ScriptedLLM([_reply()])
    planner = Planner(client)

    plan_a = planner.plan_for(dict(FEATURES))
    plan_b = planner.plan_for(dict(FEATURES))  # a fresh dict, same values
    assert plan_a is plan_b
    assert client.calls == 1


def test_a_different_signature_is_a_genuine_cache_miss():
    """The negative case. Without it, a cache that ignores its key would pass
    every test above."""
    client = ScriptedLLM([_reply(), _reply(action="ACT_RETRY", channel="none")])
    planner = Planner(client)

    planner.plan_for(FEATURES)
    other = dict(FEATURES, amount_band=3)
    planner.plan_for(other)

    assert client.calls == 2
    assert planner.stats.distinct_signatures == 2


def test_newly_built_records_one_entry_per_distinct_signature_in_first_seen_order():
    """What ``pramaan.execute.runner`` reads to write one PLAN row per signature."""
    client = ScriptedLLM([_reply(), _reply(action="ACT_RETRY", channel="none")])
    planner = Planner(client)

    other = dict(FEATURES, amount_band=3)
    planner.plan_for(FEATURES, situation_ts="2026-08-03T10:00:00+05:30")
    planner.plan_for(FEATURES)  # cache hit -- must not add a second entry
    planner.plan_for(other, situation_ts="2026-08-03T11:00:00+05:30")

    sigs = [s for s, _plan, _ts in planner.newly_built]
    assert sigs == [
        canonical.planner_signature(FEATURES),
        canonical.planner_signature(other),
    ]
    assert planner.newly_built[0][2] == "2026-08-03T10:00:00+05:30"


# --------------------------------------------------------------------------
# NFR-2: a cache miss must never block the first action
# --------------------------------------------------------------------------


def test_a_true_cache_miss_falls_back_to_the_deterministic_default():
    """The real LLMClient, offline, with nothing cached: NFR-2's own case."""
    from pramaan.config import Config
    from pramaan.llm.client import LLMClient

    config = Config(seed=42, mode="shadow", llm_offline=True)
    planner = Planner(LLMClient(config))

    plan = planner.plan_for(FEATURES)
    assert plan.steps[0].action == "ACT_MESSAGE"  # taxonomy default for INSTRUMENT_DEAD
    assert plan.llm_call_ids == []
    assert planner.stats.fallback_built == 1
    assert planner.stats.llm_built == 0


def test_default_plan_for_matches_arm_bs_own_policy():
    """The fallback must be arm B's table, not a second, drifting copy of it.

    If this and arm B's ``DEFAULT_ACTION_BY_CLASS`` lookup ever disagreed, the
    NFR-2 fallback would silently become a third, untested policy -- exactly
    the anti-pattern PRD 9.1 warns a builder into without a test to catch it.
    """
    from pramaan import taxonomy

    for reason_class in taxonomy.REASON_CLASSES:
        features = dict(FEATURES, reason_class=reason_class)
        plan = default_plan_for(features)
        assert plan.steps[0].action == taxonomy.DEFAULT_ACTION_BY_CLASS[reason_class]


def test_an_unreadable_reply_also_falls_back_rather_than_raising():
    """The reply parses as JSON but has no usable steps -- degrade, do not crash.

    Distinct from a true cache miss (``CacheMiss``): the network call
    happened, and this checks the fallback still fires and the wasted call's
    id is preserved for a reviewer to find in the committed cache.
    """
    client = ScriptedLLM(["not json with no braces at all"])
    planner = Planner(client)

    plan = planner.plan_for(FEATURES)
    assert plan.steps[0].action == "ACT_MESSAGE"
    assert planner.stats.parse_failures == 1
    assert planner.stats.llm_built == 0


def test_a_reply_with_zero_valid_steps_falls_back():
    client = ScriptedLLM([{"steps": [{"action": "NOT_A_REAL_ACTION"}]}])
    planner = Planner(client)
    plan = planner.plan_for(FEATURES)
    assert plan.steps[0].action == "ACT_MESSAGE"
    assert planner.stats.parse_failures == 1


# --------------------------------------------------------------------------
# Parsing a well-formed reply
# --------------------------------------------------------------------------


def test_a_well_formed_reply_produces_a_plan_carrying_the_call_id():
    client = ScriptedLLM([_reply(action="ACT_MESSAGE", channel="sms", delay=120)])
    planner = Planner(client)
    plan = planner.plan_for(FEATURES)

    assert isinstance(plan, RecoveryPlan)
    assert plan.signature == canonical.planner_signature(FEATURES)
    assert len(plan.llm_call_ids) == 1
    step = plan.steps[0]
    assert (step.action, step.channel, step.delay_seconds) == ("ACT_MESSAGE", "sms", 120)
    assert planner.stats.llm_built == 1


def test_a_partially_malformed_step_list_keeps_the_valid_steps():
    """One bad step must not sink a plan whose other steps are fine."""
    reply = {
        "steps": [
            {"action": "NOT_A_REAL_ACTION"},
            {
                "step_index": 1,
                "action": "ACT_WAIT",
                "delay_seconds": 300,
                "stop_conditions": list(DEFAULT_STOP_CONDITIONS),
                "rationale": "let them retry",
            },
        ],
    }
    client = ScriptedLLM([reply])
    planner = Planner(client)
    plan = planner.plan_for(FEATURES)

    assert len(plan.steps) == 1
    assert plan.steps[0].action == "ACT_WAIT"
    assert plan.steps[0].step_index == 0  # reindexed, not the model's own "1"
    assert planner.stats.llm_built == 1


def test_stats_as_dict_is_json_shaped():
    stats = PlannerStats(signatures_seen=5, cache_hits=2, llm_built=2, fallback_built=1)
    out = stats.as_dict()
    assert out["distinct_signatures"] == 3
    # Rounded to one decimal for display, matching every other printed ratio
    # in this project (the LLM cache's own memoisation_ratio does the same).
    assert out["memoisation_ratio"] == pytest.approx(5 / 3, abs=0.05)
