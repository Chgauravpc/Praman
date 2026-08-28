"""The agent loop: detection, bounds, determinism, and the end-to-end find.

Driven by ``tests/scripted.py`` -- a canned model, not a stubbed loop. The tool
belt, the projected database, the prompt builder and the auditor are all the
production article; only the network is absent. So these tests consume no tokens
and still exercise the code that will run against a live model.

The last section is the Day 4 definition-of-done item: the investigator, given
only a flagged window, reaches the injected degradation *and* separates the real
rate shift from the mix shift it must not act on.
"""
from __future__ import annotations

import pytest

from pramaan.investigate import agent as A
from pramaan.investigate import receipts as R
from pramaan.investigate.tools import ToolBelt, build_agent_db
from sim.generate import dev_batch_degraded, generate
from sim.incident import DEV_INCIDENT, build_downtime, build_traffic
from tests.scripted import ScriptedLLM, conclusion, tool_call


@pytest.fixture(scope="module")
def world():
    events, truth = dev_batch_degraded()
    return events, build_traffic(events, 42, DEV_INCIDENT), build_downtime(DEV_INCIDENT), truth


@pytest.fixture()
def belt(world):
    events, traffic, downtime, _t = world
    return ToolBelt(build_agent_db(events, traffic, downtime))


# --------------------------------------------------------------------------
# Detection -- arithmetic, and it must find the right window
# --------------------------------------------------------------------------


def test_the_detector_finds_exactly_the_injected_window(belt, world):
    """The window handed to the investigator must be the whole episode.

    The first version of the detector returned ``(3,)`` for a two-day incident,
    because on day 4 the trailing baseline included day 3 -- the incident's own
    first day -- which lifted the baseline and pushed the measured elevation
    under the threshold. Nothing raised: the detector returned a window, the
    investigation succeeded, and the diagnosis was about half an incident.
    """
    _events, _traffic, _downtime, truth = world
    incidents = A.detect_incidents(belt)
    assert len(incidents) == 1, [i.window for i in incidents]
    assert incidents[0].window == DEV_INCIDENT.days == (truth.start_day, truth.end_day)
    assert incidents[0].baseline == (0, 1, 2)
    assert incidents[0].delta > 0.05


def test_the_detector_finds_nothing_in_an_undegraded_world():
    """The null case. A detector that fires on a quiet world is a detector that
    will fire on anything, and every subsequent "it found the incident" result
    would be uninformative.
    """
    events = generate(1_200, seed=42, days=8)
    belt = ToolBelt(build_agent_db(events, build_traffic(events, 42, None), []))
    assert A.detect_incidents(belt) == []


def test_the_detector_uses_no_llm(belt):
    """PRD 1.1 step 3: detection is arithmetic, zero LLM calls.

    Enforced by passing no client at all -- ``detect_incidents`` has nowhere to
    reach for one. Stated as a test because "the LLM is not on the per-event path"
    is the claim the entire token budget rests on.
    """
    incidents = A.detect_incidents(belt)
    assert incidents
    assert A.blended_rate(belt, [0, 1, 2]) > 0


def test_adjacent_flagged_days_merge_into_one_incident(belt):
    """One episode, one diagnosis. Otherwise a two-day incident costs two
    investigations and PRD 1.1's amortisation argument stops holding."""
    incidents = A.detect_incidents(belt)
    assert len(incidents) == 1
    assert len(incidents[0].window) == 2


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------


def test_the_loop_stops_at_the_turn_limit_without_a_diagnosis(belt):
    """A model that never concludes must not loop forever.

    And the session still gets audited: an investigation that failed to conclude
    appears in the coverage metrics as UNSUPPORTED rather than vanishing from the
    denominator, which is how a failure rate quietly becomes flattering.
    """
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM([tool_call("get_downtime", window=[3, 4])] * 20)
    session = A.investigate(incident, belt, client, max_turns=8)
    assert len(session.turns) == 8
    assert client.calls == 8
    assert "turn limit" in session.stop_reason
    assert session.audit.status == R.STATUS_UNSUPPORTED
    assert session.audit.may_plan_action is False


