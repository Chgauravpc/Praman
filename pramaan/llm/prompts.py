"""Prompt construction. Canonical banded features only -- never identifiers.

This is the module PRD 9.1 calls the single most likely way the build breaks in a
way no test catches:

    If a prompt embeds any high-cardinality identifier -- payment_id, event_id, a
    raw timestamp, a customer name -- then every call is a cache miss and every
    signature is unique. The hit rate collapses to zero, the ~30x memoisation
    ratio becomes 1x, and the token budget goes from ~800K to ~15M. The system
    still *works*. It just becomes unaffordable and non-reproducible, and it does
    so silently.

So the defence is structural rather than a review habit, in three layers:

1. **Whitelist construction.** A prompt is rendered from a feature dict that has
   already passed ``canonical.validate_features`` -- exactly the seven frozen
   fields, each checked against a closed domain. An identifier cannot arrive by
   being added to the feature set, because an eighth key is a hard error.
2. **A screen on the rendered bytes** (``assert_no_identifiers``). This catches
   the other route in: an identifier smuggled inside a field's *value*, or
   pasted into a template while editing it.
3. **The invariant test** (``tests/test_prompt_canonical.py``, invariant I2): two
   different events sharing a signature must produce byte-identical prompt
   bytes. HANDOFF 9 is blunt about this one -- a broken canonicalisation
   invariant is a budget emergency, not a bug.

Note what is *not* here: the exact amount. The planner needs to know an amount
sits in the 500-5k band, not that it is Rs 2,437. Banding is what makes the
signature space finite, and unbanded amounts alone would make every event unique.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

from pramaan.canonical import (
    AMOUNT_BAND_LABELS,
    PLANNER_SIGNATURE_FIELDS,
    planner_signature,
    validate_features,
)
from pramaan.taxonomy import REASON_CLASS_POLICY

# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------
#
# Each pattern corresponds to a specific way the invariant has a realistic
# chance of breaking, and the name is the error message. Deliberately strict:
# a false positive costs one prompt edit, a false negative costs the token
# budget.

FORBIDDEN_PATTERNS: Sequence[Tuple[str, "re.Pattern[str]"]] = (
    (
        "razorpay-style identifier",
        # pay_XXXX, order_XXXX, sub_, cust_, inv_, rzp_, plus this project's own
        # evt_ / cp_ prefixes.
        #
        # **Corrected on Day 4, and the correction matters.** The original pattern
        # was `PREFIX_[A-Za-z0-9]{3,}`, which refuses any token beginning with one
        # of these prefixes and an underscore. That is too broad, because three
        # legitimate Razorpay *reason codes* are shaped exactly that way --
        # `order_already_paid`, `order_amount_mismatch`,
        # `order_payment_method_mismatch` -- as is this project's own future tool
        # name `run_canary`.
        #
        # Those are closed-domain enum values, not identifiers. Refusing them is a
        # false positive with a real cost: PRD 6.2 makes grounding the investigator
        # in reason-code semantics an explicit job of `get_reason_taxonomy`
        # ("instead of guessing what `upi_autopay_not_supported_on_psp` means"),
        # and `cause_signal` is a column in the agent's own projection, so any
        # query grouping by it renders codes. The screen was refusing the domain
        # vocabulary the agent exists to reason about. Nothing caught it for three
        # days because nothing had yet put a reason code in a prompt.
        #
        # So the discrimination is now on identifier *shape* rather than on the
        # prefix alone. A Razorpay id is base-62 random -- `pay_29QQoUBi66xm2f` --
        # so it carries a digit or a capital. A reason code is lowercase words
        # joined by underscores and carries neither.
        re.compile(
            r"\b(?:pay|order|sub|plan|inv|cust|txn|rzp|evt|cp|run)_"
            r"(?=[A-Za-z0-9_]*[A-Z0-9])[A-Za-z0-9_]{3,}"
        ),
    ),
    (
        "identifier-shaped token",
        # The backstop for the one case the shape rule above would miss: an id
        # that happens to be all lowercase with no digits. For a base-62 random
        # id that is about a one-in-eighty-thousand event, but "unlikely" is not
        # the standard this screen is held to, and the giveaway is structural
        # rather than statistical -- a real id is one long unbroken run of
        # alphanumerics, where a reason code is short words separated by
        # underscores. The longest single word following an id prefix anywhere in
        # the 69-code taxonomy is seven characters, so twelve is a wide margin.
        re.compile(r"\b(?:pay|order|sub|plan|inv|cust|txn|rzp|evt|cp|run)_[A-Za-z0-9]{12,}"),
    ),
    (
        "raw date or timestamp",
        re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}"),
    ),
    (
        "exact currency amount",
        re.compile(r"(?:Rs\.?|INR|₹)\s*\d"),
    ),
    (
        "long digit run (an exact amount or an id)",
        # Four or more consecutive digits. Band labels ("500-5k"), rule IDs
        # ("R11") and step counts all stay under this.
        re.compile(r"\d{4,}"),
    ),
    (
        "hex digest",
        re.compile(r"\b[0-9a-f]{16,}\b"),
    ),
    (
        "email address",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"),
    ),
    (
        "Indian mobile number",
        re.compile(r"(?:\+91[\s-]?)?[6-9]\d{9}"),
    ),
)


class PromptCanonicalityError(AssertionError):
    """A prompt contained something high-cardinality. Treated as a build break.

    An AssertionError subclass on purpose: this is the same category of failure
    as a broken invariant, not a recoverable runtime condition. Nothing should
    catch it.
    """


def assert_no_identifiers(prompt: str, *, context: str = "prompt") -> str:
    """Screen rendered prompt bytes. Returns the prompt so it can wrap a return.

    Runs on every prompt build, in production as well as in tests. The cost is a
    handful of regexes against a few hundred bytes; the alternative is
    discovering the problem four days later as a token overrun.
    """
    for label, pattern in FORBIDDEN_PATTERNS:
        match = pattern.search(prompt)
        if match:
            raise PromptCanonicalityError(
                "%s contains a %s: %r.\n"
                "Prompts are built from canonical banded features only "
                "(PRD 9.1, anti-pattern A3). Identifiers travel alongside the "
                "prompt for logging, never inside it -- an identifier here makes "
                "every call a cache miss and takes the token budget from ~800K "
                "to ~15M, silently."
                % (context, label, match.group(0))
            )
    return prompt


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_features(features: Dict[str, Any]) -> str:
    """The feature block, in the frozen field order.

    Iterating PLANNER_SIGNATURE_FIELDS rather than the dict is what makes the
    bytes stable: a dict's insertion order would otherwise leak into the prompt
    and two events with identical features could produce different bytes.
    """
    validate_features(features)
    lines = []
    for field in PLANNER_SIGNATURE_FIELDS:
        value = features[field]
        if field == "amount_band":
            # Rendered as the band label, never the underlying rupee figure.
            value = "%d (%s)" % (value, AMOUNT_BAND_LABELS[value])
        lines.append("- %s: %s" % (field, value))
    return "\n".join(lines)


PLANNER_INSTRUCTIONS = """\
You are the recovery planner for an Indian payments platform. A payment has \
failed and you must compose a bounded recovery plan.

