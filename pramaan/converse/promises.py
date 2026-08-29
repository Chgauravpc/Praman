"""The promise-to-pay state machine (PRD 6.8).

``NONE -> PROMISED(amount, date, channel, verbatim)``, then exactly one of
``KEPT`` / ``PARTIAL`` / ``BROKEN``. ``PARTIAL`` gets one re-negotiation;
``BROKEN`` is a single escalation, never a third chase (PRD 6.8, S3's own
docstring in ``pramaan.envelope.stopping``).

Two things this module is honest about rather than silent on.

**The extractor is a heuristic today, not the LLM.** PRD 6.8 calls promise
extraction "genuinely hard in Hinglish" and names the exact trap: "haan haan
kal dekhta hoon" is not a promise, "Friday tak pakka kar dunga" is. Both
carry a day-of-week reference; what separates them is whether a real
commitment verb sits next to it. ``extract_commitment`` below is a
deterministic, pattern-based first pass -- testable today, and it correctly
separates the brief's own two examples (``tests/test_promises.py``).
``extract_commitment_via_llm`` is the LLM path PRD 6.8 actually wants, wired
and tested against a scripted client exactly like the planner's own tests do
(``tests/scripted.ScriptedLLM``); it is not exercised against a live model
this session, because a real customer transcript needs its own date and
amount to be readable, which the canonicality screen exists specifically to
refuse (PRD 9.1). That is why ``LLMClient.call`` gained a single, named,
tested ``screen=False`` escape hatch this day (ADR-038): a per-conversation
call was never going to be memoised, so the screen's own rationale -- protect
a *shared* signature space -- does not reach it. What is deliberately not
built today: a live call against a real transcript, and the reply-first
WhatsApp mechanic that supplies one (PRD 6.7, Day 7).

**Reliability is a Beta posterior, not a raw ratio.** A counterparty's very
first promise cannot be scored 0% or 100% kept -- that is one data point
pretending to be a rate. ``ReliabilityStats.reliability`` uses a
Laplace-smoothed estimate (Beta(1,1) prior) for exactly this reason: it
starts at 0.5 with no history and moves smoothly as evidence arrives,
which is what a *calibration* curve (PRD 6.8's own required metric) needs to
be checking something other than noise on the first few counterparties.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pramaan import canonical
from pramaan.envelope.context import PROMISE_STATES

NONE_STATE, PROMISED, KEPT, PARTIAL, BROKEN = PROMISE_STATES

# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Promise:
    """One counterparty's live (or resolved) commitment."""

    counterparty_id: str
    state: str = NONE_STATE
    amount_paise: Optional[int] = None
    promised_date: Optional[str] = None  # ISO-8601 with offset -- the deadline
    channel: str = "none"
    verbatim: str = ""
    confidence: Optional[float] = None
    #: PARTIAL gets exactly one re-negotiation (PRD 6.8); this is what stops
    #: a second PARTIAL from silently granting a third chase.
    renegotiated: bool = False

    def __post_init__(self) -> None:
        if self.state not in PROMISE_STATES:
            raise ValueError(
                "state must be one of %r, got %r" % (PROMISE_STATES, self.state)
            )


def make_promise(
    counterparty_id: str, extracted: "ExtractedCommitment", *, channel: str
) -> Promise:
    """NONE -> PROMISED, from one extracted commitment. Not a promise -> NONE."""
    if not extracted.is_promise:
        return Promise(counterparty_id=counterparty_id, state=NONE_STATE)
    return Promise(
        counterparty_id=counterparty_id,
        state=PROMISED,
        amount_paise=extracted.amount_paise,
        promised_date=extracted.promised_date,
        channel=channel,
        verbatim=extracted.verbatim,
        confidence=extracted.confidence,
    )


def resolve_promise(
    promise: Promise,
    *,
    paid_amount_paise: int,
    now: str,
) -> Promise:
    """Evaluate a live promise against what actually arrived by ``now``.

    ``PROMISED`` and ``PARTIAL`` are the two live states -- PARTIAL is not
    terminal, it is "one re-negotiation already spent, still waiting on the
    remainder." ``NONE``/``KEPT``/``BROKEN`` are terminal and are returned
    unchanged -- resolving twice must be a no-op, the same discipline the
    envelope holds execute to.
    """
    if promise.state not in (PROMISED, PARTIAL):
        return promise

    owed = promise.amount_paise or 0
    if paid_amount_paise >= owed and owed > 0:
        return replace(promise, state=KEPT)

    date_passed = promise.promised_date is not None and canonical.parse_iso(
        now
    ) > canonical.parse_iso(promise.promised_date)

    if promise.state == PARTIAL:
        # The one re-negotiation this promise gets has already been spent.
        # Anything short of full payment once the (same) date passes again
        # is broken, not a second PARTIAL (PRD 6.8: "never a third chase").
        return replace(promise, state=BROKEN) if date_passed else promise

    # state == PROMISED
    if 0 < paid_amount_paise < owed:
        return replace(promise, state=PARTIAL, renegotiated=True)
    if date_passed:
        return replace(promise, state=BROKEN)
    return promise


