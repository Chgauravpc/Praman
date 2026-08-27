"""Every rule id the envelope can emit, derived rather than listed.

This module exists because of a mistake made twice in one day, and the second
time immediately after writing the fix for the first.

The first: a docstring claimed "two of eleven rules are [A]-verified" while the
dict beside it graded one. The guard that was supposed to prevent exactly that
drift protected the data structure and had nothing to say about the prose.

The second: the fix for the first included a demo line reading ``rule ids
implemented 25``, typed by hand. It is 30. A hand-typed count of a thing the
program can count is a claim waiting to go stale, and it went stale inside an
hour.

So: nothing here is written by hand except the ``P`` list, which has no other
home. Everything else is collected from the modules that own it, and
``tests/test_envelope_matrix.py`` asserts that no judgement anywhere in the
matrix cites an id this registry does not know about -- which is the check that
turns the count from an assertion into a measurement.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Tuple

from pramaan.envelope import reason_map, rules, stopping

#: House-policy ids. The only hand-maintained list in this module, because a
#: policy id is not derivable from anything -- it exists precisely where no rule
#: does. Keep the reasons attached: an unexplained P id is indistinguishable
#: from a regulation somebody forgot to cite.
POLICY_RULES: Dict[str, str] = {
    "P0": "no rule in the envelope constrains this action",
    "P1": "promotional traffic on channels TRAI does not reach (email, in-app) "
          "is held to the same 09:00-21:00 hours anyway",
    "P2": "a voice call below Rs 500 can cost more than it recovers",
    "P3": "a concession above Rs 5,000 needs human approval",
}


def _guardrail_ids() -> FrozenSet[str]:
    """The G ids, collected from the tables that define them."""
    ids = set()
    for table in (
        reason_map.RETRY_FORBIDDEN_CLASSES,
        reason_map.CONTACT_FORBIDDEN_CLASSES,
        reason_map.CONTACT_WASTE_CLASSES,
        reason_map.SCHEDULED_ONLY_CLASSES,
        reason_map.NOT_RETRYABLE_BY_CLASS,
        reason_map.NOT_RETRYABLE_CODES,
    ):
        ids.update(rule_id for rule_id, _ in table.values())
    return frozenset(ids)


def _stopping_ids() -> FrozenSet[str]:
    return frozenset(stopping.TERMINAL_RULES) | frozenset(stopping.ECONOMIC_RULES)


REGULATORY_RULES: Tuple[str, ...] = tuple(rules.RULE_IDS)
GUARDRAIL_RULES: Tuple[str, ...] = tuple(sorted(_guardrail_ids()))
STOPPING_RULES: Tuple[str, ...] = tuple(sorted(_stopping_ids()))

#: Every id the envelope can put in a ledger row. Derived. Do not hand-edit.
ALL_RULE_IDS: Tuple[str, ...] = tuple(
    sorted(
        set(REGULATORY_RULES)
        | set(GUARDRAIL_RULES)
        | set(STOPPING_RULES)
        | set(POLICY_RULES)
    )
)

#: One line for the demo, so the number on screen is counted and not typed.
def summary() -> str:
    return "%d (%d regulatory R, %d guardrail G, %d stopping S, %d policy P)" % (
        len(ALL_RULE_IDS),
        len(REGULATORY_RULES),
        len(GUARDRAIL_RULES),
        len(STOPPING_RULES),
        len(POLICY_RULES),
    )


def _self_check() -> None:
    if len(GUARDRAIL_RULES) != 8:
        raise AssertionError(
            "the guardrails are G1-G8; collected %r" % (GUARDRAIL_RULES,)
        )
    if len(STOPPING_RULES) != 7:
        raise AssertionError("the stopping rules are S1-S7; collected %r" % (STOPPING_RULES,))
    if len(REGULATORY_RULES) != 11:
        raise AssertionError("the regulatory rules are R1-R11")
    for rule_id in ALL_RULE_IDS:
        if rule_id[0] not in "RGSP":
            raise AssertionError(
                "rule id %r has no namespace prefix; see envelope/context.py"
                % rule_id
            )


_self_check()