You are given a canonical situation, not a specific transaction. The plan you \
write will be reused for every future case matching this same situation, so \
reason about the class of case, never about an individual customer.

Action space, with reversibility tier:
- ACT_WAIT (T0)            do nothing yet; the payer is likely retrying already
- ACT_ROUTE (T0)           change routing or switch rail
- ACT_RETRY (T1)           re-attempt the charge; reversible
- ACT_MESSAGE (T2)         SMS, WhatsApp, email or in-app; NOT reversible
- ACT_VOICE (T3)           outbound voice call; the least reversible contact
- ACT_CONCESSION (T4)      discount or waiver; needs human approval
- ACT_ALERT_MERCHANT (T0)  the merchant's own configuration is at fault
- ACT_PAGE_ENGINEER (T0)   the merchant's integration is at fault
- ACT_ESCALATE_HUMAN (T4)  hand to a person
- ACT_STOP                 terminate the thread

Hold on to the asymmetry: a charge retry is refundable, a message is not, and a \
phone call really is not. Gate contact harder than you gate retries.

Constraints you must respect:
- channel_eligibility is authoritative. silent_only means no customer contact of \
any kind. silent_and_message forbids voice.
- Step timing is a delay in seconds measured from the moment the case becomes \
actionable. Never an absolute time.
- Every step names the stop conditions that abort it.
- If the right first action is to do nothing, say so. Waiting is a real action \
here, not a failure to plan.