def test_the_token_ceiling_stops_the_loop_early(belt):
    """Checked before each call, from measured usage, so it cannot be overshot
    by the call that discovers it."""
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM([tool_call("get_downtime", window=[3, 4])] * 20, tokens_per_call=1_000)
    session = A.investigate(incident, belt, client, max_turns=8, token_ceiling=3_000)
    assert len(session.turns) == 3
    assert session.tokens == 3_000
    assert "token ceiling" in session.stop_reason


def test_two_consecutive_unreadable_responses_end_the_session(belt):
    """One re-ask, then stop. A retry loop is how a token budget disappears."""
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM(["not json at all", "still not json", "nor this"])
    session = A.investigate(incident, belt, client, max_turns=8)
    assert len(session.turns) == 2
    assert "unreadable" in session.stop_reason
    assert all(t.parse_error for t in session.turns)


def test_one_unreadable_response_is_survivable(belt):
    """The recovery path. Small models wrap JSON in prose; losing a whole
    session to one bad turn would waste every turn already paid for."""
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM(
        [
            "I think I should look at the downtime table.",   # no JSON
            tool_call("decompose", window=[3, 4]),
            tool_call("get_downtime", window=[3, 4]),
            conclusion(
                "issuer_degraded",
                [
                    {"claim_id": "c1", "statement": "No downtime was declared.",
                     "receipt_ids": ["tc_02"]},
                    {"claim_id": "c2", "statement": "The change is partly a mix shift.",
                     "receipt_ids": ["tc_01"]},
                ],
            ),
        ]
    )
    session = A.investigate(incident, belt, client, max_turns=8)
    assert session.diagnosis.diagnosis_class == "issuer_degraded"
    assert session.audit.status == R.STATUS_SUPPORTED


def test_prose_wrapped_json_is_parsed(belt):
    """``LLMResponse.json`` takes the first and last brace, and the loop relies
    on it. Asserted here because a change there would silently turn every turn
    into a parse failure."""
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM(
        [
            'Sure! Here is my next step:\n```json\n{"thought":"x","tool":"get_downtime",'
            '"args":{"window":[3,4]}}\n```\nLet me know.',
            conclusion(
                "undiagnosed",
                [
                    {"claim_id": "c1", "statement": "No downtime was declared.",
                     "receipt_ids": ["tc_01"]},
                    {"claim_id": "c2", "statement": "The evidence does not settle the cause.",
                     "receipt_ids": ["tc_01"]},
                ],
            ),
        ]
    )
    session = A.investigate(incident, belt, client, max_turns=8)
    assert session.turns[0].tool == "get_downtime"
    assert session.turns[0].parse_error == ""


def test_the_final_turn_is_told_to_conclude(belt):
    """Without it a model spends its last turn querying and returns nothing,
    which costs the whole session for no output."""
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM([tool_call("get_downtime", window=[3, 4])] * 5)
    A.investigate(incident, belt, client, max_turns=5)
    assert "LAST turn" in client.prompts[-1]
    assert "One turn remains" in client.prompts[-2]
    assert "LAST turn" not in client.prompts[0]


# --------------------------------------------------------------------------
# What reaches the model
# --------------------------------------------------------------------------


def test_no_prompt_ever_contains_an_identifier(belt):
    """Every turn's prompt, screened. The chokepoint already does this; running
    it over the collected prompts proves the *transcript* is safe too, which is
    the part that grows with each tool result."""
    from pramaan.llm.prompts import assert_no_identifiers

    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM(
        [
            tool_call("query_sql", sql="SELECT * FROM agent_events LIMIT 5"),
            tool_call("compare_baseline", window=[3, 4]),
            tool_call("decompose", window=[3, 4]),
            tool_call("get_reason_taxonomy"),
            tool_call("get_merchant_config"),
            tool_call("get_downtime", window=[3, 4]),
        ]
    )
    A.investigate(incident, belt, client, max_turns=6)
    assert len(client.prompts) == 6
    for index, prompt in enumerate(client.prompts):
        assert_no_identifiers(prompt, context="turn %d prompt" % (index + 1))


