"""Every action in ``canonical.ACTIONS`` is exercised by a real component.

``test_ledger_kind_coverage.py`` does this for ``LEDGER_KINDS`` and ADR-011 gives
the reason: an enum whose members nothing exercises "reads as aspirational".
There was no equivalent check for the *action* vocabulary, and the gap was not
theoretical -- the track brief describes an agent that "plans a recovery action
(retry, message, voice call, escalate, concession, or do nothing)", and on the
committed 6,000-event batch **two of those six never occur**: across all 303
``PLAN`` rows and all 6,000 ``OUTCOME`` rows, ``ACT_VOICE`` and
``ACT_CONCESSION`` are proposed zero times and executed zero times.

That is a fact about the batch, not about the code, and the difference is what
this module pins. Both actions are fully built: offered to the planner in
``llm/prompts.py``'s action menu, admitted by ``schemas.py``, tiered and gated,
and -- for voice -- run end to end by ``converse/voice.py``. What neither has is
an *occurrence* in a batch run, because the deterministic reason-class default
(``taxonomy.DEFAULT_ACTION_BY_CLASS``, which ``planner.default_plan_for`` must
keep identical to arm B's policy) maps no reason class to either one.

So "is it real?" is the wrong question and "how far does it get?" is the right
one. ``ACTION_CEILING`` below answers it per action, and these tests assert the
answer both ways: every action reaches its stated ceiling, and the two that stop
short of an ``OUTCOME`` genuinely stop there. **The ceiling table is a claim
about the system, so a future executor that quietly starts honouring
concessions fails this file rather than silently outdating a README.**
"""
from __future__ import annotations

import sqlite3
from typing import Dict, List

import pytest

from pramaan import canonical, taxonomy
from pramaan.converse import voice
from pramaan.converse.promises import PROMISED
from pramaan.envelope import EnvelopeContext, Step, judge
from pramaan.eval.resolve import resolve_batch
from pramaan.ledger.chain import Ledger
from pramaan.sense.store import connect
from sim.generate import full_batch_all_types

#: How far each action is actually carried by a real component, measured.
#:
#: ``OUTCOME``  -- proposed by ``eval.arms.arm_step`` and resolved into an
#:                 ``OUTCOME`` ledger row on the committed batch.
#: ``CONVERSE`` -- never chosen by the batch planner, but driven end to end by
#:                 ``converse.voice.place_call``, envelope-gated, writing
#:                 ``CONVERSE``/``PROMISE`` rows. Real, separately driven.
#: ``GATE``     -- judged by the real envelope and nothing further. No executor
#:                 exists, so it can be proposed and refused but never taken.
ACTION_CEILING: Dict[str, str] = {
    "ACT_WAIT": "OUTCOME",
    "ACT_ROUTE": "OUTCOME",
    "ACT_RETRY": "OUTCOME",
    "ACT_MESSAGE": "OUTCOME",
    "ACT_ALERT_MERCHANT": "OUTCOME",
    "ACT_PAGE_ENGINEER": "OUTCOME",
    "ACT_ESCALATE_HUMAN": "OUTCOME",
    "ACT_STOP": "OUTCOME",
    "ACT_VOICE": "CONVERSE",
    "ACT_CONCESSION": "GATE",
}

#: The six the track brief names, in its own words, mapped to this vocabulary.
#: Kept explicit so a reader can check the brief against the code without
#: trusting a paraphrase.
BRIEF_ACTIONS: Dict[str, str] = {
    "retry": "ACT_RETRY",
    "message": "ACT_MESSAGE",
    "voice call": "ACT_VOICE",
    "escalate": "ACT_ESCALATE_HUMAN",
    "concession": "ACT_CONCESSION",
    "do nothing": "ACT_WAIT",
}

THURSDAY_1520 = "2026-08-13T15:20:00+05:30"  # inside R9's collection window


