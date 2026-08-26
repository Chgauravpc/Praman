"""The legal-window matrix: ``(legal_context x channel x hour)``, as a table.

This file is meant to be **read**, not traced. A reviewer with the regulation
open in another window should be able to check every cell in one pass, which is
why the windows are a literal dict and an hour-by-hour grid rather than a chain
of ``if`` statements. Branching logic that computes the same answer is worth
less here, because the artifact under review is the *correspondence between the
table and the regulation*, and you cannot eyeball that against control flow.

The three clocks (PRD 7)
------------------------

Three regulators impose three different windows, and the most valuable hour of
the day falls outside two of them::

      08:00   09:00                    19:00   21:00   22:00        08:00
        |       |                        |       |       |            |
  R9  --+-------+------------------------+       |       |            |
        | DEBT COLLECTION 08:00-19:00    X       |       |            |
  R5/R8 |       +--------------------------------+       |            |
        |       | PROMOTIONAL / VOICE 09:00-21:00        |            |
  svc   +-------+--------------------------------+-------+------------+
        | SERVICE / TRANSACTIONAL -- no time restriction              |
  fail  |       |                        +---------------+            |
  peak  |       |                        | 19:00-22:00 most money at risk
                                           collection window ALREADY SHUT

The consequence the envelope has to encode: the same message is lawful at 18:55
and unlawful at 19:05 when ``legal_context = collection``, and the payment-failure
peak sits entirely outside the collection window.

Sources, and their grades
-------------------------

**R9 -- debt collection, 08:00-19:00. [A], verified against the primary
instrument.** RBI/2022-23/108, ``DOR.ORG.REC.65/21.04.158/2022-23``, dated
12 August 2022, *Outsourcing of Financial Services -- Responsibilities of
regulated entities employing Recovery Agents*. The operative words are a
prohibition on "persistently calling the borrower and/ or calling the borrower
**before 8:00 a.m. and after 7:00 p.m.** for recovery of overdue loans."
Day 1 recorded this as [B] from secondary commentary and flagged it as the
weakest of the eleven rules; it was checked against the RBI notification on
Day 2 and the figure held.

**R5 / R8 -- promotional messaging and commercial voice, 09:00-21:00. [B],
still.** TRAI's TCCCPR framework. The 21:00-09:00 quiet period traces to the
Telecom Commercial Communications Customer Preference Regulations **2010** and is
carried through the 2018 regulation's preference schedule; every source found for
the flat "09:00-21:00" figure is secondary. It is consistent across those
sources and is the number the industry operates to, but it has **not** been
matched to a clause in a primary TRAI instrument, so it stays [B] and this
docstring says so. See ``FAILURES.md``, Day 2.

**Service / transactional messages -- no time restriction. [B].** The distinction
is real and load-bearing (it is what buys access to the evening failure peak),
but *which* side of it a payment-retry link falls on is a legal judgment and not
an engineering one. PRD 7 is explicit: do not assume it, register the template in
the correct DLT category, and flag it for review. The matrix below encodes the
service classification because that is the design under test; it is the single
biggest compliance assumption in the system, and it is stated rather than buried.

The interval convention, which is where this kind of table usually goes wrong
-----------------------------------------------------------------------------

Windows are **closed intervals** on both ends, at second precision.

RBI prohibits contact "before 8:00 a.m. and after 7:00 p.m." Read literally:
08:00:00 is not "before 8:00 a.m." so it is permitted, and 19:00:00 is not
"after 7:00 p.m." so it is permitted too -- but 19:00:01 is. So the permitted
set is ``[08:00:00, 19:00:00]`` inclusive, and the first prohibited instant is
one second past seven.

That is a one-second distinction and it is worth the two lines it costs. A
half-open ``[08:00, 19:00)`` implementation would refuse a lawful contact at
exactly 19:00:00, and an implementation that rounded to the hour would permit an
unlawful one at 19:59. Both are the same class of bug -- a boundary asserted
rather than read -- and it is the class this file exists to make visible.

Hours, not buckets
------------------

Every function here takes a **timestamp** and works in seconds. The three-value
``hour_bucket`` from ``canonical.py`` is a *cache key*, not a legal input:
08:00-09:00 is a fourth legal state (R9 open, R5/R8 shut) that three buckets
cannot represent, so a gate built on the bucket would permit a voice call at
08:30 that R8 forbids. The gate is built on the timestamp instead, and
``tests/test_envelope_matrix.py`` pins the case where the two disagree.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from pramaan.canonical import IST, LEGAL_CONTEXTS, parse_iso
from pramaan.envelope.context import Ruling, passes, refuses

# -- channels ---------------------------------------------------------------
#
# Mirrors schemas.Channel. Duplicated as a tuple rather than imported because
# schemas.py imports pydantic, and the envelope is the component that has to be
# readable and importable on its own -- see the no-LLM-imports test.

CHANNELS: Tuple[str, ...] = ("none", "sms", "whatsapp", "email", "in_app", "voice")

#: Channels that put something in front of a human being. ``none`` is the silent
#: action -- a retry, a routing change, a wait -- and no time band reaches it,
#: which is the whole reason the evening peak is workable at all.
CONTACT_CHANNELS: Tuple[str, ...] = ("sms", "whatsapp", "email", "in_app", "voice")

HOUR = 3600


def _h(hour: int, minute: int = 0) -> int:
    return hour * HOUR + minute * 60


# -- the windows, as data ---------------------------------------------------


@dataclass(frozen=True)
class Bound:
    """One rule's time band. ``None`` at either end means unbounded there."""

    rule_id: str
    opens_at: Optional[int]   # seconds since IST midnight, inclusive
    closes_at: Optional[int]  # seconds since IST midnight, inclusive
    basis: str                # the citation, verbatim enough to check
    #: Is citing this rule informative when the step is permitted?
    #:
    #: There are two kinds of unrestricted cell, and flattening them loses the
    #: difference. ``R5_SERVICE_EXEMPT`` is "R5 was considered and does not reach
    #: a service message" -- worth citing on an allow, because it records that
    #: the classification question was asked. ``NO_BOUND`` is "no rule of any
    #: kind governs a silent action" -- citing it on an allow says nothing, and
    #: worse, it crowds out a rule that does.
    #:
    #: This flag exists because the first version did flatten them, and P0 then
    #: won the citation on every ``ACT_ESCALATE_HUMAN`` in the demo -- burying
    #: R3 under "no time band applies".
    informative_when_open: bool = True

    def contains(self, sec: int) -> bool:
        if self.opens_at is not None and sec < self.opens_at:
            return False
        if self.closes_at is not None and sec > self.closes_at:
            return False
        return True

    @property
    def unrestricted(self) -> bool:
        return self.opens_at is None and self.closes_at is None