Reason-class guardrail for this situation:
%(guardrail)s

Situation:
%(features)s

Reply with a single JSON object:
{"steps": [{"step_index": 0, "action": "...", "channel": "...", \
"delay_seconds": 0, "expected_value_paise": 0, "cost_paise": 0, \
"stop_conditions": ["..."], "rationale": "..."}], \
"expected_value_paise": 0, "rationale": "..."}
"""


def build_planner_prompt(features: Dict[str, Any]) -> str:
    """The planner prompt for one signature. Byte-identical per signature.

    Everything that varies comes from the validated feature dict or is derived
    from ``reason_class``, which is itself one of the seven fields. Nothing here
    can reach outside that set.
    """
    validate_features(features)
    policy = REASON_CLASS_POLICY[features["reason_class"]]
    guardrail = "- retry: %s\n- customer contact: %s\n- %s" % (
        policy.retry_mode,
        policy.contact,
        policy.envelope_note,
    )
    prompt = PLANNER_INSTRUCTIONS % {
        "guardrail": guardrail,
        "features": render_features(features),
    }
    return assert_no_identifiers(prompt, context="planner prompt")


INVESTIGATOR_INSTRUCTIONS = """\
You are the investigator for an Indian payments platform. You are given a \
canonical situation and a read-only tool belt. Find the cause.

Rules:
- Every claim you make must cite the receipt of a tool call that supports it. An \
uncited claim will be stripped before it can influence any action, so an honest \
"insufficient evidence" is worth more than a plausible guess.
- You may revise a hypothesis. Say what changed your mind.
- Conclude with exactly one diagnosis_class from the allowed list.

Allowed diagnosis_class values:
%(classes)s

Situation:
%(features)s

Reply with a single JSON object:
{"diagnosis_class": "...", "summary": "...", \
"claims": [{"claim_id": "c1", "statement": "...", "receipt_ids": ["r1"]}]}
"""


def build_investigator_prompt(
    features: Dict[str, Any], allowed_classes: Sequence[str]
) -> str:
    """The investigator prompt. Same rules; wired up on Day 4."""
    validate_features(features)
    prompt = INVESTIGATOR_INSTRUCTIONS % {
        "classes": "\n".join("- %s" % c for c in allowed_classes),
        "features": render_features(features),
    }
    return assert_no_identifiers(prompt, context="investigator prompt")


# --------------------------------------------------------------------------
# The investigator, at incident granularity (Day 4)
# --------------------------------------------------------------------------
#
# ``build_investigator_prompt`` above is the per-signature shape written on
# Day 1. It is kept, and it is not what the agent loop uses. The reason is PRD
# 1.1: the investigator runs **per incident**, not per event -- "a degradation
# episode spanning 400 failures has one root cause, and diagnosing it 400 times
# is not expensive, it is wrong". So the loop prompt describes a *window under
# suspicion* plus a tool belt, not one event seven features.
#
# Everything variable in the turn prompt arrives already screened: the incident
# brief is built from integer day indices and formatted rates, and the transcript
# is the concatenation of tool renderings that ``ToolBelt._record`` has already
# put through ``assert_no_identifiers``. The screen runs again on the assembled
# bytes anyway, because a template edit is exactly the route in that layer 1
# cannot see.

TOOL_CATALOGUE = """\
- query_sql(sql)               Read-only SELECT over the tables below. This is
                               where you can ask something nobody anticipated.