def _benign_context(action: str) -> EnvelopeContext:
    """A context in which ``action`` is a sensible thing to propose.

    Per-action rather than one shared default, because a single context cannot
    be simultaneously realistic for a merchant-config alert and a voice call.
    Every field set here is set to make the action *plausible*, never to force a
    particular verdict -- the verdict is what the tests read.
    """
    common = dict(
        at=THURSDAY_1520,
        reason_code="insufficient_funds",
        amount_paise=250_000,
        consent="explicit",
    )
    if action == "ACT_VOICE":
        return EnvelopeContext(
            legal_context="collection",
            ai_disclosure_scripted=True,
            self_identification_scripted=True,
            **common,
        )
    if action == "ACT_CONCESSION":
        # Under P3's Rs 5,000 autonomous cap, so this is the *allowed* case.
        return EnvelopeContext(amount_paise=100_000, **{
            k: v for k, v in common.items() if k != "amount_paise"
        })
    if action == "ACT_MESSAGE":
        return EnvelopeContext(dlt_template_id="t", **common)
    return EnvelopeContext(**common)


def _channel_for(action: str) -> str:
    return {"ACT_VOICE": "voice", "ACT_MESSAGE": "sms"}.get(action, "none")


# --------------------------------------------------------------------------
# The table is complete and honest about itself
# --------------------------------------------------------------------------


def test_the_ceiling_table_covers_exactly_the_action_vocabulary():
    """Same discipline ``ACTION_TIER`` is held to: no member unaccounted for."""
    missing = set(canonical.ACTIONS) - set(ACTION_CEILING)
    extra = set(ACTION_CEILING) - set(canonical.ACTIONS)
    assert not missing and not extra, (
        "ACTION_CEILING must cover exactly canonical.ACTIONS; "
        "missing=%r extra=%r" % (sorted(missing), sorted(extra))
    )


def test_every_brief_action_maps_to_a_real_member_of_the_vocabulary():
    for phrase, action in BRIEF_ACTIONS.items():
        assert action in canonical.ACTIONS, "%s -> %s" % (phrase, action)


# --------------------------------------------------------------------------
# Stage 1: the envelope judges all ten
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", canonical.ACTIONS)
def test_every_action_gets_a_verdict_and_a_named_rule(action: str):
    """No action falls through the envelope unjudged.

    This is the floor every action clears, including the two the batch never
    proposes: an unreachable action would still have to be *refusable*, and one
    that returned no rule id would be a hole in the audit trail rather than a
    permission.
    """
    judgement = judge(Step(action, _channel_for(action)), _benign_context(action))
    assert judgement.verdict in ("ALLOW", "AMEND", "REJECT")
    assert judgement.rule_id, "%s produced no rule id" % action
    assert judgement.rulings, "%s produced no rulings" % action


# --------------------------------------------------------------------------
# Stage 2: the eight the batch actually takes
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def batch_actions() -> Dict[str, int]:
    """Actions the real resolver emits on the committed five-type full batch.

    The full batch, not the dev batch: 200 events do not contain a
    MERCHANT_CONFIG or INTEGRATION_BUG failure, so the dev batch reaches only
    six of the eight and would make this test pass for the wrong reason.
    """
    counts: Dict[str, int] = {}
    for outcome in resolve_batch(full_batch_all_types(42)):
        counts[outcome.action] = counts.get(outcome.action, 0) + 1
    return counts


@pytest.mark.parametrize(
    "action", [a for a, c in ACTION_CEILING.items() if c == "OUTCOME"]
)
def test_outcome_ceiling_actions_appear_in_real_outcome_rows(
    action: str, batch_actions: Dict[str, int]
):
    assert batch_actions.get(action, 0) > 0, (
        "%s is declared to reach an OUTCOME but the batch never emitted it. "
        "Either the resolver changed or the ceiling table is now wrong." % action
    )


def test_the_batch_emits_no_action_outside_the_vocabulary(batch_actions):
    """``none`` is the resolver's own word for "the arm did nothing"."""
    unknown = set(batch_actions) - set(canonical.ACTIONS) - {"none"}
    assert not unknown, "resolver emitted unknown actions: %r" % sorted(unknown)


# --------------------------------------------------------------------------
# Stage 3: voice -- a real writer, driven outside the batch
# --------------------------------------------------------------------------