#: R9. Verified [A] -- RBI/2022-23/108, 12 Aug 2022. Covers *every* form of
#: contact for recovery of an overdue amount: calls, SMS, WhatsApp, email.
R9_COLLECTION = Bound(
    "R9",
    _h(8),
    _h(19),
    "RBI/2022-23/108 DOR.ORG.REC.65/21.04.158/2022-23, 12 Aug 2022: agents "
    "shall not call the borrower before 8:00 a.m. and after 7:00 p.m. [A]",
)

#: R8. Commercial voice. [B] -- TRAI TCCCPR, via secondary sources only.
R8_VOICE = Bound(
    "R8",
    _h(9),
    _h(21),
    "TRAI TCCCPR: outbound commercial voice calls 09:00-21:00 [B]",
)

#: R5. Promotional SMS on a DLT-registered header. [B], same caveat as R8.
R5_PROMOTIONAL_SMS = Bound(
    "R5",
    _h(9),
    _h(21),
    "TRAI TCCCPR: promotional commercial SMS 09:00-21:00 [B]",
)

#: R5 considered and *not* binding. A service-classified transactional message
#: carries no statutory time band, so this Bound is unrestricted -- but it still
#: carries R5's id, because "R5 was considered and does not reach this message"
#: is a materially different ledger entry from "no rule was considered".
R5_SERVICE_EXEMPT = Bound(
    "R5",
    None,
    None,
    "TRAI TCCCPR's time band binds promotional traffic; a service / "
    "transactional message carries no time restriction [B]. The classification "
    "itself is a legal judgment -- see this module's docstring",
)

#: P1. House policy, no regulation behind it. TRAI regulates telecom, so it does
#: not reach email or in-app push; DPDPA governs consent there, not timing.
#: Sending a promotional email at 03:00 is nonetheless bad practice, so the same
#: band is applied -- under a P id, so that nobody mistakes it for law.
P1_PROMOTIONAL_NON_TELECOM = Bound(
    "P1",
    _h(9),
    _h(21),
    "house policy: TRAI's band does not reach email or in-app, but promotional "
    "traffic is held to the same hours anyway",
)

