"""Canonicalisation -- the module every determinism guarantee rests on.

Three jobs, and all three are about making a hash stable:

1. ``canonical_json`` / ``sha256_hex`` -- the byte-exact serialisation used by
   the ledger hash chain, the LLM cache key, and the planner signature.
2. ``amount_band`` -- collapses an unbounded rupee figure into one of exactly
   five bands (BUILD-PLAN 1.5, frozen decision F6). This is what makes the
   planner signature space finite.
3. ``hour_bucket`` / ``channel_eligibility`` -- the remaining derived features.

PRD 9.1 is the reason this file exists. If a high-cardinality identifier ever
reaches a prompt, every LLM call becomes a cache miss, the memoisation ratio
goes 30x -> 1x, and the token budget goes 800K -> 15M. Silently. Everything here
is built so that the only way to construct a prompt is from a finite, validated
feature set.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Tuple

# --------------------------------------------------------------------------
# Byte-exact serialisation
# --------------------------------------------------------------------------

#: Indian Standard Time. Every regulatory window (R5, R8, R9) is stated in IST,
#: so bucketing must happen in IST. Bucketing a UTC hour would place the
#: boundary 5h30m away from the rule it is supposed to represent.
IST = timezone(timedelta(hours=5, minutes=30))


def canonical_json(obj: Any) -> str:
    """The one serialisation used wherever bytes must be reproducible.

    - ``sort_keys``       -- dict insertion order must not leak into the hash.
    - tight separators    -- no incidental whitespace.
    - ``ensure_ascii``    -- output is pure ASCII, so the bytes do not depend on
      the writer's locale or filesystem encoding. Hinglish content is escaped
      rather than emitted raw: a deliberate trade of readability for
      portability, because the ledger hash has to match on a reviewer's machine.
    - ``allow_nan=False`` -- NaN/Infinity are not JSON, and a float in a money
      field is a bug anyway. Every amount in this system is integer paise.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sha256_hex(payload: Any) -> str:
    """SHA-256 of a string, or of the canonical JSON of anything else."""
    if not isinstance(payload, str):
        payload = canonical_json(payload)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


GENESIS_HASH = "0" * 64

# --------------------------------------------------------------------------
# Amount bands -- FROZEN (F6). Changing these invalidates the entire LLM cache.
# --------------------------------------------------------------------------
#
# Five bands. The Rs 15,000 and Rs 1,00,000 boundaries are not arbitrary
# cardinality reduction: they are RBI's AFA exemption thresholds (envelope rule
# R2, PRD 6.5). Subsequent e-mandate debits are AFA-exempt up to Rs 15,000, and
# up to Rs 1,00,000 for insurance premiums, mutual-fund subscriptions and
# credit-card bills. So a plan for band 4 may legitimately differ from a plan
# for band 3 for a *legal* reason rather than a statistical one, and the band
# carries that compliance meaning into the signature for free.
#
# Bands are UPPER-INCLUSIVE: band N covers (ceiling[N-1], ceiling[N]].
# This matters. RBI's exemption is "up to Rs 15,000" -- inclusive. A debit of
# exactly Rs 15,000.00 is exempt and must not share a band with Rs 15,000.01,
# which is not. Lower-inclusive bands would put the exempt boundary case in the
# non-exempt band, and the envelope would then demand AFA for a payment that
# does not need it.

AMOUNT_BAND_CEILINGS_PAISE: Tuple[int, ...] = (
    50_000,      # band 1: Rs 0           .. Rs 500        inclusive
    500_000,     # band 2: Rs 500.01      .. Rs 5,000      inclusive
    1_500_000,   # band 3: Rs 5,000.01    .. Rs 15,000     inclusive  <- R2
    10_000_000,  # band 4: Rs 15,000.01   .. Rs 1,00,000   inclusive  <- R2
)                # band 5: Rs 1,00,000.01 and above

AMOUNT_BANDS: Tuple[int, ...] = (1, 2, 3, 4, 5)