- compare_baseline(window, segment=None, dimension="segment")
                               Failure rate in a window against that slice OWN
                               trailing baseline -- never the fleet average,
                               because a structurally weak but stable slice would
                               alarm forever.
- get_downtime(window)         Downtimes the platform itself declared. An empty
                               result is a finding, not missing data.
- decompose(window, dimension="segment")
                               Splits a change in blended failure rate into RATE
                               (something broke), MIX (traffic moved) and
                               INTERACTION. The three sum to the observed change
                               exactly.
- get_merchant_config()        Enabled methods, networks, mandate and auth
                               settings.
- get_reason_taxonomy()        What each reason code means, and its action class.

Windows are [start_day, end_day], inclusive, in integer day indices.

Readable tables, and every column in them:

agent_events    day_index, hour_of_day, hour_bucket, weekday, source_type,
                cause_signal, reason_class, segment, counterparty_kind,
                legal_context, decay_profile, amount_band, amount_band_label,
                afa_exempt, channel_eligibility, retry_eligible,
                contact_verdict, arm
                -- one row per FAILED payment. There is no row here for a
                   payment that succeeded, so COUNT(*) counts failures and is
                   never a rate.
agent_traffic   day_index, segment, attempts, failures
                -- the denominator. failures/attempts is the failure rate.
agent_downtime  day_index_start, hour_start, day_index_end, hour_end, method,
                severity, scope, entity, status
"""

INVESTIGATOR_TURN_INSTRUCTIONS = """\
You are the investigator for an Indian payments platform. A deterministic
detector has flagged a window in which the failure rate looks elevated. Your job
is to find out why. You cannot act on anything: you produce a diagnosis, and a
separate policy layer decides what if anything to do with it.

How your output gets used, because it changes what is worth writing:

- Every claim you make must cite the call_id of a tool call that supports it.
- A deterministic auditor then checks, with no model involved, that each cited
  call_id exists, that its recorded output has not changed, and that every NUMBER
  in your claim actually appears in the output you cited. Claims failing any of
  those are STRIPPED, and if too few survive, the whole diagnosis is marked
  UNSUPPORTED and nothing acts on it.
- So a claim you cannot evidence is worse than no claim at all, and an honest
  "the evidence does not settle this" is worth more than a plausible guess.

The one reasoning trap on this data, stated because it is the whole difficulty:

  A rise in the blended failure rate has three possible causes and only ONE of
  them is a degradation. Something broke (RATE). Or traffic moved toward a slice
  that already failed more often than average, so the blend moved with nothing
  broken (MIX). Or both (INTERACTION). Indian payment traffic makes this acute:
  metro succeeds around four fifths of the time and Tier-3 closer to three
  fifths, so a modest shift in where traffic comes from moves the blended number
  on its own.

  Do not report a blended change as a degradation without decomposing it. If part
  of the rise is mix, say which part, and say that you are not acting on it.

Tools:
%(tools)s

Allowed diagnosis_class values. You must choose exactly one:
%(classes)s

%(incident)s
%(transcript)s
Turn %(turn)s of %(max_turns)s.%(pressure)s

Reply with ONE JSON object and nothing else. Either call a tool:

{"thought": "what you are testing and why", "tool": "query_sql",
 "args": {"sql": "SELECT ..."}}

or conclude:

{"thought": "...", "diagnosis": {
  "diagnosis_class": "one of the values above",
  "summary": "what happened, in two or three sentences",
  "confidence": 0.0,
  "falsifiable_by": "the observation that would show this is wrong",
  "claims": [
    {"claim_id": "c1", "statement": "one checkable assertion",
     "receipt_ids": ["tc_01"]}
  ]}}