#: No rule reaches a silent action. Retries, routing changes and waits are not
#: communications, and this is the cell that makes the 19:00-22:00 failure peak
#: recoverable at all (PRD 7: after 21:00 only silent recovery remains).
NO_BOUND = Bound(
    "P0",
    None,
    None,
    "a silent action is not a communication; no time band applies",
    informative_when_open=False,
)


@dataclass(frozen=True)
class Window:
    """The bounds that apply to one ``(legal_context, channel)`` cell.

    A tuple, not a single band, because the interesting cells are
    **intersections**. Collection voice is bound by R9 *and* R8, and the whole
    point of keeping them separate is that the envelope can then name which of
    the two shut the door: refused at 08:30 it cites R8, refused at 19:30 it
    cites R9. Collapsing the intersection into one 09:00-19:00 constant would
    lose the attribution, and the attribution is the product.
    """

    bounds: Tuple[Bound, ...]

    def is_open(self, sec: int) -> bool:
        return all(b.contains(sec) for b in self.bounds)

    def binding(self, sec: int) -> Bound:
        """The bound that decides this instant.

        Shut: the first bound that excludes it. Open: the bound that closes
        soonest, so an 18:55 collection contact is allowed *citing R9* -- the
        rule that is about to bind -- rather than citing whichever rule happened
        to be listed first.
        """
        for bound in self.bounds:
            if not bound.contains(sec):
                return bound
        closing = [b for b in self.bounds if b.closes_at is not None]
        if closing:
            return min(closing, key=lambda b: b.closes_at)
        return self.bounds[0]

    def evaluate(self, sec: int) -> Ruling:
        bound = self.binding(sec)
        if self.is_open(sec):
            if bound.unrestricted:
                return passes(
                    bound.rule_id,
                    "no time band binds: %s" % bound.basis,
                    bound=bound.informative_when_open,
                )
            return passes(
                bound.rule_id,
                "%s is inside %s-%s -- %s"
                % (_clock(sec), _clock(bound.opens_at), _clock(bound.closes_at), bound.basis),
            )
        return refuses(
            bound.rule_id,
            "%s is outside %s-%s -- %s"
            % (_clock(sec), _clock(bound.opens_at), _clock(bound.closes_at), bound.basis),
        )

    def reopens_in(self, sec: int) -> Optional[int]:
        """Seconds until every bound is open again, or ``None`` if never.

        Advisory only. PRD 7's rule is that contact actions queue for the
        morning -- 09:00 for promotional, 08:00 for collection -- and this is
        the number that says how long that wait is. It is deliberately *not* a
        permission: the deferred step gets judged again on its own merits.
        """
        if self.is_open(sec):
            return 0
        opens = [b.opens_at for b in self.bounds if b.opens_at is not None]
        if not opens:
            return None
        target = max(opens)
        if sec < target:
            return target - sec
        return 24 * HOUR - sec + target


UNRESTRICTED = Window((NO_BOUND,))


# ===========================================================================
# THE MATRIX -- (legal_context x channel) -> Window
# ===========================================================================
#
# Eighteen cells: three legal contexts x six channels. Read it against the
# docstring above.
#
# The one cell worth arguing about is ``("service", "voice")``. R8 restricts
# *commercial* voice calls, and whether a recovery call about a payment the
# customer was already trying to make is "commercial" is exactly the open
# classification question. The conservative reading is applied -- R8 binds every
# outbound voice call in this system regardless of context -- because the cost of
# being wrong in the other direction is a TRAI penalty reported up to Rs 10 lakh
# against the merchant, and the cost of being wrong in this direction is losing
# two hours of calling time.

LEGAL_WINDOWS: Dict[Tuple[str, str], Window] = {
    # -- service: a transactional message about money the customer was already
    #    trying to move. No statutory time band on messaging; voice still
    #    conservatively bound by R8.
    ("service", "none"):          UNRESTRICTED,
    ("service", "sms"):           Window((R5_SERVICE_EXEMPT,)),
    ("service", "whatsapp"):      Window((R5_SERVICE_EXEMPT,)),
    ("service", "email"):         Window((R5_SERVICE_EXEMPT,)),
    ("service", "in_app"):        Window((R5_SERVICE_EXEMPT,)),
    ("service", "voice"):         Window((R8_VOICE,)),

    # -- collection: recovery of an overdue amount. R9 reaches every channel,
    #    and voice is the intersection R9 n R8 = 09:00-19:00 (PRD 7).
    ("collection", "none"):       UNRESTRICTED,
    ("collection", "sms"):        Window((R9_COLLECTION,)),
    ("collection", "whatsapp"):   Window((R9_COLLECTION,)),
    ("collection", "email"):      Window((R9_COLLECTION,)),
    ("collection", "in_app"):     Window((R9_COLLECTION,)),
    ("collection", "voice"):      Window((R9_COLLECTION, R8_VOICE)),

    # -- promotional: an upsell or a win-back. R5 on SMS, R8 on voice, and the
    #    same hours held as house policy on the channels TRAI does not reach.
    ("promotional", "none"):      UNRESTRICTED,
    ("promotional", "sms"):       Window((R5_PROMOTIONAL_SMS,)),
    ("promotional", "whatsapp"):  Window((R5_PROMOTIONAL_SMS,)),
    ("promotional", "email"):     Window((P1_PROMOTIONAL_NON_TELECOM,)),
    ("promotional", "in_app"):    Window((P1_PROMOTIONAL_NON_TELECOM,)),
    ("promotional", "voice"):     Window((R8_VOICE,)),
}