def test_voice_is_gated_by_r9_and_reaches_the_ledger():
    """The ceiling ``ACTION_CEILING`` claims for ``ACT_VOICE``, exercised.

    Drives the real ``place_call`` loop, the real envelope and the real ledger
    writer -- the same path ``make voice`` runs. This is what makes "voice call"
    a built capability despite never appearing in a batch ``OUTCOME`` row.
    """
    context = _benign_context("ACT_VOICE")
    preflight = voice.preflight(context)
    assert preflight.verdict == "ALLOW"
    assert preflight.rule_id == "R9", (
        "the collection-window rule is what should decide a recovery call"
    )

    result = voice.place_call(
        ["Theek hai, Friday tak pakka kar dunga, pura amount."],
        context,
        counterparty_id="cp_action_coverage",
    )
    assert result.call_placed

    conn = connect(None)
    ledger = Ledger(conn)
    voice.write_call_to_ledger(ledger, result, THURSDAY_1520)
    kinds = dict(ledger.kind_counts())
    assert kinds.get("CONVERSE", 0) == 1
    assert kinds.get("PROMISE", 0) == 1, (
        "the Hinglish promise should have been extracted and recorded"
    )
    assert ledger.verify_chain().ok
    conn.close()


def test_voice_is_never_proposed_by_the_batch_planner(batch_actions):
    """The other half of the claim, and the reason it is worth stating.

    ``ACT_VOICE`` is built and gated, but no reason class defaults to it, so no
    batch run has ever chosen to call a customer. A reviewer told "the agent can
    place a voice call" should be able to find this test rather than discover
    the zero themselves.
    """
    assert batch_actions.get("ACT_VOICE", 0) == 0
    assert "ACT_VOICE" not in taxonomy.DEFAULT_ACTION_BY_CLASS.values()


# --------------------------------------------------------------------------
# Stage 4: concession -- gated, and that is the whole of it
# --------------------------------------------------------------------------


def test_a_concession_within_the_cap_is_allowed():
    judgement = judge(Step("ACT_CONCESSION"), _benign_context("ACT_CONCESSION"))
    assert judgement.verdict == "ALLOW"


def test_a_concession_over_the_cap_is_refused_by_p3():
    """P3 is a *house* rule, not a regulator's, and it still binds.

    Rs 5,000 is the line above which giving money away stops being a recovery
    tactic and becomes a commercial decision. The rule id is asserted because
    "refused" without a citation is not an audit trail.
    """
    judgement = judge(
        Step("ACT_CONCESSION"),
        EnvelopeContext(
            at=THURSDAY_1520,
            reason_code="insufficient_funds",
            consent="explicit",
            amount_paise=50_000_000,  # Rs 5,00,000, a hundred times the cap
        ),
    )
    assert judgement.verdict == "REJECT"
    assert judgement.rule_id == "P3"
    assert "autonomous limit" in (judgement.reason or "")


def test_concession_has_no_executor_and_the_ceiling_says_so(batch_actions):
    """The pin. If a concession executor ever lands, this fails on purpose.

    Failing here is the intended behaviour, not an obstacle: it means
    ``ACTION_CEILING`` above and the note under ``canonical.ACTIONS`` both need
    updating in the same commit that gives the action a writer.
    """
    assert ACTION_CEILING["ACT_CONCESSION"] == "GATE"
    assert batch_actions.get("ACT_CONCESSION", 0) == 0
    assert "ACT_CONCESSION" not in taxonomy.DEFAULT_ACTION_BY_CLASS.values()


# --------------------------------------------------------------------------
# The brief's six, together
# --------------------------------------------------------------------------


def test_all_six_brief_actions_are_exercised_by_a_real_component(batch_actions):
    """Six of six reach a real component. Four of six reach an ``OUTCOME``.

    Both sentences are true and only one of them is the brief's implication, so
    this test asserts the pair rather than the flattering half.
    """
    reached: Dict[str, str] = {}
    for phrase, action in BRIEF_ACTIONS.items():
        ceiling = ACTION_CEILING[action]
        if ceiling == "OUTCOME":
            assert batch_actions.get(action, 0) > 0
        reached[phrase] = ceiling

    assert reached == {
        "retry": "OUTCOME",
        "message": "OUTCOME",
        "escalate": "OUTCOME",
        "do nothing": "OUTCOME",
        "voice call": "CONVERSE",
        "concession": "GATE",
    }
    in_batch = [p for p, a in BRIEF_ACTIONS.items() if ACTION_CEILING[a] == "OUTCOME"]
    assert len(in_batch) == 4, (
        "four of the brief's six actions occur in a batch run; the other two are "
        "built and gated but never planner-selected. Update the docs, not this "
        "number, if that changes."
    )
