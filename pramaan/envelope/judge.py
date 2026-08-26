"""``judge(step, context) -> Judgement``. Deterministic, and it names the rule.

This is the component the whole build order is arranged around (F11: envelope
before LLM). Everything the planner proposes passes through here, and nothing
downstream may act on a step this function has not approved. It is the reason the
LLM layers can be built fast on Days 4-7: the blast radius of a bad plan is
bounded by a file with no model in it.

Three properties, and each one is a design constraint rather than an aspiration.

**It cannot be the thing it is judging.** No import anywhere under
``pramaan/envelope/`` reaches ``pramaan/llm/``, and
``tests/test_redteam_envelope.py`` asserts it. An envelope that consulted a model
to decide whether a model's plan was acceptable would be a more sophisticated
version of asking the model twice.

**It has no clock and no I/O.** Every input arrives in the ``EnvelopeContext``.
So a verdict is a pure function, the same inputs give the same verdict on any
machine on any day, and every branch is reachable from a test -- including the
ones that need a specific second of a specific hour.

**Three verdicts, never two.** ``ALLOW`` / ``AMEND`` / ``REJECT``, always with a
``rule_id``. A boolean gate cannot express the most common planner error, which
is not "this action is forbidden" but "this action is right and the timing is
wrong", and it has nowhere to put the citation.

Evaluation order, and why it is the interesting part
----------------------------------------------------

Many rules can refuse the same step. Which one gets cited is what appears in the
ledger, in the compliance report, and in the sentence a reviewer reads -- so the
order is chosen to make the citation *the most informative true statement
available*, not to make the code shortest.

1. **Terminal stopping rules** -- S1, S7, S4. These end the thread. "This person
   has already paid" or "this person has disclosed hardship" is more useful than
   any downstream finding, and none of the downstream findings would change the
   answer.
2. **R3, attributability.** Before anything else is assessed, the action has to
   be answerable-for. An unattributable action is a hole in the audit trail
   whether or not it is otherwise lawful.
3. **Taxonomy guardrails** -- G1-G8. Futility before legality. Telling a planner
   that a ``card_expired`` retry is outside the collection window would be true
   and useless; it can never succeed at any hour.
4. **Regulatory rules** -- R11, then the mandate group, then the channel and
   conduct group, then the window. Consent first because it is the broadest.
5. **Tier gates** -- P2's value floor, P3's approval ceiling. House policy comes
   after regulation, so a step that is both unlawful and uneconomic cites the
   law.
6. **Economic stopping rules** -- S3, S2, S5, S6. Last, for the same reason: if a
   step is illegal, "we were also over budget" is the weaker sentence.

Then, and only if something refused, the amendment path.

Why AMEND is implemented rather than deferred
---------------------------------------------

BUILD-PLAN Day 5 permits shipping a binary envelope and merely logging what
would have been amended. It is implemented here instead, for two reasons and one
of them is not tidiness:

- The two amendable cases are the two most common planner errors, and both are
  *arithmetic* rather than judgment -- an immediate retry that should be
  scheduled, and a voice call that should be a message. Refusing them outright
  discards a plan that was substantively right.
- An amendment is only offered if the amended step **passes the full envelope on
  its own**. It is re-judged, once, from the top. So AMEND cannot become a hole:
  the envelope never proposes something it would not have allowed.

A deferral is *not* an amendment. A collection contact at 19:05 is ``REJECT``
citing R9, and the judgement carries ``reopens_in_seconds`` so the caller can
queue it for 08:00 -- but the queued step gets judged again on its own merits
when it fires. Turning "you may not do this now" into "you may do this later"
inside the envelope would mean the envelope had approved an action at a time it
never evaluated.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from pramaan.canonical import parse_iso, to_iso
from pramaan.envelope import reason_map, rules, stopping, tiers
from pramaan.envelope.context import (
    ALLOW,
    AMEND,
    NO_RULE_BINDS,
    REJECT,
    EnvelopeContext,
    Judgement,
    Ruling,
)


@dataclass(frozen=True)
class Step:
    """The envelope's own minimal view of a proposed action.

    Structurally compatible with ``schemas.PlanStep`` and deliberately not the
    same class. The envelope accepts anything carrying these five attributes,
    which keeps this package importable with nothing but the standard library
    plus ``canonical`` and ``taxonomy`` -- no pydantic, and provably no model.
    The point is not dependency hygiene for its own sake: it is that the
    component asserting "no LLM in here" should be checkable by reading its
    imports.
    """

    action: str
    channel: str = "none"
    delay_seconds: int = 0
    expected_value_paise: int = 0
    cost_paise: int = 0


def as_step(plan_step: Any) -> Step:
    """Accept a ``PlanStep``, a ``Step``, or anything with the five attributes."""
    if isinstance(plan_step, Step):
        return plan_step
    return Step(
        action=plan_step.action,
        channel=getattr(plan_step, "channel", "none"),
        delay_seconds=int(getattr(plan_step, "delay_seconds", 0)),
        expected_value_paise=int(getattr(plan_step, "expected_value_paise", 0)),
        cost_paise=int(getattr(plan_step, "cost_paise", 0)),
    )


def effective_context(step: Step, context: EnvelopeContext) -> EnvelopeContext:
    """The context as it will be when the step actually fires.

    ``delay_seconds`` is a relative offset (schemas.py), so a step proposed at
    18:00 with a 4,000-second delay is an action *at 19:07*. Judging it at 18:00
    would approve a collection contact that lands outside R9's window -- which is
    exactly PRD 7's warning against front-loading nothing and queueing
    receivables work into the evening. The window is evaluated where the action
    lands, not where it was decided.
    """
    if step.delay_seconds == 0:
        return context
    fires_at = parse_iso(context.at) + timedelta(seconds=step.delay_seconds)
    return replace(context, at=to_iso(fires_at))


def _collect(step: Step, context: EnvelopeContext) -> List[Tuple[str, Ruling]]:
    """Every ruling, in evaluation order, labelled by where it came from.

    The label exists because several rules share a ``rule_id`` -- R5 is both a
    template requirement and a time band, R8 is both a call cap and a time band
    -- and the amendment path needs to know *which* R8 refused, while the ledger
    only needs to know that R8 did.
    """
    at = context  # already the effective context; named for readability
    action = step.action

    checks: List[Tuple[str, Ruling]] = [
        # 1. terminal stopping rules
        ("s1_already_paid", stopping.s1_already_paid(action, at)),
        ("s7_distress", stopping.s7_distress_or_dispute(action, at)),
        ("s4_consent", stopping.s4_consent_withdrawn(action, at)),
        # 2. attributability
        ("r3_attribution", rules.r3_acquirer_attribution(action, at)),
        # 3. taxonomy guardrails: futility before legality
        ("g_futility", reason_map.check_retry_futility(at.reason_code, action)),
        ("g_contact", reason_map.check_contact(at.reason_code, action)),
        # 4. regulation -- consent, then mandate mechanics
        ("r11_consent", rules.r11_consent(action, at)),
        ("r1_notification", rules.r1_pre_debit_notification(action, at)),
        ("r2_afa", rules.r2_additional_factor_auth(action, at)),
        ("r4_opt_out", rules.r4_customer_opt_out(action, at)),
        ("r6_retry_cap", rules.r6_retry_cap(action, at)),
        ("r7_exemption", rules.r7_replenishment_exemption(action, at)),
        # 4b. channel and conduct
        ("r5_dnd", rules.r5_dlt_template(action, at)),
        ("r5_template", rules.r5_sms_template(action, step.channel, at)),
        ("r8_conduct", rules.r8_voice_conduct(action, at)),
        ("r9_conduct", rules.r9_collection_conduct(action, at)),
        ("r10_disclosure", rules.r10_ai_disclosure(action, at)),
        # 4c. the window, last of the regulatory group
        ("window", rules.check_legal_window(action, step.channel, at)),
        # 4d. G7, the one guardrail that is a *timing* rule rather than a
        # futility rule, so it sits after the regulation whose floors outrank
        # its own. See reason_map.check_retry_futility for why.
        (
            "g_timing",
            reason_map.check_retry_timing(at.reason_code, action, step.delay_seconds),
        ),
        # 5. tier gates -- house policy after regulation
        ("p2_value_floor", tiers.check_value_floor(action, at)),
        ("p3_approval", tiers.check_human_approval(action, at)),
        # 6. economic stopping rules
        ("s3_promise", stopping.s3_promise_honoured(action, at)),
        ("s2_budget", stopping.s2_contact_budget(action, at)),
        ("s5_circuit_breaker", stopping.s5_merchant_circuit_breaker(action, at)),
        (
            "s6_diminishing_returns",
            stopping.s6_diminishing_returns(
                action, step.expected_value_paise, step.cost_paise
            ),
        ),
    ]
    return checks


def _decisive(checks: List[Tuple[str, Ruling]]) -> Optional[Tuple[str, Ruling]]:
    for label, ruling in checks:
        if ruling.blocks:
            return label, ruling
    return None


#: Which rule to cite when a step is ALLOWED, most informative first.
#:
#: An allow needs a citation as much as a refusal does -- "permitted, and here is
#: the rule that was closest to forbidding it" is auditable; "permitted" is not.
#: But the refusal order is the wrong order to reuse, because it is arranged by
#: *severity* and an allow wants *specificity*. Every rule in the refusal chain
#: passed, so the interesting one is not the first or the last, it is the tightest.
#:
#: Hence an explicit list. The window leads it: for a permitted contact, the time
#: band is the thing a compliance reader wants to see was checked, and it is what
#: makes "allowed at 18:55 citing R9" and "refused at 19:05 citing R9" a matched
#: pair in the ledger. R3 is last because every action is attributable and saying
#: so carries no information.
#:
#: The first version of this took "the last bound rule", which cited S6 -- "no
#: cost attributed to this step" -- on a lawful evening collection message. True,
#: and the least interesting true thing available.
ALLOW_CITATION_PRIORITY: Tuple[str, ...] = (
    "window",            # the time band -- the tightest constraint on a contact
    "r1_notification",   # the mandate group, most specific first
    "r2_afa",
    "r4_opt_out",
    "r6_retry_cap",
    "r7_exemption",
    "r9_conduct",        # conduct and channel mechanics
    "r10_disclosure",
    "r8_conduct",
    "r5_template",
    "r5_dnd",
    "g_timing",          # scheduling floors
    "g_futility",        # retry-eligibility
    "g_contact",
    "r11_consent",
    "p2_value_floor",    # house policy
    "p3_approval",
    "s2_budget",         # economic limits
    "s3_promise",
    "s5_circuit_breaker",
    "s6_diminishing_returns",
    "s1_already_paid",
    "s4_consent",
    "s7_distress",
    "r3_attribution",    # true of everything, therefore says nothing
)


def _binding_allow(checks: List[Tuple[str, Ruling]]) -> Ruling:
    """The rule to cite on an ALLOW: the tightest constraint that was cleared."""
    bound = {label: ruling for label, ruling in checks if ruling.bound}
    for label in ALLOW_CITATION_PRIORITY:
        if label in bound:
            return bound[label]
    return Ruling(
        rule_id=NO_RULE_BINDS,
        verdict=ALLOW,
        reason="no rule in the envelope constrains this action",
        bound=False,
    )


# -- the amendment path -----------------------------------------------------


def _candidate_amendments(
    step: Step, context: EnvelopeContext, label: str
) -> List[Tuple[Dict[str, Any], str]]:
    """Modifications that might make this exact step acceptable, as it stands.

    Two, and both are arithmetic rather than judgment. Note what is *not* here:

    - **A deferral is not an amendment.** Moving a step out of a closed window
      changes when it happens, and the envelope has not judged it at that time.
      Those get ``REJECT`` plus ``reopens_in_seconds``.
    - **A missing AI disclosure (R10) or a missing DLT template (R5) is not
      amendable.** The envelope would have to author a script or invent a
      registered template. Refusing sends the defect back to the planner, which
      is where it can actually be fixed.
    """
    out: List[Tuple[Dict[str, Any], str]] = []

    # G7: the retry is right, the timing is not. Insufficient funds retried
    # immediately fails for the reason the first attempt did.
    if label == "g_timing" and step.action == "ACT_RETRY":
        delay = reason_map.scheduled_retry_delay(context.reason_code)
        if delay is not None and step.delay_seconds < delay:
            out.append(
                (
                    {"delay_seconds": delay},
                    "defer the retry to %dh, past the credit-cycle / daily-cap "
                    "boundary" % (delay // 3600),
                )
            )

    # P2: the call is not worth its cost at this amount. A message is.
    # R8-window: voice is shut but the messaging window may be open -- this is
    # the 08:00-09:00 case, where R9 permits a collection contact and R8 does
    # not yet permit a call.
    if step.action == "ACT_VOICE" and label in ("p2_value_floor", "window"):
        out.append(
            (
                {"action": "ACT_MESSAGE", "channel": "sms"},
                "downgrade the voice call to a message -- T3 to T2, which is a "
                "less irreversible action on a channel that is open",
            )
        )

    return out


def _apply(step: Step, changes: Dict[str, Any]) -> Step:
    return replace(step, **changes)


# -- the entry point --------------------------------------------------------


def judge(plan_step: Any, context: EnvelopeContext, *, _depth: int = 0) -> Judgement:
    """Judge one proposed step. Always returns a verdict and a rule id.

    ``_depth`` guards the amendment path: an amended step is re-judged exactly
    once and may not itself be amended. Without the guard a pathological rule
    set could amend in a cycle, and an envelope that can loop is an envelope that
    can hang the executor.
    """
    step = as_step(plan_step)
    effective = effective_context(step, context)
    checks = _collect(step, effective)
    rulings = tuple(r for _, r in checks)

    blocked = _decisive(checks)
    if blocked is None:
        allow = _binding_allow(checks)
        return Judgement(
            verdict=ALLOW,
            rule_id=allow.rule_id,
            reason=allow.reason,
            rulings=rulings,
            reopens_in_seconds=0,
        )

    label, ruling = blocked
    terminates = ruling.rule_id in stopping.TERMINAL_RULES

    reopens = None
    if label == "window":
        reopens = rules.window_reopens_in(step.action, step.channel, effective)

    # Try to amend, once. The amended step must clear the whole envelope on its
    # own -- the envelope never offers something it would not have allowed.
    if _depth == 0 and not terminates:
        for changes, why in _candidate_amendments(step, effective, label):
            amended = _apply(step, changes)
            verdict = judge(amended, context, _depth=1)
            if verdict.allowed:
                return Judgement(
                    verdict=AMEND,
                    rule_id=ruling.rule_id,
                    reason="%s -- amended: %s" % (ruling.reason, why),
                    rulings=rulings,
                    amendment=dict(changes),
                    terminates_thread=False,
                    reopens_in_seconds=reopens,
                )

    return Judgement(
        verdict=REJECT,
        rule_id=ruling.rule_id,
        reason=ruling.reason,
        rulings=rulings,
        terminates_thread=terminates,
        reopens_in_seconds=reopens,
    )


def _self_check() -> None:
    """Every label the collector emits must appear in the citation priority list.

    Otherwise a newly added rule can pass, bind, and never be citable on an
    ALLOW -- which is invisible until somebody asks why a permitted action's
    GATE row names a less specific rule than the one that actually constrained
    it.
    """
    labels = [label for label, _ in _collect(Step("ACT_WAIT"), EnvelopeContext())]
    missing = set(labels) - set(ALLOW_CITATION_PRIORITY)
    extra = set(ALLOW_CITATION_PRIORITY) - set(labels)
    if missing or extra:
        raise AssertionError(
            "ALLOW_CITATION_PRIORITY must list exactly the collector's labels; "
            "missing=%r extra=%r" % (sorted(missing), sorted(extra))
        )
    if len(labels) != len(set(labels)):
        raise AssertionError("duplicate check label in _collect")


def judge_plan(steps: Any, context: EnvelopeContext) -> Tuple[Judgement, ...]:
    """Judge every step of a plan, stopping at the first terminal verdict.

    Short-circuiting is the point rather than an optimisation: if S1 fires on
    step one because the customer has already paid, steps two and three are not
    "also rejected", they are *moot*. Recording them as individual rejections
    would inflate the planner violation rate with steps that were never going to
    be reached, and that rate is a published metric (PRD 8).
    """
    out: List[Judgement] = []
    for step in steps:
        judgement = judge(step, context)
        out.append(judgement)
        if judgement.terminates_thread:
            break
    return tuple(out)


_self_check()