# ===========================================================================
# THE SAME TABLE AT HOUR GRANULARITY -- hand-written, and cross-checked
# ===========================================================================
#
# 24 characters per row, one per IST hour. '#' means the whole hour is inside
# the permitted window; '.' means some or all of it is not.
#
# This is redundant with LEGAL_WINDOWS on purpose. It is written by hand from
# the regulation, and ``_self_check`` asserts that it agrees with the windows
# above at every hour of every cell. So the two representations verify each
# other: a typo in one is caught at import by the other, and a reviewer gets a
# picture of the day rather than a list of boundary constants.
#
#                                    hour 0         6         12        18    23
#                                         |         |         |         |     |
HOURLY_GRID: Dict[Tuple[str, str], str] = {
    ("service", "none"):          "########################",
    ("service", "sms"):           "########################",
    ("service", "whatsapp"):      "########################",
    ("service", "email"):         "########################",
    ("service", "in_app"):        "########################",
    ("service", "voice"):         ".........############...",

    ("collection", "none"):       "########################",
    ("collection", "sms"):        "........###########.....",
    ("collection", "whatsapp"):   "........###########.....",
    ("collection", "email"):      "........###########.....",
    ("collection", "in_app"):     "........###########.....",
    ("collection", "voice"):      ".........##########.....",

    ("promotional", "none"):      "########################",
    ("promotional", "sms"):       ".........############...",
    ("promotional", "whatsapp"):  ".........############...",
    ("promotional", "email"):     ".........############...",
    ("promotional", "in_app"):    ".........############...",
    ("promotional", "voice"):     ".........############...",
}


# -- time helpers -----------------------------------------------------------


def seconds_since_midnight_ist(at: str) -> int:
    """Seconds past midnight IST for an ISO-8601 timestamp with an offset.

    IST because every window in this file is stated in IST. Bucketing a UTC
    hour would put every boundary 5h30m away from the rule it represents, which
    is a five-and-a-half-hour compliance error that no test on the *shape* of
    the code would find.
    """
    dt = parse_iso(at).astimezone(IST)
    return dt.hour * HOUR + dt.minute * 60 + dt.second