def test_the_model_is_never_handed_rows_it_did_not_ask_for(belt):
    """The opening prompt contains a window and two rates, and no data.

    BUILD-PLAN Day 4: "Do not let raw event logs into the prompt. The agent
    QUERIES; it does not get handed the data. That is what prevents it inventing
    entities." So the turn-1 prompt must not mention any segment, code or count
    that came from the store.
    """
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM([tool_call("get_downtime", window=[3, 4])])
    A.investigate(incident, belt, client, max_turns=1)
    opening = client.prompts[0]
    # The instructions legitimately name the columns and the segments as
    # vocabulary; what must be absent is any *observation* about them.
    assert "No tool calls yet" in opening
    assert "bank_technical_error" not in opening
    assert "tier2" not in opening.split("Incident under investigation:")[1]


def test_the_transcript_grows_only_with_tool_results(belt):
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM([tool_call("get_downtime", window=[3, 4])] * 4)
    A.investigate(incident, belt, client, max_turns=4)
    lengths = [len(p) for p in client.prompts]
    assert lengths == sorted(lengths), lengths
    assert "[tc_01]" in client.prompts[1]
    assert "[tc_03]" in client.prompts[3]


def test_a_long_sql_string_is_truncated_in_the_transcript(belt):
    """The transcript is the term that grows quadratically in a multi-turn loop.

    A model does not need to re-read its own two-hundred-character query on every
    later turn, and this is where the session's token cost is either kept or lost.
    """
    incident = A.detect_incidents(belt)[0]
    long_sql = (
        "SELECT segment, cause_signal, amount_band, hour_bucket, COUNT(*) n "
        "FROM agent_events WHERE day_index IN (3,4) AND segment IN ('tier2','tier3') "
        "GROUP BY segment, cause_signal, amount_band, hour_bucket ORDER BY n DESC"
    )
    assert len(long_sql) > 160
    client = ScriptedLLM(
        [tool_call("query_sql", sql=long_sql), tool_call("get_downtime", window=[3, 4])]
    )
    A.investigate(incident, belt, client, max_turns=2)
    assert "..." in client.prompts[1]
    assert long_sql not in client.prompts[1]


# --------------------------------------------------------------------------
# Determinism -- the zero-token second run
# --------------------------------------------------------------------------


def test_the_same_session_produces_byte_identical_prompts(world):
    """The Day 4 definition-of-done item, at its root.

    "Every LLM call is cached, and a second run costs zero tokens" is true only if
    the prompt bytes are identical on the second run, because the cache key is the
    prompt. So the property to test is not the cache -- it is determinism of the
    prompts, which is what makes the cache hit.
    """
    events, traffic, downtime, _t = world
    runs = []
    for _ in range(2):
        belt = ToolBelt(build_agent_db(events, traffic, downtime))
        incident = A.detect_incidents(belt)[0]
        client = ScriptedLLM(
            [
                tool_call("decompose", window=[3, 4]),
                tool_call("compare_baseline", window=[3, 4]),
                tool_call("query_sql", sql="SELECT segment, COUNT(*) n FROM agent_events GROUP BY 1"),
            ]
        )
        A.investigate(incident, belt, client, max_turns=3)
        runs.append(client.prompts)
    assert runs[0] == runs[1]