Requirements on the diagnosis: at least two claims, every claim citing at least
one call_id you have actually made, and falsifiable_by naming a concrete
observation rather than restating the hypothesis.
"""


def build_incident_brief(
    window: Sequence[int],
    baseline: Sequence[int],
    blended_baseline: float,
    blended_window: float,
) -> str:
    """The opener: a window, its trailing baseline, and the two blended rates.

    Deliberately thin. The detector job is to say *where* to look; handing the
    agent more than that would be handing it the answer, and handing it raw rows
    would be the anti-pattern BUILD-PLAN Day 4 names explicitly -- the agent
    queries, it is not given the data, which is what stops it inventing entities
    it never counted.
    """
    return (
        "Incident under investigation:\n"
        "- suspect window: days %d to %d\n"
        "- trailing baseline: days %d to %d\n"
        "- blended failure rate: %.1f%% in the baseline, %.1f%% in the window "
        "(%+.1fpp)\n"
        "- nothing else is known. Everything else you have to establish."
        % (
            min(window), max(window),
            min(baseline), max(baseline),
            100.0 * blended_baseline,
            100.0 * blended_window,
            100.0 * (blended_window - blended_baseline),
        )
    )


def render_transcript(entries: Sequence[Tuple[str, str, str, str]]) -> str:
    """The tool log so far, as the model sees it.

    ``entries`` are ``(call_id, tool, args_summary, rendered)`` tuples. The
    call_id is shown because the model has to cite it; the *hash* is not, and
    that is a decision with a reason -- see the ``receipts`` module docstring. A
    64-character digest is precisely the high-cardinality string the canonicality
    screen refuses, and a hash the model transcribes proves it read the output
    rather than binding the claim to it. The harness stamps the digest from its
    own log instead.
    """
    if not entries:
        return (
            "\nNo tool calls yet. A sensible first move is to establish whether the\n"
            "rise is a rate shift or a mix shift.\n"
        )
    blocks = ["\nTool calls so far. Cite these call_ids in your claims.\n"]
    for call_id, tool, args_summary, rendered in entries:
        blocks.append("[%s] %s(%s)\n%s\n" % (call_id, tool, args_summary, rendered))
    return "\n".join(blocks)


def build_investigator_turn_prompt(
    incident: str,
    transcript: str,
    allowed_classes: Sequence[str],
    turn: int,
    max_turns: int,
) -> str:
    """One turn of the investigator loop.

    The final turn carries an explicit instruction to conclude. Without it a
    model will happily spend its last turn on another query and return no
    diagnosis at all, which costs the whole session tokens for nothing -- a
    failure mode worth one line of prompt rather than a retry.
    """
    pressure = ""
    if turn >= max_turns:
        pressure = (
            " This is your LAST turn: return a diagnosis now, using only the"
            " evidence you already have. If it is thin, say so in the summary and"
            " set a low confidence rather than inventing support."
        )
    elif turn == max_turns - 1:
        pressure = " One turn remains after this one."
    prompt = INVESTIGATOR_TURN_INSTRUCTIONS % {
        "tools": TOOL_CATALOGUE,
        "classes": "\n".join("- %s" % c for c in allowed_classes),
        "incident": incident,
        "transcript": transcript,
        "turn": turn,
        "max_turns": max_turns,
        "pressure": pressure,
    }
    return assert_no_identifiers(prompt, context="investigator turn prompt")


def signature_of(features: Dict[str, Any]) -> str:
    """Convenience re-export, so callers need only this module."""
    return planner_signature(features)


def distinct_signatures(feature_dicts: Sequence[Dict[str, Any]]) -> List[str]:
    """Distinct signatures, in first-seen order.

    Order is stable rather than sorted so that a run's LLM calls happen in the
    order the situations were encountered, which makes a partial run's cache a
    prefix of a full run's.
    """
    seen: Dict[str, None] = {}
    for features in feature_dicts:
        seen.setdefault(planner_signature(features), None)
    return list(seen)
