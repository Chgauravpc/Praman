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
        re.compile(r"\b(?:pay|order|sub|plan|inv|cust|txn|rzp|evt|cp|run)_[A-Za-z0-9]{3,}"),
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