def test_a_session_is_fully_recorded_for_the_ledger(belt):
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM(
        [
            tool_call("decompose", window=[3, 4]),
            conclusion(
                "issuer_degraded",
                [
                    {"claim_id": "c1", "statement": "Part of the change is a mix shift.",
                     "receipt_ids": ["tc_01"]},
                    {"claim_id": "c2", "statement": "Part of the change is a rate shift.",
                     "receipt_ids": ["tc_01"]},
                ],
            ),
        ]
    )
    session = A.investigate(incident, belt, client, max_turns=8)
    payload = session.as_ledger_payload()
    assert payload["turns"] == 2
    assert payload["tool_calls"] == 1
    assert payload["falsifier_stated"] is True
    assert payload["tool_log"][0]["call_id"] == "tc_01"
    assert payload["tool_log"][0]["result_hash"].startswith("sha256:")
    # The result itself is referenced by hash, never embedded: a ledger row is an
    # audit record, not a data warehouse.
    assert "result" not in payload["tool_log"][0]


# --------------------------------------------------------------------------
# Robustness of the diagnosis parse
# --------------------------------------------------------------------------


def test_an_invented_diagnosis_class_degrades_to_undiagnosed(belt):
    """The class is a signature field with a closed domain.

    A model inventing one should degrade to "I do not know" rather than take the
    session down or, worse, widen the signature space and collapse memoisation.
    """
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM(
        [
            tool_call("get_downtime", window=[3, 4]),
            conclusion(
                "the_bank_is_having_a_bad_day",
                [
                    {"claim_id": "c1", "statement": "No downtime was declared.",
                     "receipt_ids": ["tc_01"]},
                    {"claim_id": "c2", "statement": "The platform declared nothing.",
                     "receipt_ids": ["tc_01"]},
                ],
            ),
        ]
    )
    session = A.investigate(incident, belt, client, max_turns=8)
    assert session.diagnosis.diagnosis_class == "undiagnosed"


def test_a_diagnosis_is_never_supported_before_the_auditor_sees_it(belt):
    """Fail-closed. A parse failure must not produce a supported diagnosis."""
    from pramaan.investigate.agent import _parse_diagnosis

    parsed = _parse_diagnosis({"diagnosis_class": "issuer_degraded", "confidence": 0.99}, belt, "x")
    assert parsed.status == "UNSUPPORTED"
    assert _parse_diagnosis("not a dict", belt, "x").diagnosis_class == "undiagnosed"


def test_confidence_out_of_range_is_clamped(belt):
    from pramaan.investigate.agent import _parse_diagnosis

    assert _parse_diagnosis({"confidence": 7.5}, belt, "x").confidence == 1.0
    assert _parse_diagnosis({"confidence": -3}, belt, "x").confidence == 0.0
    assert _parse_diagnosis({"confidence": "high"}, belt, "x").confidence == 0.0


def test_a_missing_falsifier_is_recorded_as_a_failed_instruction(belt):
    """Not raised, recorded. The rate at which the model skips the instruction is
    a number worth publishing, and a validator would hide it."""
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM(
        [
            tool_call("get_downtime", window=[3, 4]),
            conclusion(
                "issuer_degraded",
                [
                    {"claim_id": "c1", "statement": "No downtime was declared.",
                     "receipt_ids": ["tc_01"]},
                    {"claim_id": "c2", "statement": "Nothing was declared upstream.",
                     "receipt_ids": ["tc_01"]},
                ],
                falsifiable_by="unknown",
            ),
        ]
    )
    session = A.investigate(incident, belt, client, max_turns=8)
    assert session.diagnosis.falsifiable_by == "unknown"
    assert session.diagnosis.falsifier_stated is False
    assert session.as_ledger_payload()["falsifier_stated"] is False


# --------------------------------------------------------------------------
# The end-to-end find: the Day 4 definition-of-done item
# --------------------------------------------------------------------------