# --------------------------------------------------------------------------
# Per-counterparty reliability
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReliabilityStats:
    kept: int
    partial: int
    broken: int

    @property
    def total_resolved(self) -> int:
        return self.kept + self.partial + self.broken

    @property
    def reliability(self) -> float:
        """Laplace-smoothed kept-rate, PARTIAL counted as half a keep.

        Beta(1, 1) prior: with zero history this is 0.5, not 0.0 or 1.0 --
        the honest statement that nothing is known yet, rather than a
        confident-looking number built from one data point.
        """
        successes = self.kept + 0.5 * self.partial
        return (successes + 1.0) / (self.total_resolved + 2.0)


def reliability_for(promises: Sequence[Promise], counterparty_id: str) -> ReliabilityStats:
    kept = partial = broken = 0
    for p in promises:
        if p.counterparty_id != counterparty_id:
            continue
        if p.state == KEPT:
            kept += 1
        elif p.state == PARTIAL:
            partial += 1
        elif p.state == BROKEN:
            broken += 1
    return ReliabilityStats(kept=kept, partial=partial, broken=broken)


# --------------------------------------------------------------------------
# Calibration -- Brier score and a reliability curve (PRD 6.8's own metric)
# --------------------------------------------------------------------------


def brier_score(predictions: Sequence[Tuple[float, bool]]) -> float:
    """Mean squared error between a stated probability and the 0/1 outcome.

    Lower is better; 0.0 is perfect. PRD 6.8: "a well-calibrated 60% model
    beats a badly-calibrated 80% one" -- accuracy is the wrong metric for a
    collections team allocating effort against probabilities, and this is
    the metric that actually penalises overconfidence.
    """
    if not predictions:
        return 0.0
    total = sum((p - (1.0 if outcome else 0.0)) ** 2 for p, outcome in predictions)
    return total / len(predictions)


def reliability_curve(
    predictions: Sequence[Tuple[float, bool]], bins: int = 5
) -> List[Tuple[float, Optional[float], int]]:
    """Bucket by stated probability; report the empirical keep-rate per bucket.

    Returns ``(bin_midpoint, empirical_rate_or_None, n)``. ``None`` marks an
    empty bin rather than a false 0% -- an empty bucket is missing data, not
    evidence the model is wrong there.
    """
    if bins <= 0:
        raise ValueError("bins must be positive")
    width = 1.0 / bins
    buckets: List[List[bool]] = [[] for _ in range(bins)]
    for probability, outcome in predictions:
        index = min(bins - 1, max(0, int(probability / width)))
        buckets[index].append(outcome)
    curve: List[Tuple[float, Optional[float], int]] = []
    for i, outcomes in enumerate(buckets):
        midpoint = (i + 0.5) * width
        rate = (sum(outcomes) / len(outcomes)) if outcomes else None
        curve.append((midpoint, rate, len(outcomes)))
    return curve


# --------------------------------------------------------------------------
# Extraction -- the heuristic first pass
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedCommitment:
    is_promise: bool
    promised_date: Optional[str]
    amount_paise: Optional[int]
    confidence: float
    verbatim: str
    rationale: str


#: A real commitment verb, in Hinglish or English. Deliberately narrow: a
#: false positive here manufactures a promise nobody made, which is worse
#: than missing a real one (a missed promise just means no ladder change;
#: a fabricated one means the ladder pauses contact on a customer who never
#: actually committed -- see S3).
_COMMITMENT_PATTERNS: Tuple[str, ...] = (
    r"\bpakka\b",
    r"\bkar\s*dunga\b",
    r"\bkar\s*doonga\b",
    r"\bkar\s*denge\b",
    r"\bde\s*dunga\b",
    r"\bde\s*doonga\b",
    r"\bbhej\s*dunga\b",
    r"\bpay\s*kar\s*dunga\b",
    r"\bi\s*will\s*pay\b",
    r"\bi['’]?ll\s*pay\b",
    r"\bi\s*promise\b",
    r"\bdefinitely\s*pay\b",
)

#: Non-committal acknowledgement. Present specifically to veto a match that
#: has a date word but no real commitment -- PRD 6.8's own trap case.
_VAGUE_PATTERNS: Tuple[str, ...] = (
    r"\bhaan\s*haan\b",
    r"\bdekhta\s*hoon\b",
    r"\bdekh\s*lenge\b",
    r"\bdekhenge\b",
    r"\bpata\s*nahi\b",
    r"\bshayad\b",
    r"\btry\s*karunga\b",
    r"\bkarunga\s*try\b",
)

#: Relative day words -> offset in days from ``now``. Weekday names resolve to
#: the next occurrence of that weekday (today counts as 7 days out, matching
#: how a customer means "Friday" when today already is Friday morning).
_RELATIVE_DAYS: Dict[str, int] = {"aaj": 0, "today": 0, "kal": 1, "tomorrow": 1, "parso": 2}
_WEEKDAYS: Dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_AMOUNT_PATTERN = re.compile(r"(?:rs\.?|inr|₹)?\s*(\d[\d,]{2,})", re.IGNORECASE)


