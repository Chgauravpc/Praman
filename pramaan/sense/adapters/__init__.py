"""The four adapters Day 6 adds -- checkout, subscription, mandate, receivable.

Thin normalisers, on purpose (BUILD-PLAN Day 6 block B): each one turns a raw,
adapter-specific shape into a ``RiskEvent``, the same way the payment adapter
embedded in ``sim/generate.py`` already does. None of them touches the loop
downstream of ``RiskEvent`` -- envelope, planner, executor, eval -- because none
of it needs to know. That is the payoff of PRD 3.1's structural insight: four
adapters, one loop.

What is shared across all four lives here rather than being copied four times:
the contact-eligible action set, derived from the same
``pramaan.taxonomy.REASON_CLASS_POLICY`` the payment adapter reads. What is
*not* shared -- ``cause_signal`` vocabulary, ``decay_profile``, ``legal_context``,
whether ``ACT_RETRY`` is even on the table -- is each adapter's own, because that
is precisely the part PRD 3.1 says is not generic.
"""
from __future__ import annotations

from typing import List, Tuple

from pramaan.taxonomy import REASON_CLASS_POLICY


def contact_capable_actions(
    reason_class: str, eligibility: str, amount_band: int
) -> List[str]:
    """The contact actions this reason class and window would permit.

    Mirrors ``sim.generate._available_actions``'s own contact branch exactly,
    so a checkout/subscription/mandate/receivable event's contact options are
    computed by the identical rule a payment event's are -- one source of
    truth for "is a message defensible here", not four adapters each guessing.
    """
    policy = REASON_CLASS_POLICY[reason_class]
    actions: List[str] = []
    if policy.contact == "allowed" and eligibility in ("silent_and_message", "full"):
        actions.append("ACT_MESSAGE")
        # T3's value threshold, same as the payment adapter's: a call is the
        # least reversible contact there is, offered only above band 3.
        if eligibility == "full" and amount_band >= 3:
            actions.append("ACT_VOICE")
    return actions


def base_actions(
    reason_class: str,
    eligibility: str,
    amount_band: int,
    *,
    retry: bool = False,
    stop: bool = False,
) -> Tuple[str, ...]:
    """``ACT_WAIT`` plus whatever this adapter and class jointly permit.

    ``retry``/``stop`` are the adapter's own decision, not derived from the
    class policy -- see each adapter module for why (a checkout has nothing
    to retry; a reconciled receivable has nothing left to say but stop).
    """
    actions: List[str] = ["ACT_WAIT"]
    if retry:
        actions.append("ACT_RETRY")
    actions.extend(contact_capable_actions(reason_class, eligibility, amount_band))
    if stop:
        actions.append("ACT_STOP")
    return tuple(actions)