def test_the_investigator_finds_the_injected_degradation_and_separates_the_terms(world):
    """A full investigation, ending in a diagnosis that survives the audit.

    The claims are written from the tool results rather than hardcoded, because a
    test whose "correct answer" is a typed number starts failing for the wrong
    reason the moment the simulator moves. What is asserted is the *structure* of
    the finding:

    - the rate effect is real and positive: something broke;
    - the mix effect is comparably large: half the blended rise is traffic moving
      toward a slice that always failed more, and acting on it would be a false
      positive;
    - the slice that broke is tier2, and it is the one the injection broke;
    - the slice whose volume moved is tier3, and its own rate did not move;
    - no platform downtime was declared, so the conclusion rests on the
      decomposition rather than on corroboration;
    - and the whole thing passes the receipt auditor at 100% coverage.
    """
    events, traffic, downtime, truth = world
    belt = ToolBelt(build_agent_db(events, traffic, downtime))
    incident = A.detect_incidents(belt)[0]

    # Turns 1-4: the investigation a competent analyst would run.
    steps = [
        tool_call("decompose", window=list(incident.window), dimension="segment"),
        tool_call("compare_baseline", window=list(incident.window)),
        tool_call("get_downtime", window=list(incident.window)),
        tool_call(
            "query_sql",
            sql="SELECT cause_signal, COUNT(*) n FROM agent_events WHERE segment='tier2' "
                "AND day_index IN (3,4) GROUP BY 1 ORDER BY n DESC LIMIT 3",
        ),
    ]
    probe = ScriptedLLM(steps)
    A.investigate(incident, belt, probe, max_turns=4, audit=False)

    decomp = belt.get("tc_01").result
    compare = {r["key"]: r for r in belt.get("tc_02").result["rows"]}

    # The structure of the finding, asserted before it is claimed.
    assert decomp["rate_effect"] > 0.02, "no real rate shift to find"
    assert decomp["mix_effect"] > 0.02, "no mix confound to separate"
    assert compare["tier2"]["delta"] > 0.08, "the broken slice did not break"
    assert abs(compare["tier3"]["delta"]) < 0.05, "the mix slice's own rate moved"
    assert belt.get("tc_03").result["count"] == 0, "downtime was declared after all"
    assert truth.rate_segment == "tier2" and truth.mix_segment == "tier3"

    # Now the same investigation, concluding with claims built from that evidence.
    belt = ToolBelt(build_agent_db(events, traffic, downtime))
    client = ScriptedLLM(
        steps
        + [
            conclusion(
                "issuer_degraded",
                [
                    {
                        "claim_id": "c1",
                        "statement": "Of the blended rise, %+.1fpp is a real rate shift and "
                        "%+.1fpp is traffic mix. I am acting on the rate shift only."
                        % (100 * decomp["rate_effect"], 100 * decomp["mix_effect"]),
                        "receipt_ids": ["tc_01"],
                    },
                    {
                        "claim_id": "c2",
                        "statement": "tier2 moved %+.1fpp against its own trailing baseline, "
                        "while tier3 moved %+.1fpp and is therefore not broken."
                        % (100 * compare["tier2"]["delta"], 100 * compare["tier3"]["delta"]),
                        "receipt_ids": ["tc_02"],
                    },
                    {
                        "claim_id": "c3",
                        "statement": "No platform downtime was declared over this window.",
                        "receipt_ids": ["tc_03"],
                    },
                ],
                summary="tier2's own failure rate rose while tier3's did not; roughly half "
                "the blended rise is a mix shift toward tier3 and is excluded from action.",
                confidence=0.72,
                falsifiable_by="If tier2's bank_technical_error share is flat across the "
                "window, the rate shift is not issuer-driven and this is wrong.",
            )
        ]
    )
    session = A.investigate(incident, belt, client, max_turns=8)

    assert session.diagnosis.diagnosis_class == "issuer_degraded"
    assert session.audit.status == R.STATUS_SUPPORTED
    assert session.audit.receipt_coverage == 1.0
    assert session.audit.support == 1.0
    assert session.audit.stripped == ()
    assert session.audit.may_plan_action is True
    assert session.diagnosis.falsifier_stated is True
    assert session.tool_calls == 4
    assert len(session.diagnosis.receipts) == 3
