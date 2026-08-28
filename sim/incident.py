"""Incidents, and the denominators that make an incident visible.

Day 4 needs two things the Day 1-3 world does not have, and neither is a detail.

**A failure rate needs a denominator.** ``events`` holds failures only. There is
no ``attempts`` anywhere in the Day 1-3 schema, so "failure rate" was not a
computable quantity -- which means ``compare_baseline`` and ``decompose`` (PRD
6.2, 6.3) would have been facades over data that cannot support them. A
segment's failure *count* rising tells you nothing: it rises when that segment
sends more traffic. So this module declares a baseline failure rate per segment,
anchored to the published ranges in PRD 6.3, and *derives* attempt counts from
the observed failures. Attempts are the denominator; they are not invented
independently of the failures they have to stay consistent with.

**Nothing was ever wrong.** ``generate()`` draws every event i.i.d. from a fixed
distribution: reason code, segment and hour are independent of each other and of
calendar time. That is the right null world for Days 1-3, whose job was to
measure an estimator, but it contains no incident. There is no window in which
anything is elevated, so an investigator run against it could only ever
correctly conclude "nothing is happening" -- and a Day 4 that ships an
investigator unable to demonstrate a find is not a Day 4.

So this module injects one, and the injection is designed to be *diagnostically
ambiguous in exactly the way PRD 6.3 describes*:

- a **rate shift** -- extra failures in a window with no extra attempts behind
  them. Something broke. This is the part an intervention should act on.
- a **mix shift** -- extra attempts from a structurally weaker segment, with
  failures in proportion to that segment's *unchanged* baseline rate. Nothing
  broke. Traffic moved. Acting on this is a false positive.

Both at once, in the same window, so the blended failure rate rises and the
naive reading ("failures are up twelve points, the bank must be down") is wrong.
An investigator that reports the blended figure has failed; one that separates
the two terms and says which it is acting on has done the job. That is the whole
reason the decomposition survived from v1 (PRD 6.3).

**The default is off, and that is load-bearing.** ``generate(degradation=None)``
executes exactly the code path it executed on Day 3, drawing from the same RNG
stream in the same order, so every figure Day 3 pinned -- the arm vectors by
SHA-256, the 22.0x memoisation ratio, the 18,028-row ledger, the 4,905/1,095/0
envelope split -- is unchanged afterwards. Injected events are appended *after*
the base loop completes, so they cannot perturb a single base draw.
``tests/test_incident.py`` asserts that byte-identity rather than trusting this
paragraph.

**Ground truth is recorded as what was injected** -- not as the answer the
decomposition ought to print. ``InjectedTruth`` says "38 extra failures went in
with no extra attempts behind them, and 300 extra attempts went in carrying
their own segment's baseline rate". What ``decompose`` should therefore report is
a *consequence* of that, and the test derives it independently from the stored
counts with its own arithmetic (ADR-023: a test may not compare a function to
its own delegate).

**The injected magnitudes were sized from the generated counts, not chosen.** A
first pass used 180 and 900, which on a 200-event base drove tier2's in-window
failure rate to a clamped 1.000 -- a physically impossible reading, and one the
decomposition cannot represent, so the incident was invisible to the very tool
meant to find it. The sizes below are computed backwards from the actual
per-slice counts to land tier2 at a +11pp rate shift and tier3 at a 9.3% -> 26.6%
share of attempts. Both are large enough to clear sampling noise on this batch
and small enough to be a plausible afternoon rather than an outage.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from random import Random
from typing import Dict, List, Optional, Sequence, Tuple

from pramaan import canonical
from pramaan.config import SIM_EPOCH
from pramaan.sense.models import Counterparty, RiskEvent
from pramaan.taxonomy import BY_CODE

# --------------------------------------------------------------------------
# Denominators
# --------------------------------------------------------------------------
#
# PRD 6.3, graded [B]: "published success rates run 78-82% metro against 55-62%
# Tier-3". Those are *success* rates, so failure rates are the complements, and
# each is taken at its midpoint rather than at whichever end flatters a later
# number.
#
#   metro   78-82% success -> 20.0% failure
#   tier3   55-62% success -> 41.5% failure
#
# tier2 is not published and is interpolated, which is a judgement and is graded
# [C] accordingly. It sits nearer metro than a midpoint would put it, because the
# metro/tier2 infrastructure gap is smaller than the tier2/tier3 gap -- also a
# judgement, and also [C].
#
# The size of the metro/tier3 spread is what makes a mix shift a real confounder
# rather than a theoretical one: moving share from metro to tier3 moves the
# blended failure rate by 21.5 points per unit of share moved, with nothing
# broken anywhere in the system.

BASELINE_FAILURE_RATE: Dict[str, float] = {
    "metro": 0.200,   # [B] -- PRD 6.3, midpoint of the 78-82% success range
    "tier2": 0.290,   # [C] -- interpolated, see above
    "tier3": 0.415,   # [B] -- PRD 6.3, midpoint of the 55-62% success range
}

#: Attempts are derived from failures and then jittered, because a denominator
#: that is exactly ``failures / rate`` makes every segment's observed rate
#: *identically* equal to the declared baseline. A baseline with no sampling
#: noise around it is not a baseline, it is a constant -- and
#: ``compare_baseline`` would then report a 0.000pp deviation on every quiet
#: segment, which reads as a working tool and is in fact a tool handed a world
#: with no variance to detect.
ATTEMPT_JITTER = 0.10


@dataclass(frozen=True)
class DegradationSpec:
    """One injected incident. The two shifts are optional and independent.

    Windows are expressed in **day indices** relative to ``SIM_EPOCH``, never as
    calendar dates. Day indices are what the agent-facing projection exposes
    (see ``pramaan/investigate/tools.py``): a date string would be a raw
    timestamp in prompt bytes, which the canonicality screen refuses, and
    rightly.
    """

    name: str
    start_day: int
    end_day: int                      # inclusive

    #: RATE shift: extra failures, no extra attempts. Something broke.
    rate_segment: Optional[str] = None
    rate_code: str = "bank_technical_error"
    rate_extra_failures: int = 0

    #: MIX shift: extra attempts, plus failures at that segment's *own*
    #: unchanged baseline rate. Nothing broke; traffic moved.
    mix_segment: Optional[str] = None
    mix_extra_attempts: int = 0

    #: Whether the platform declared a downtime covering this window. False is
    #: the interesting setting: it is what lets ``get_downtime`` be a
    #: *discriminating* tool rather than a corroborating one. An investigator
    #: that finds an elevated rate and no declared downtime has learned
    #: something real -- either the platform has not noticed yet, or the
    #: elevation is not a platform problem at all.
    declares_downtime: bool = False
    downtime_method: str = "upi"

    def __post_init__(self) -> None:
        if self.end_day < self.start_day:
            raise ValueError("end_day must be >= start_day")
        for label, segment in (("rate", self.rate_segment), ("mix", self.mix_segment)):
            if segment is not None and segment not in canonical.SEGMENTS:
                raise ValueError("%s_segment %r is not a known segment" % (label, segment))
        if self.rate_segment is not None and self.rate_code not in BY_CODE:
            raise ValueError("rate_code %r is not in the taxonomy" % self.rate_code)
        if self.rate_extra_failures < 0 or self.mix_extra_attempts < 0:
            raise ValueError("injected counts must be non-negative")

    @property
    def days(self) -> Tuple[int, ...]:
        return tuple(range(self.start_day, self.end_day + 1))

    def covers(self, day_index: int) -> bool:
        return self.start_day <= day_index <= self.end_day


@dataclass(frozen=True)
class InjectedTruth:
    """What went in. Deliberately not what should come out.

    The distinction matters. Recording "the decomposition should say +7.9pp of
    rate effect" here would make the Day 4 test a comparison against a number
    typed by the same hand that wrote the tool -- the ADR-024 mistake, one layer
    up. So this records the *mechanism*: extra failures with no attempts behind
    them, and extra attempts carrying their own baseline rate. Whatever the
    shift-share arithmetic yields from that is a consequence, and the test
    derives it independently.
    """

    spec_name: str
    start_day: int
    end_day: int
    rate_segment: Optional[str]
    rate_extra_failures: int
    mix_segment: Optional[str]
    mix_extra_attempts: int
    mix_extra_failures: int
    downtime_declared: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "spec_name": self.spec_name,
            "window_days": [self.start_day, self.end_day],
            "rate_segment": self.rate_segment,
            "rate_extra_failures": self.rate_extra_failures,
            "mix_segment": self.mix_segment,
            "mix_extra_attempts": self.mix_extra_attempts,
            "mix_extra_failures": self.mix_extra_failures,
            "downtime_declared": self.downtime_declared,
        }


#: The one incident Day 4 is demonstrated against.
#:
#: Shape, and why each choice is what it is:
#:
#: - The window is days 3-4 of an 8-day batch, so there are three clean days of
#:   trailing baseline before it and three after. ``compare_baseline`` needs a
#:   *trailing* baseline, so an incident starting on day 0 would leave it nothing
#:   to compare against.
#: - The batch is 1,200 events, not the 200-event dev batch. That is a statement
#:   about rows, not about tokens: the investigator's cost is one incident either
#:   way, and SQL over 1,200 rows is free. At 200 events a (day, segment) slice
#:   holds ~11 failures, so any incident realistic enough to be worth diagnosing
#:   is smaller than the noise -- which is the same minimum-detectable-effect
#:   arithmetic Day 3 recorded (22.2pp at 67/arm), showing up one layer down.
#: - The rate shift is on **tier2**, not tier3. Putting it on the weakest segment
#:   would let a lazy investigator reach the right answer for the wrong reason
#:   ("tier3 is bad, and tier3 volume is up") without ever separating the terms.
#: - The mix shift is on **tier3**, the segment with the highest baseline rate.
#:   That is what makes the confound bite: extra tier3 traffic raises the blended
#:   rate with nothing broken.
#: - No downtime is declared, so the corroboration an investigator would like is
#:   absent and the honest diagnosis has to rest on the decomposition.
DEV_INCIDENT = DegradationSpec(
    name="tier2_bank_timeout_with_tier3_mix_shift",
    start_day=3,
    end_day=4,
    rate_segment="tier2",
    rate_code="bank_technical_error",
    rate_extra_failures=38,
    mix_segment="tier3",
    mix_extra_attempts=300,
    declares_downtime=False,
)


# --------------------------------------------------------------------------
# Injection
# --------------------------------------------------------------------------


def day_index(detected_at: str) -> int:
    """Whole days between SIM_EPOCH and this event, in IST.

    Computed in Python rather than in SQL. SQLite's ``strftime`` normalises an
    ISO string carrying ``+05:30`` to UTC, which would move an event detected at
    02:00 IST into the previous day and silently mis-bucket every window
    boundary. The codebase is already explicit that IST bucketing is not optional
    (``canonical.IST``); this is the same rule one layer down.
    """
    epoch = canonical.parse_iso(SIM_EPOCH).astimezone(canonical.IST)
    moment = canonical.parse_iso(detected_at).astimezone(canonical.IST)
    return (moment.date() - epoch.date()).days


def hour_of_day(detected_at: str) -> int:
    return canonical.parse_iso(detected_at).astimezone(canonical.IST).hour


def weekday(detected_at: str) -> int:
    """0 = Monday. Low-cardinality, and payments traffic is weekday-shaped."""
    return canonical.parse_iso(detected_at).astimezone(canonical.IST).weekday()


def _synthetic_event(
    rng: Random,
    *,
    index: int,
    seed: int,
    code: str,
    segment: str,
    day: int,
    pool_size: int,
) -> RiskEvent:
    """One injected failure, shaped exactly like a generated one.

    Amount and hour are drawn from the same distributions the base stream uses,
    deliberately. An injected event distinguishable by its amount or its hour
    would let a test pass for the wrong reason: the investigator would be keying
    on an artefact of the injection rather than on an elevated rate.

    The arm is left at ``A`` here and overwritten by the caller, which holds the
    live stratified assigner. Splitting it that way keeps this function pure with
    respect to the assigner's state.
    """
    from sim.generate import (
        HOUR_WEIGHTS,
        _available_actions,
        _external_ref,
        _lognormal_paise,
        _self_recovery,
        _weighted_choice,
    )

    reason_class = BY_CODE[code].reason_class
    amount_paise = _lognormal_paise(rng)
    hour = int(_weighted_choice(rng, tuple((str(h), w) for h, w in enumerate(HOUR_WEIGHTS))))
    minute = int(rng.random() * 60)
    second = int(rng.random() * 60)

    epoch = canonical.parse_iso(SIM_EPOCH)
    detected_dt = epoch + timedelta(days=day, hours=hour, minutes=minute, seconds=second)
    detected_at = canonical.to_iso(detected_dt)

    band = canonical.amount_band(amount_paise)
    legal_context = "service"
    eligibility = canonical.channel_eligibility(
        reason_class, legal_context, canonical.hour_bucket(detected_at)
    )

    return RiskEvent(
        # 9-prefixed ordinal, so an injected id can never collide with a base one
        # and is greppable in a ledger when something looks wrong.
        event_id="evt_%04d_9%05d" % (seed, index),
        source_type="payment",
        amount_at_risk_paise=amount_paise,
        counterparty=Counterparty(
            id="cp_%05d" % (int(rng.random() * pool_size) % pool_size),
            kind="payer",
            segment=segment,
        ),
        detected_at=detected_at,
        decay_profile=canonical.DECAY_BY_SOURCE_TYPE["payment"],
        cause_signal=code,
        legal_context=legal_context,
        available_actions=_available_actions(reason_class, eligibility, band),
        arm="A",
        external_ref=_external_ref(rng),
        latent=_self_recovery(rng, reason_class, detected_dt),
    )


def inject(
    events: Sequence[RiskEvent],
    spec: DegradationSpec,
    seed: int,
    *,
    arms=None,
) -> Tuple[List[RiskEvent], InjectedTruth]:
    """Append the incident's extra failures to a base stream.

    Takes the base stream as an argument and returns a new list; it does not
    reach into ``generate``'s loop. That separation is what makes the
    ``degradation=None`` path provably unchanged.

    ``arms`` is the live ``ArmAssigner``, so injected events are assigned by the
    same stratified mechanism as every other event. Constructing a fresh assigner
    here would be subtly wrong: the per-stratum counters carry state, and
    restarting them would concentrate injected events in whichever arm each
    stratum's block permutation happens to open with.
    """
    if arms is None:
        from pramaan.eval.arms import ArmAssigner

        arms = ArmAssigner(seed)

    # A separate stream, seeded off the base seed. Deterministic, and drawing
    # from it cannot consume a draw the base loop would otherwise have made.
    rng = Random(seed * 7919 + 13)
    pool_size = max(1, len(events) // 3)
    window = spec.days

    injected: List[RiskEvent] = []
    counter = 0

    # -- the rate shift: failures with no attempts behind them -------------
    if spec.rate_segment is not None and spec.rate_extra_failures > 0:
        for _ in range(spec.rate_extra_failures):
            day = window[int(rng.random() * len(window)) % len(window)]
            event = _synthetic_event(
                rng,
                index=counter,
                seed=seed,
                code=spec.rate_code,
                segment=spec.rate_segment,
                day=day,
                pool_size=pool_size,
            )
            injected.append(
                replace(event, arm=arms.assign("payment", event.amount_band, spec.rate_segment))
            )
            counter += 1

    # -- the mix shift: extra attempts, carrying their own baseline rate ----
    mix_extra_failures = 0
    if spec.mix_segment is not None and spec.mix_extra_attempts > 0:
        # Failures in proportion to the segment's *unchanged* rate. That is the
        # definition of a mix shift: the segment is no worse than it always was,
        # there is simply more of it.
        mix_extra_failures = int(
            round(spec.mix_extra_attempts * BASELINE_FAILURE_RATE[spec.mix_segment])
        )
        from sim.generate import _pick_code

        for _ in range(mix_extra_failures):
            day = window[int(rng.random() * len(window)) % len(window)]
            # Codes drawn from the ordinary distribution, not from one cause. A
            # mix shift is not a new failure mode; it is more of the same ones.
            code = _pick_code(rng)
            event = _synthetic_event(
                rng,
                index=counter,
                seed=seed,
                code=code,
                segment=spec.mix_segment,
                day=day,
                pool_size=pool_size,
            )
            injected.append(
                replace(event, arm=arms.assign("payment", event.amount_band, spec.mix_segment))
            )
            counter += 1

    combined = list(events) + injected
    combined.sort(key=lambda e: (e.detected_at, e.event_id))

    truth = InjectedTruth(
        spec_name=spec.name,
        start_day=spec.start_day,
        end_day=spec.end_day,
        rate_segment=spec.rate_segment,
        rate_extra_failures=spec.rate_extra_failures if spec.rate_segment else 0,
        mix_segment=spec.mix_segment,
        mix_extra_attempts=spec.mix_extra_attempts if spec.mix_segment else 0,
        mix_extra_failures=mix_extra_failures,
        downtime_declared=spec.declares_downtime,
    )
    return combined, truth


def truth_for(spec: DegradationSpec) -> InjectedTruth:
    """The ground truth for a spec, without running the injection.

    Every field of ``InjectedTruth`` is a pure function of the spec and the
    declared baseline rates, so a caller that already has the events does not
    need to re-inject to learn what went in. ``inject`` returns the same object;
    ``tests/test_incident.py`` asserts the two agree, because two paths to one
    value is exactly how a value quietly becomes two values.
    """
    mix_extra_failures = 0
    if spec.mix_segment is not None and spec.mix_extra_attempts > 0:
        mix_extra_failures = int(
            round(spec.mix_extra_attempts * BASELINE_FAILURE_RATE[spec.mix_segment])
        )
    return InjectedTruth(
        spec_name=spec.name,
        start_day=spec.start_day,
        end_day=spec.end_day,
        rate_segment=spec.rate_segment,
        rate_extra_failures=spec.rate_extra_failures if spec.rate_segment else 0,
        mix_segment=spec.mix_segment,
        mix_extra_attempts=spec.mix_extra_attempts if spec.mix_segment else 0,
        mix_extra_failures=mix_extra_failures,
        downtime_declared=spec.declares_downtime,
    )


# --------------------------------------------------------------------------
# Traffic -- the denominator table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TrafficRow:
    day_index: int
    segment: str
    attempts: int
    failures: int

    @property
    def failure_rate(self) -> float:
        return self.failures / self.attempts if self.attempts else 0.0


def build_traffic(
    events: Sequence[RiskEvent],
    seed: int,
    spec: Optional[DegradationSpec] = None,
) -> List[TrafficRow]:
    """Attempt counts per (day, segment), consistent with the observed failures.

    The construction, and the one subtlety in it:

    Attempts are derived from the failures a *quiet* world would have produced --
    so injected **rate-shift** failures are excluded from the derivation, and
    injected **mix-shift** failures are included. That asymmetry is the entire
    semantic difference between the two shifts:

    - rate shift: the denominator does not grow, so ``failures/attempts`` rises.
      Something broke.
    - mix shift: the denominator grows in step, so that segment's own
      ``failures/attempts`` is unchanged and only its *share of total attempts*
      rises. That moves the blended rate. Nothing broke.

    Getting this the wrong way round would make the two shifts indistinguishable
    and the decomposition tool would be measuring nothing at all.
    """
    rng = Random(seed * 104729 + 7)

    observed: Dict[Tuple[int, str], int] = {}
    for event in events:
        key = (day_index(event.detected_at), event.counterparty.segment)
        observed[key] = observed.get(key, 0) + 1

    # The counts the denominator is derived from: observed, minus the rate-shift
    # excess in its own slice.
    quiet: Dict[Tuple[int, str], int] = dict(observed)
    if spec is not None and spec.rate_segment is not None and spec.rate_extra_failures > 0:
        window_keys = [
            (day, spec.rate_segment) for day in spec.days if (day, spec.rate_segment) in quiet
        ]
        total_in_window = sum(quiet[k] for k in window_keys) or 1
        for key in window_keys:
            share = quiet[key] / total_in_window
            take = int(round(spec.rate_extra_failures * share))
            # Never below 1: a slice with zero quiet failures would give an
            # attempts figure of zero and a division by zero downstream.
            quiet[key] = max(1, quiet[key] - take)

    rows: List[TrafficRow] = []
    for (day, segment), failures in sorted(observed.items()):
        base_failures = quiet.get((day, segment), failures)
        rate = BASELINE_FAILURE_RATE[segment]
        jitter = 1.0 + ATTEMPT_JITTER * (rng.random() * 2.0 - 1.0)
        attempts = int(round((base_failures / rate) * jitter))
        # A denominator below its own numerator is not a rounding artefact, it is
        # a failure rate above 100%. Clamped, and the clamp is a real guard: with
        # a large injection on a thin day the arithmetic does get there.
        attempts = max(attempts, failures)
        rows.append(
            TrafficRow(day_index=day, segment=segment, attempts=attempts, failures=failures)
        )
    return rows


# --------------------------------------------------------------------------
# Downtime -- the platform's own declaration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DowntimeRow:
    """Shaped after Razorpay's ``payment.downtime`` entity [A].

    Fields kept: begin/end, method, severity, scope, status. Dropped: the entity
    id and the raw epoch timestamps, because neither survives the canonicality
    screen and neither carries diagnostic weight. Times are day index plus hour,
    the same grain the rest of the agent's world uses.
    """

    day_index_start: int
    hour_start: int
    day_index_end: int
    hour_end: int
    method: str
    severity: str        # low | medium | high
    scope: str           # issuer | psp | network | platform
    entity: str          # a bank/PSP label; low-cardinality
    status: str          # resolved | started


def build_downtime(spec: Optional[DegradationSpec]) -> List[DowntimeRow]:
    """Declared downtimes. Empty unless the spec says the platform noticed.

    An empty list is a *finding*, not missing data, and the tool that serves it
    says so to the agent in as many words. The most common real case is exactly
    this: the elevation is visible in your own data before -- or instead of --
    any upstream declaration.
    """
    if spec is None or not spec.declares_downtime:
        return []
    return [
        DowntimeRow(
            day_index_start=spec.start_day,
            hour_start=0,
            day_index_end=spec.end_day,
            hour_end=23,
            method=spec.downtime_method,
            severity="high",
            scope="issuer",
            entity=(spec.rate_segment or "unknown"),
            status="resolved",
        )
    ]