def _clock(sec: Optional[int]) -> str:
    if sec is None:
        return "--:--"
    sec %= 24 * HOUR
    return "%02d:%02d:%02d" % (sec // HOUR, (sec % HOUR) // 60, sec % 60)


def window_for(legal_context: str, channel: str) -> Window:
    """The window governing one cell. Raises rather than defaulting.

    A missing cell is a hole in the matrix, and defaulting a hole to
    ``UNRESTRICTED`` would silently permit contact in a context nobody had
    thought about. Defaulting it to *closed* would be safe but would hide the
    hole. Raising surfaces it at the first call.
    """
    try:
        return LEGAL_WINDOWS[(legal_context, channel)]
    except KeyError:
        raise KeyError(
            "no window defined for legal_context=%r channel=%r -- every cell of "
            "the matrix must be stated explicitly" % (legal_context, channel)
        ) from None


def check_window(legal_context: str, channel: str, at: str) -> Ruling:
    """The gate. Second precision, on the real timestamp."""
    return window_for(legal_context, channel).evaluate(seconds_since_midnight_ist(at))


def reopens_in_seconds(legal_context: str, channel: str, at: str) -> Optional[int]:
    return window_for(legal_context, channel).reopens_in(
        seconds_since_midnight_ist(at)
    )


# -- the coarse signature feature -------------------------------------------
#
# ``canonical.channel_eligibility`` delegates here, so the derivation lives
# beside the rules it cites (STATE.md, Day 1 -> Day 2 handover). What it is NOT
# is a gate. It is one of the seven frozen signature fields (F7): a
# low-cardinality hint that goes into a cache key, computed from a three-value
# hour bucket that provably cannot represent the 08:00-09:00 legal state.
#
# So it is allowed to be wrong, in one direction. Two of its answers are house
# conservatism rather than regulation, and both are labelled:
#
#   P5  a promotional touch never earns a phone call in this system. A product
#       decision -- R8 would permit the call.
#   P6  the coarse feature offers no voice in the 19:00-21:00 evening peak,
#       although R8 permits it for a service context. Retained from Day 1
#       because it is *conservative* and because changing a frozen signature
#       field's output churns the cache for no gain: the envelope re-derives
#       from the real timestamp and is the authority either way.

#: P5. Product decision, not regulation.
PROMOTIONAL_NEVER_EARNS_VOICE = True

#: P6. Day 1 conservatism, retained. See above.
COARSE_FEATURE_WITHHOLDS_EVENING_VOICE = True


def channel_eligibility_for_bucket(
    reason_class: str, legal_context: str, hour_bucket_value: str
) -> str:
    """Coarse contact permission for the planner signature. Not a gate.

    Deliberately conservative, and deliberately unchanged from Day 1: the
    values feed a cache key, and the envelope's minute-precise judgement
    overrules this in every case where the two can disagree.
    """
    from pramaan.taxonomy import NO_CONTACT_CLASSES  # local: avoids a cycle

    if reason_class in NO_CONTACT_CLASSES:
        return "silent_only"
    if hour_bucket_value == "night":
        return "silent_only"
    if hour_bucket_value == "evening_peak":
        # R9 shuts collection at 19:00 while R5/R8 run to 21:00.
        if legal_context == "collection":
            return "silent_only"
        return "silent_and_message"  # P6
    if legal_context == "promotional":
        return "silent_and_message"  # P5
    return "full"


# -- self-check -------------------------------------------------------------


def _self_check() -> None:
    """Cross-verify the two representations, and the shape of the matrix.

    Runs at import. Three failures it is designed to catch, all of which are
    the kind a reader skims past:

    1. a cell missing from either table,
    2. a grid row that disagrees with its window -- i.e. a hand-typed hour
       table that drifted from the boundary constants, or vice versa,
    3. a grid row that is not 24 characters, which is how an off-by-one hour
       gets in.
    """
    expected_cells = {(lc, ch) for lc in LEGAL_CONTEXTS for ch in CHANNELS}
    for name, table in (("LEGAL_WINDOWS", LEGAL_WINDOWS), ("HOURLY_GRID", HOURLY_GRID)):
        missing = expected_cells - set(table)
        extra = set(table) - expected_cells
        if missing or extra:
            raise AssertionError(
                "%s must cover exactly %d cells; missing=%r extra=%r"
                % (name, len(expected_cells), sorted(missing), sorted(extra))
            )

    for cell, row in HOURLY_GRID.items():
        if len(row) != 24:
            raise AssertionError(
                "HOURLY_GRID%r has %d characters, needs 24 (one per IST hour)"
                % (cell, len(row))
            )
        window = LEGAL_WINDOWS[cell]
        for hour, mark in enumerate(row):
            if mark not in "#.":
                raise AssertionError(
                    "HOURLY_GRID%r hour %d is %r; use '#' or '.'" % (cell, hour, mark)
                )
            # "the whole hour is permitted" == open at its first and last second
            whole_hour_open = window.is_open(_h(hour)) and window.is_open(
                _h(hour) + HOUR - 1
            )
            if (mark == "#") != whole_hour_open:
                raise AssertionError(
                    "HOURLY_GRID%r hour %02d says %r but LEGAL_WINDOWS says %s"
                    % (cell, hour, mark, "open" if whole_hour_open else "shut")
                )

    # The boundary the whole file exists for. Stated as an assertion so it is
    # checked at import and not only in the test suite.
    collection_sms = LEGAL_WINDOWS[("collection", "sms")]
    if not collection_sms.is_open(_h(19)):
        raise AssertionError("19:00:00 exactly must be permitted under R9")
    if collection_sms.is_open(_h(19) + 1):
        raise AssertionError("19:00:01 must not be permitted under R9")
    if collection_sms.is_open(_h(8) - 1):
        raise AssertionError("07:59:59 must not be permitted under R9")
    if not collection_sms.is_open(_h(8)):
        raise AssertionError("08:00:00 exactly must be permitted under R9")


_self_check()
