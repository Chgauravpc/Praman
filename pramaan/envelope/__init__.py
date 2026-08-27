"""The policy envelope: the one place in this system where the LLM is not.

Every plan the LLM proposes passes through ``judge`` before anything happens, and
``judge`` is deterministic, cites the rule it applied, and has no model in it.
That is not a statement about LLMs being unreliable -- the rest of this codebase
is built the other way round, LLM-forward on purpose (F1). It is a statement
about **citability**: a compliance decision has to be answerable to "which rule,
and where does it say that", and a decision produced by a model is answerable to
"the model said so". Those are different products.

PRD 4.1 names three places the LLM is absent, each for its own engineering
reason: money movement (idempotency), arm assignment and estimation
(verifiability), and this component (citability).

Usage::

    from pramaan.envelope import EnvelopeContext, Step, judge

    verdict = judge(
        Step(action="ACT_MESSAGE", channel="sms"),
        EnvelopeContext(
            at="2026-08-03T19:05:00+05:30",
            legal_context="collection",
            reason_code="insufficient_funds",
            dlt_template_id="1207xxxxxxxxxxx",
            self_identification_scripted=True,
        ),
    )
    verdict.verdict   # 'REJECT'
    verdict.rule_id   # 'R9'

The five modules, and which question each answers:

``rules.py``      what a regulator requires -- R1-R11, each with its instrument
                  and the grade of that citation. Becomes ``SAFETY.md``.
``windows.py``    when a contact is lawful -- the (legal_context x channel x
                  hour) matrix, as a table meant to be read against the
                  regulation rather than traced.
``reason_map.py`` what is physically capable of working -- G1-G8, from Razorpay's
                  documented decline reasons. Futility, not legality.
``tiers.py``      how badly a mistake would hurt -- T0-T4 reversibility, and the
                  gates each tier has to clear.
``stopping.py``   when to stop entirely -- S1-S7.
``judge.py``      the order all of the above are applied in, and why that order
                  is the interesting part.
``registry.py``   every rule id the envelope can emit, derived from the five
                  modules above rather than listed by hand.
"""
from __future__ import annotations

from pramaan.envelope.context import (
    ALLOW,
    AMEND,
    NO_RULE_BINDS,
    REJECT,
    VERDICTS,
    EnvelopeContext,
    Judgement,
    Ruling,
)
from pramaan.envelope.judge import Step, as_step, effective_context, judge, judge_plan
from pramaan.envelope.registry import ALL_RULE_IDS, POLICY_RULES
from pramaan.envelope.rules import RULE_IDS, RULE_SOURCES
from pramaan.envelope.stopping import TERMINAL_RULES
from pramaan.envelope.tiers import ACTION_TIER, TIER_SPEC, TIERS, tier_of
from pramaan.envelope.windows import (
    CHANNELS,
    HOURLY_GRID,
    LEGAL_WINDOWS,
    channel_eligibility_for_bucket,
    check_window,
)

__all__ = [
    "ALLOW",
    "AMEND",
    "REJECT",
    "VERDICTS",
    "NO_RULE_BINDS",
    "EnvelopeContext",
    "Judgement",
    "Ruling",
    "Step",
    "as_step",
    "effective_context",
    "judge",
    "judge_plan",
    "ALL_RULE_IDS",
    "POLICY_RULES",
    "RULE_IDS",
    "RULE_SOURCES",
    "TERMINAL_RULES",
    "ACTION_TIER",
    "TIER_SPEC",
    "TIERS",
    "tier_of",
    "CHANNELS",
    "HOURLY_GRID",
    "LEGAL_WINDOWS",
    "channel_eligibility_for_bucket",
    "check_window",
]