AMOUNT_BAND_LABELS: Dict[int, str] = {
    1: "0-500",
    2: "500-5k",
    3: "5k-15k",
    4: "15k-1L",
    5: "1L+",
}

#: Bands 1-3 sit at or below R2's Rs 15,000 AFA exemption ceiling.
AFA_EXEMPT_BANDS: Tuple[int, ...] = (1, 2, 3)


def amount_band(amount_paise: int) -> int:
    """Map integer paise onto one of the five frozen bands.

    ``amount_band(1_500_000) -> 3``  exactly Rs 15,000, AFA-exempt.
    ``amount_band(1_500_001) -> 4``  Rs 15,000.01, AFA required.
    """
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int):
        raise TypeError(
            "amount must be integer paise, got %s -- floats in money fields are "
            "how rounding errors get into a ledger" % type(amount_paise).__name__
        )
    if amount_paise < 0:
        raise ValueError(
            "amount_at_risk_paise must be non-negative, got %d" % amount_paise
        )
    for band, ceiling in enumerate(AMOUNT_BAND_CEILINGS_PAISE, start=1):
        if amount_paise <= ceiling:
            return band
    return 5


def amount_band_label(amount_paise: int) -> str:
    return AMOUNT_BAND_LABELS[amount_band(amount_paise)]


# --------------------------------------------------------------------------
# The remaining closed vocabularies
# --------------------------------------------------------------------------

SOURCE_TYPES: Tuple[str, ...] = (
    "payment",       # payment.failed
    "checkout",      # order created, never captured
    "subscription",  # subscription.pending -> halted
    "mandate",       # e-mandate debit failure
    "receivable",    # invoice past due
)

#: How fast this money dies (PRD 3.1). The field that lets one policy handle
#: both a UPI PIN error and a 45-day-overdue invoice without special-casing.
DECAY_PROFILES: Tuple[str, ...] = (
    "minutes",          # payment degradation -- fix routing, do not call anyone
    "minutes_hours",    # checkout drop-off -- fast, light touch
    "days",             # failed subscription -- sequenced, notification-gated
    "days_hard_floor",  # mandate retry -- R1's 24h notification floor
    "weeks",            # B2B receivable -- escalation ladder
)

DECAY_BY_SOURCE_TYPE: Dict[str, str] = {
    "payment": "minutes",
    "checkout": "minutes_hours",
    "subscription": "days",
    "mandate": "days_hard_floor",
    "receivable": "weeks",
}

#: PRD 3.1's second load-bearing field, and the one most builds will not have at
#: all. These three have different legal calling windows (PRD 7): a receivables
#: chase is debt collection (R9), a retry link is a service message, an upsell
#: is promotional (R5).
LEGAL_CONTEXTS: Tuple[str, ...] = ("service", "collection", "promotional")

SEGMENTS: Tuple[str, ...] = ("metro", "tier2", "tier3")

#: Three arms, not two (F5, PRD 8.1). PRD 3.1 sketches ``arm`` as
#: treatment|control; F5 supersedes that sketch, because a two-arm design loses
#: C-B, the only measured answer to "did the LLM earn its place". Recorded as
#: ADR-004 in DECISIONS.md.
ARMS: Tuple[str, ...] = ("A", "B", "C")

ARM_LABELS: Dict[str, str] = {
    "A": "control (detect, diagnose, log, do not act)",
    "B": "rules-only (deterministic reason-class map, no LLM)",
    "C": "llm-planned (investigator -> planner -> envelope)",
}