def _find_date_offset_days(lowered: str) -> Optional[int]:
    for word, offset in _RELATIVE_DAYS.items():
        if re.search(r"\b%s\b" % word, lowered):
            return offset
    for name, weekday in _WEEKDAYS.items():
        if re.search(r"\b%s\b" % name, lowered):
            return weekday  # resolved relative to `now`'s own weekday below
    return None


def _resolve_date(now: str, lowered: str) -> Optional[str]:
    offset = _find_date_offset_days(lowered)
    if offset is None:
        return None
    now_dt = canonical.parse_iso(now)
    if any(re.search(r"\b%s\b" % name, lowered) for name in _WEEKDAYS):
        for name, weekday in _WEEKDAYS.items():
            if re.search(r"\b%s\b" % name, lowered):
                days_ahead = (weekday - now_dt.weekday()) % 7
                days_ahead = days_ahead or 7  # "Friday" said on a Friday means next week
                return canonical.to_iso(now_dt + timedelta(days=days_ahead))
    return canonical.to_iso(now_dt + timedelta(days=offset))


def _find_amount_paise(text: str) -> Optional[int]:
    match = _AMOUNT_PATTERN.search(text)
    if not match:
        return None
    rupees = int(match.group(1).replace(",", ""))
    return rupees * 100


def extract_commitment(text: str, *, now: str) -> ExtractedCommitment:
    """Deterministic Hinglish-aware first pass. See the module docstring.

    A promise needs **both** a real commitment verb and a date reference --
    either alone is exactly the ambiguous case PRD 6.8 names. Confidence is a
    fixed heuristic value today (0.75 for an extracted promise), not a
    calibrated probability; ``extract_commitment_via_llm`` is where a real
    stated probability would come from.
    """
    lowered = text.lower()
    has_commitment = any(re.search(p, lowered) for p in _COMMITMENT_PATTERNS)
    has_vague_only = any(re.search(p, lowered) for p in _VAGUE_PATTERNS) and not has_commitment
    date = _resolve_date(now, lowered)

    if has_commitment and date is not None:
        return ExtractedCommitment(
            is_promise=True,
            promised_date=date,
            amount_paise=_find_amount_paise(text),
            confidence=0.75,
            verbatim=text,
            rationale="a commitment verb and a date reference are both present",
        )

    reason = (
        "a date reference is present with no commitment verb (or only a vague "
        "acknowledgement) -- not a promise"
        if has_vague_only or date is not None
        else "no commitment verb and no date reference"
    )
    return ExtractedCommitment(
        is_promise=False,
        promised_date=None,
        amount_paise=None,
        confidence=0.0,
        verbatim=text,
        rationale=reason,
    )


# --------------------------------------------------------------------------
# Extraction -- the LLM path (PRD 6.8's actual answer to "genuinely hard")
# --------------------------------------------------------------------------

PROMISE_EXTRACTION_INSTRUCTIONS = """\
You are extracting a payment commitment from one customer reply. The customer \
may write in Hinglish (Hindi-English code-switching).

A promise needs a real commitment, not a vague acknowledgement. \
"haan haan kal dekhta hoon" (yeah yeah I'll look tomorrow) is NOT a promise -- \
there is no commitment, only an acknowledgement. "Friday tak pakka kar dunga" \
(I will definitely pay by Friday) IS a promise -- a specific date and a real \
commitment.

The conversation's current time is %(now)s. Resolve any relative date \
("kal"/tomorrow, a weekday name) against it and reply with an absolute date.

Customer reply:
%(reply)s

Reply with a single JSON object:
{"is_promise": true/false, "promised_date": "YYYY-MM-DDTHH:MM:SS+05:30" or null, \
"amount_paise": 0 or null, "confidence": 0.0, "rationale": "..."}
"""


def build_promise_extraction_prompt(reply_text: str, *, now: str) -> str:
    return PROMISE_EXTRACTION_INSTRUCTIONS % {"now": now, "reply": reply_text}


def extract_commitment_via_llm(client: Any, reply_text: str, *, now: str) -> ExtractedCommitment:
    """The LLM path. ``screen=False`` -- see the module docstring and ADR-038.

    Tested against ``tests.scripted.ScriptedLLM`` exactly like the planner's
    own tests (``tests/test_promises.py``); not exercised against a live
    model this session (see the module docstring for why).
    """
    prompt = build_promise_extraction_prompt(reply_text, now=now)
    response = client.call(prompt, tier="fast", schema={"type": "object"}, screen=False)
    data = response.json()
    return ExtractedCommitment(
        is_promise=bool(data.get("is_promise", False)),
        promised_date=data.get("promised_date"),
        amount_paise=(int(data["amount_paise"]) if data.get("amount_paise") else None),
        confidence=float(data.get("confidence", 0.0)),
        verbatim=reply_text,
        rationale=str(data.get("rationale", "")),
    )