#: The bounded action space. ``ACT_WAIT`` is first-class rather than an
#: afterthought (PRD 5.1): the plurality of failures are transient and
#: infrastructural, so the correct first action is frequently not to contact the
#: customer at all. An agent whose action space cannot express "wait seven
#: minutes, they are probably already retrying" will pay to message people who
#: have already paid.
ACTIONS: Tuple[str, ...] = (
    "ACT_WAIT",            # T0 -- they are probably already retrying
    "ACT_ROUTE",           # T0 -- routing / rail change
    "ACT_RETRY",           # T1 -- charge retry; reversible
    "ACT_MESSAGE",         # T2 -- SMS / WhatsApp / email; irreversible
    "ACT_VOICE",           # T3 -- Hinglish voice call; really irreversible
    "ACT_CONCESSION",      # T4 -- discount / waiver; human approval over a cap
    "ACT_ALERT_MERCHANT",  # T0 -- merchant configuration, not a customer problem
    "ACT_PAGE_ENGINEER",   # T0 -- integration defect
    "ACT_ESCALATE_HUMAN",  # T4 -- risk, distress, dispute, legal signal
    "ACT_STOP",            # terminal -- one of S1..S7 fired
)

# --------------------------------------------------------------------------
# Hour bucketing -- three values, each a distinct legal state
# --------------------------------------------------------------------------
#
# Not "morning/afternoon/evening". Every boundary here is a regulatory edge, so
# the bucket carries the same kind of free compliance information the amount
# bands do:
#
#   business      08:00-19:00 IST   see the KNOWN GAP below
#   evening_peak  19:00-21:00 IST   R5/R8 still open; R9 debt collection CLOSED
#   night         21:00-08:00 IST   every outbound contact window closed
#
#   R5  promotional SMS   09:00-21:00   [B] -- unverified, see SAFETY.md when it exists
#   R8  commercial voice  09:00-21:00   [B]
#   R9  debt collection   08:00-19:00   [B]
#
# 08:00 and 21:00 are the outer union of those windows; 19:00 is R9's ceiling.
#
# KNOWN GAP -- 08:00-09:00 IST is a fourth legal state, and three buckets cannot
# hold it. R9 opens at 08:00 but R5 and R8 do not open until 09:00, so the real
# state space is:
#
#   00:00-08:00   everything closed
#   08:00-09:00   R9 open, R5/R8 CLOSED      <- not representable here
#   09:00-19:00   everything open
#   19:00-21:00   R5/R8 open, R9 closed
#   21:00-24:00   everything closed
#
# This bucketing merges 08:00-09:00 into "business", so channel_eligibility can
# return "full" -- voice permitted -- during an hour when the R8 voice window is
# shut. In the seeded 6,000-event batch that is 322 events (5.4%), of which 166
# are marked "full" and 24 are offered ACT_VOICE. Nothing acts on them today
# (there is no executor yet), so no rule has actually been broken -- but the
# feature Day 2's envelope inherits already disagrees with the rule it cites.
#
# Deliberately NOT fixed here. hour_bucket is one of the seven frozen signature
# fields (F7) and its domain is frozen precisely so that Day 2's handover cannot
# invalidate the cache, so adding a bucket is a frozen-decision change; and
# whether R5/R8 bind a service-context recovery call at 08:30 is a [B] figure
# that has not been checked against a primary TRAI instrument. HANDOFF 9 says
# stop and ask on both counts. Resolve the regulation first, then decide the
# bucket count -- do not guess the boundary into the cache key.

HOUR_BUCKETS: Tuple[str, ...] = ("business", "evening_peak", "night")


def hour_bucket(detected_at: str) -> str:
    """Bucket an ISO-8601 *event* timestamp into one of three legal states.

    Takes event time, never wall-clock.
    """
    dt = parse_iso(detected_at).astimezone(IST)
    hour = dt.hour
    if 8 <= hour < 19:
        return "business"
    if 19 <= hour < 21:
        return "evening_peak"
    return "night"


# --------------------------------------------------------------------------
# Channel eligibility -- three values
# --------------------------------------------------------------------------
#
# Which channels are open *right now*, as one of three coarse states. It is a
# signature feature, so it has to be low-cardinality and derived -- never free
# text.
#
# Day 1 owns a minimal derivation from (reason_class, legal_context,
# hour_bucket) so the signature is complete and the cache key is stable from the
# first commit. Day 2's envelope becomes the authority: it will compute the same
# three values with full rule citation (R5/R8/R9, contact budgets, consent and
# DND state) and this function will delegate to it. The vocabulary is frozen now
# precisely so that handover cannot invalidate the cache.

CHANNEL_ELIGIBILITY: Tuple[str, ...] = (
    "silent_only",         # no customer contact permitted at all
    "silent_and_message",  # retry / route / message; no voice
    "full",                # retry / route / message / voice
)


def channel_eligibility(
    reason_class: str, legal_context: str, hour_bucket_value: str
) -> str:
    """Coarse contact permission. A cache key, **not** a gate.

    Day 2 took ownership of the derivation, as STATE.md said it would: the body
    now lives in ``pramaan.envelope.windows``, beside the rules it cites. This
    function stays as the signature-feature entry point so that F7 is untouched
    and the cache key is computed from the same place it always was.

    What did *not* change is the output for any input, and that was the point of
    the handover rather than an accident of it. ``channel_eligibility`` is one of
    the seven frozen signature fields; changing its values churns every cached
    plan, and the values themselves are a coarse hint computed from a three-value
    hour bucket that provably cannot represent the 08:00-09:00 legal state.

    So this feature is allowed to be optimistic, and the envelope is the
    authority. At 08:30 in a service context this returns ``full`` -- voice
    permitted -- while ``envelope.judge`` refuses the call citing R8, whose
    window does not open until 09:00. The disagreement is deliberate, pinned by
    ``tests/test_envelope_matrix.py``, and safe in exactly one direction: a
    permissive cache key costs nothing because no action is taken on it, whereas
    a permissive *gate* would be a compliance breach.
    """
    from pramaan.envelope.windows import (  # local import: avoids a cycle
        channel_eligibility_for_bucket,
    )

    return channel_eligibility_for_bucket(
        reason_class, legal_context, hour_bucket_value
    )


# --------------------------------------------------------------------------
# The planner signature -- FROZEN at exactly seven fields (F7)
# --------------------------------------------------------------------------
#
# Nominal space is 10 x 11 x 5 x 3 x 3 x 3 x 3 = 44,550. (BUILD-PLAN 1.5 quotes
# 40,500 from ten diagnosis classes; there are eleven, because "undiagnosed" is a
# live value for arm B rather than a placeholder.)
#
# MEASURED on the seeded 6,000-event batch: 273 distinct signatures, a ratio of
# 22.0x. The build plan predicted ~100-200 and ~30x; the measured figures are the
# ones to quote. The gap to nominal holds because most combinations are
# structurally unreachable (MERCHANT_CONFIG never reaches the planner at all --
# the envelope handles it; INSTRUMENT_DEAD never co-occurs with an
# issuer_degraded diagnosis) and real traffic is concentrated (PRD 5.1: three
# reason classes are 70-100% of volume).
#
# Read 22.0x as an UPPER BOUND on today's data, not a prediction. Two of the
# seven fields are currently pinned to one value -- diagnosis_class (no
# investigator until Day 4) and legal_context (payment failures only until the
# Day 6 receivables adapter) -- so the effective space today is 1,350, not
# 44,550, and 273 of those 1,350 are already occupied. The ratio will fall as
# each field goes live.
#
# Adding a field can multiply the signature space. Do not add one.

PLANNER_SIGNATURE_FIELDS: Tuple[str, ...] = (
    "reason_class",         # 10 values -- Appendix A
    "diagnosis_class",      # ~10 values -- the investigator's output
    "amount_band",          # 5 values  -- F6, above
    "segment",              # 3 values  -- metro | tier2 | tier3
    "legal_context",        # 3 values  -- service | collection | promotional
    "channel_eligibility",  # 3 values  -- which channels are open right now
    "hour_bucket",          # 3 values  -- business | evening_peak | night
)

#: The investigator's output class (Day 4). "undiagnosed" is the real value on
#: Day 1, and stays the value for arm B, which never invokes an investigator --
#: so it is a live production value, not a placeholder.
DIAGNOSIS_CLASSES: Tuple[str, ...] = (
    "undiagnosed",
    "issuer_degraded",         # a specific bank / issuer is failing
    "rail_degraded",           # the whole method (UPI/netbanking/card) degraded
    "customer_auth_friction",  # PIN / OTP / CVV -- the customer can fix it
    "customer_funds",          # insufficient balance, credit-cycle timing
    "customer_limit",          # per-transaction or daily cap
    "instrument_dead",         # the instrument cannot succeed; re-collect it
    "merchant_misconfigured",  # the merchant's own settings
    "integration_defect",      # the merchant's code
    "risk_declined",           # risk / compliance -- human only
    "already_settled",         # already paid; terminate the thread (S1)
)

#: Legal values per signature field, for validation. Kept adjacent to
#: PLANNER_SIGNATURE_FIELDS so the two cannot drift apart.
SIGNATURE_DOMAINS: Dict[str, Tuple[Any, ...]] = {}


def _init_signature_domains() -> None:
    from pramaan.taxonomy import REASON_CLASSES

    SIGNATURE_DOMAINS.update(
        {
            "reason_class": REASON_CLASSES,
            "diagnosis_class": DIAGNOSIS_CLASSES,
            "amount_band": AMOUNT_BANDS,
            "segment": SEGMENTS,
            "legal_context": LEGAL_CONTEXTS,
            "channel_eligibility": CHANNEL_ELIGIBILITY,
            "hour_bucket": HOUR_BUCKETS,
        }
    )


def validate_features(features: Dict[str, Any]) -> Dict[str, Any]:
    """Reject anything that is not exactly the seven frozen fields, in domain.

    This is the structural half of invariant I2. A prompt cannot be built from a
    feature dict that has not passed through here, so an identifier cannot reach
    a prompt by being *added* to the feature set -- only by being smuggled into a
    field's value, which prompts.py then screens for.
    """
    if not SIGNATURE_DOMAINS:
        _init_signature_domains()

    keys = set(features)
    expected = set(PLANNER_SIGNATURE_FIELDS)
    if keys != expected:
        raise ValueError(
            "feature set must be exactly PLANNER_SIGNATURE_FIELDS (F7); "
            "extra=%r missing=%r"
            % (sorted(keys - expected), sorted(expected - keys))
        )
    for field in PLANNER_SIGNATURE_FIELDS:
        value = features[field]
        domain = SIGNATURE_DOMAINS[field]
        if value not in domain:
            raise ValueError(
                "%s=%r is outside its frozen domain %r -- a value outside the "
                "domain is unbounded cardinality, which is exactly what the "
                "signature exists to prevent" % (field, value, domain)
            )
    return features


def planner_signature(features: Dict[str, Any]) -> str:
    """The memoisation key: a stable string over exactly the seven fields.

    Human-readable on purpose. When the memoisation ratio moves you want to read
    signatures straight out of the ledger and see immediately which field went
    high-cardinality.
    """
    validate_features(features)
    return "|".join("%s=%s" % (f, features[f]) for f in PLANNER_SIGNATURE_FIELDS)


def signature_hash(features: Dict[str, Any]) -> str:
    return sha256_hex(planner_signature(features))


# --------------------------------------------------------------------------
# Time helpers -- event time only
# --------------------------------------------------------------------------

def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, requiring an explicit UTC offset.

    A naive timestamp is a bug: it means a wall-clock value was written without
    recording which clock produced it, and every downstream bucket then shifts
    with the machine's locale.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(
            "timestamp %r has no timezone offset; event times must be explicit"
            % value
        )
    return dt


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("refusing to serialise a naive datetime")
    return dt.astimezone(IST).isoformat(timespec="seconds")
