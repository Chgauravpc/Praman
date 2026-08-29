"""Simulator v0 -- payment failures, seeded, weighted by the real distribution.

**The weighting is the whole point of this file.** Anti-pattern A2:

    Uniform-sampling the 69 reason codes would put ~1.4% of events on
    bank_technical_error when reality is 35-45%. The envelope's never-retry
    branches would dominate a workload that does not exist, the eval would
    measure fiction, and the memoisation ratio would be wrong because the
    signature distribution would be wrong.

So volume is drawn from the PRD 5.1 buckets, and the codes inside each bucket are
themselves weighted. Two consequences worth stating, because they are what makes
the simulated workload behave like the real one:

- By **volume**, ~90% of events land in retry-eligible classes.
- By **code count**, 45 of 69 codes are not retryable at all.

That divergence is the trap the envelope exists to catch, and a simulator that
smoothed it out would test nothing.

Every event also carries a latent ``self_recovers_at`` -- when this payment would
have succeeded with no intervention whatsoever. In production that counterfactual
is unobservable; here it is known, which is what lets Day 3 check that the
holdout estimator is *unbiased* rather than merely produce a number (PRD 8.2,
invariant I6). Nothing in the agent path can read it: it is stored in a separate
table and excluded from the prompt feature set by construction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import timedelta
from random import Random
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional, Sequence, Tuple

from pramaan import canonical
from pramaan.config import SIM_EPOCH
from pramaan.eval.arms import ArmAssigner as _ArmAssigner
from pramaan.sense.models import Counterparty, LatentTruth, RiskEvent
from pramaan.taxonomy import BY_CODE, CODES_BY_CLASS, REASON_CLASS_POLICY

if TYPE_CHECKING:  # pragma: no cover -- sim.incident imports this module
    from sim.incident import DegradationSpec

# --------------------------------------------------------------------------
# PRD 5.1 -- the failure-reason distribution
# --------------------------------------------------------------------------
#
# Merchant-side distribution of UPI failures from PSP audits. Graded [C] in the
# PRD: directional only, not to be quoted as fact. The *shape* is what matters,
# and it is what drives ACT_WAIT being a first-class action.
#
#   Bank server timeout               35-45%
#   Wrong UPI PIN / attempts exceeded 20-30%
#   Insufficient balance              15-25%
#   Network / connectivity            10-15%
#   Account blocked / deactivated      5-10%
#
# Declared weights sit mid-range, leaving 4% for a long tail the PRD table does
# not cover. That tail is deliberate and it is small on purpose: PRD Appendix A
# is explicit that the long tail of codes is where the damage lives -- retrying a
# risk decline, chasing someone who has already paid -- so a batch containing
# none of it would never exercise the branches that matter most. 4% of 6,000
# events is ~240 cases, enough to measure, and small enough that the five
# published buckets each stay inside their stated range.

PUBLISHED_RANGES: Dict[str, Tuple[float, float]] = {
    "bank_timeout": (0.35, 0.45),
    "wrong_pin": (0.20, 0.30),
    "insufficient_balance": (0.15, 0.25),
    "network": (0.10, 0.15),
    "account_blocked": (0.05, 0.10),
}

# Note that no declared weight sits *on* a range boundary. An earlier draft
# declared network at exactly 0.10, its published floor, and the 6,000-event
# batch duly came out at 0.0923 -- i.e. outside the range the README quotes, for
# no reason other than that a boundary value falls below its floor half the time.
# Every weight now has headroom on both sides, so the published range is a claim
# the batch can actually satisfy.
BUCKET_WEIGHTS: Dict[str, float] = {
    "bank_timeout": 0.37,
    "wrong_pin": 0.24,
    "insufficient_balance": 0.18,
    "network": 0.11,
    "account_blocked": 0.06,
    "long_tail": 0.04,
}

#: Codes within each bucket, weighted. The intra-bucket weights are judgement,
#: not measurement -- what is load-bearing is that one code dominates each bucket
#: the way a real error stream does, rather than the bucket being spread evenly
#: over its codes.
BUCKET_CODES: Dict[str, Sequence[Tuple[str, float]]] = {
    "bank_timeout": (
        ("bank_technical_error", 0.60),
        ("server_error", 0.15),
        ("payment_timed_out", 0.15),
        ("upi_app_technical_error", 0.10),
    ),
    "wrong_pin": (
        ("incorrect_pin", 0.35),
        ("pin_attempts_exceeded", 0.20),
        ("incorrect_otp", 0.15),
        ("otp_expired", 0.10),
        ("otp_attempts_exceeded", 0.08),
        ("authentication_failed", 0.07),
        ("payment_cancelled", 0.05),
    ),
    "insufficient_balance": (("insufficient_funds", 1.00),),
    "network": (
        ("payment_failed", 0.60),
        ("verification_failed", 0.40),
    ),
    "account_blocked": (
        ("debit_instrument_blocked", 0.30),
        ("bank_account_invalid", 0.20),
        ("transaction_on_vpa_restricted", 0.20),
        ("invalid_vpa", 0.15),
        ("pin_not_set", 0.10),
        ("card_expired", 0.05),
    ),
}

#: The long tail, by class. These are the classes where a naive agent does
#: damage: RISK (retrying a risk decline is how a merchant gets penalised),
#: ALREADY_PAID (the humiliation case), MERCHANT_CONFIG and INTEGRATION_BUG
#: (customer contact is simply the wrong target).
LONG_TAIL_CLASS_WEIGHTS: Sequence[Tuple[str, float]] = (
    ("LIMIT", 0.30),
    ("MERCHANT_CONFIG", 0.20),
    ("INTEGRATION_BUG", 0.15),
    ("RISK", 0.15),
    ("ALREADY_PAID", 0.10),
    ("ELIGIBILITY", 0.10),
)

# --------------------------------------------------------------------------
# Latent self-recovery -- the counterfactual
# --------------------------------------------------------------------------
#
# Calibrated so the overall organic recovery rate lands near 30%, which is the
# baseline PRD 8.1 uses for its power calculation. Getting this roughly right
# matters: the whole submission rests on separating the money the agent recovered
# from the money that was coming back anyway.
#
# AUTH_DROPOFF is the highest class, per PRD Appendix A -- a wrong UPI PIN is the
# case where the customer is most likely fixing it themselves inside their
# banking app right now. TECH_TRANSIENT is close behind because the UPI app
# retries on its own. The dead classes are dead: a MERCHANT_CONFIG failure does
# not heal, and neither does a risk decline.

@dataclass(frozen=True)
class SelfRecoveryModel:
    probability: float
    median_lag_seconds: int
    sigma: float


SELF_RECOVERY: Dict[str, SelfRecoveryModel] = {
    "TECH_TRANSIENT": SelfRecoveryModel(0.35, 240, 1.00),        # minutes
    "AUTH_DROPOFF": SelfRecoveryModel(0.42, 540, 1.10),          # minutes
    "FUNDS": SelfRecoveryModel(0.18, 172_800, 0.90),             # ~2 days: payday
    "LIMIT": SelfRecoveryModel(0.15, 86_400, 0.80),              # next day, cap resets
    "INSTRUMENT_DEAD": SelfRecoveryModel(0.02, 259_200, 1.00),   # they re-add a card
    "MERCHANT_CONFIG": SelfRecoveryModel(0.00, 0, 0.0),          # does not heal
    "INTEGRATION_BUG": SelfRecoveryModel(0.00, 0, 0.0),          # does not heal
    "ALREADY_PAID": SelfRecoveryModel(1.00, 0, 0.0),             # already recovered
    "RISK": SelfRecoveryModel(0.00, 0, 0.0),                     # will not heal
    "ELIGIBILITY": SelfRecoveryModel(0.05, 86_400, 0.90),
}

# --------------------------------------------------------------------------
# Amounts and timing
# --------------------------------------------------------------------------
#
# Order amounts are log-normal (PRD 8.1), which is exactly why the money metric
# needs a bootstrap CI rather than a normal approximation (anti-pattern A6).
#
# Two components. The body is everyday retail: median around Rs 1,100. The tail
# is high-value -- subscriptions, B2B, insurance premiums -- median around
# Rs 60,000. Without that second component a 200-event batch would contain no
# band-5 events at all, and the AFA-threshold branches (R2) that motivate the
# band boundaries would never be exercised.

BODY_MU = math.log(1_100.0)
BODY_SIGMA = 1.15
HIGH_VALUE_PROBABILITY = 0.06
HIGH_MU = math.log(60_000.0)
HIGH_SIGMA = 0.90

SEGMENT_WEIGHTS: Sequence[Tuple[str, float]] = (
    ("metro", 0.50),
    ("tier2", 0.30),
    ("tier3", 0.20),
)

#: Hour-of-day weights, IST. Indian consumer payments peak in the evening; the
#: small hours are nearly empty. This shape is what gives hour_bucket real
#: variety, and hour_bucket is a signature field, so a flat profile here would
#: understate the observed signature count.
HOUR_WEIGHTS: Sequence[float] = (
    2, 1, 1, 1, 1, 2,      # 00-05
    4, 6, 8, 9, 9, 8,      # 06-11
    7, 6, 6, 7,            # 12-15
    8, 9, 10, 12, 12, 10,  # 16-21
    7, 4,                  # 22-23
)

DEV_BATCH_SIZE = 200
#: PRD 8.1 chooses this from a power calculation, not by feel: three equal arms
#: of 2,000 clears the 1,319-per-arm requirement to detect a 5pp contrast at
#: alpha=0.05 with 80% power, and leaves headroom for per-event-type analysis.
FULL_BATCH_SIZE = 6_000


# --------------------------------------------------------------------------
# Deterministic primitives
# --------------------------------------------------------------------------
#
# Box-Muller from two uniforms rather than random.normalvariate. The reason is
# narrow but real: this repo's central claim is that a reviewer can reproduce the
# headline number, and normalvariate's internals are a CPython implementation
# detail. random.random() is a specified Mersenne Twister stream, so a normal
# draw built from it by hand reproduces on any Python that ships that stream.


def _standard_normal(rng: Random) -> float:
    u1 = rng.random()
    while u1 <= 0.0:  # log(0) guard; astronomically rare, cheap to exclude
        u1 = rng.random()
    u2 = rng.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _weighted_choice(rng: Random, options: Sequence[Tuple[str, float]]) -> str:
    """Pick by weight, using one random() draw.

    Hand-rolled rather than random.choices so the number of draws per event is
    fixed and obvious. That keeps the stream position predictable, which is what
    makes "same seed reproduces an identical batch" easy to reason about.
    """
    total = sum(w for _, w in options)
    target = rng.random() * total
    upto = 0.0
    for value, weight in options:
        upto += weight
        if target < upto:
            return value
    return options[-1][0]


def _lognormal_paise(rng: Random) -> int:
    if rng.random() < HIGH_VALUE_PROBABILITY:
        mu, sigma = HIGH_MU, HIGH_SIGMA
    else:
        mu, sigma = BODY_MU, BODY_SIGMA
    rupees = math.exp(mu + sigma * _standard_normal(rng))
    # Integer paise, and at least one paisa: a zero-amount risk event is not a
    # risk event.
    return max(1, int(round(rupees * 100)))


# --------------------------------------------------------------------------
# Arm assignment -- stratified, permuted block
# --------------------------------------------------------------------------


#: Arm assignment moved to ``pramaan/eval/arms.py`` on Day 3, and is re-exported
#: here so that ``sim.generate.ArmAssigner`` keeps working for anything that
#: already imported it. It belongs with the experiment rather than the simulator:
#: in production the assigner runs on real detected events and this module does
#: not exist at all.
#:
#: **The move preserves every value**, which is asserted rather than asserted-in-
#: prose. ``tests/test_arms.py`` pins the arm vectors by SHA-256 against figures
#: measured at commit ``3edae60``, before the move: ``e259c0a446ac7ca1`` for the
#: 200-event dev batch (68/67/65) and ``56e5b02860e48e36`` for the 6,000-event
#: full batch (2003/2001/1996). ADR-023 forbids a test that compares a function
#: to its own delegate, so the pin is against recorded constants.
ArmAssigner = _ArmAssigner


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def _pick_code(rng: Random) -> str:
    bucket = _weighted_choice(rng, tuple(BUCKET_WEIGHTS.items()))
    if bucket == "long_tail":
        reason_class = _weighted_choice(rng, LONG_TAIL_CLASS_WEIGHTS)
        codes = CODES_BY_CLASS[reason_class]
        # Uniform *within* the tail class. There is no volume data for these, and
        # inventing intra-class weights would be false precision.
        return codes[int(rng.random() * len(codes)) % len(codes)]
    return _weighted_choice(rng, BUCKET_CODES[bucket])


def _available_actions(reason_class: str, eligibility: str, amount_band: int) -> Tuple[str, ...]:
    """What this adapter can physically offer. The envelope narrows it later.

    Not a policy decision -- a capability list. ACT_WAIT is always available,
    which is the action-space half of PRD 5.1's argument: an agent that cannot
    express "wait, they are probably already retrying" will pay to message people
    who have already paid.
    """
    policy = REASON_CLASS_POLICY[reason_class]
    actions: List[str] = ["ACT_WAIT"]
    if policy.retry_mode in ("immediate", "scheduled"):
        actions.append("ACT_RETRY")
    if reason_class in ("TECH_TRANSIENT", "LIMIT"):
        actions.append("ACT_ROUTE")
    if policy.contact == "allowed" and eligibility in ("silent_and_message", "full"):
        actions.append("ACT_MESSAGE")
        # T3 has a value threshold: a phone call is the least reversible contact
        # there is, so it is only ever on the table above the 5k-15k band.
        if eligibility == "full" and amount_band >= 3:
            actions.append("ACT_VOICE")
    if reason_class == "MERCHANT_CONFIG":
        actions.append("ACT_ALERT_MERCHANT")
    if reason_class == "INTEGRATION_BUG":
        actions.append("ACT_PAGE_ENGINEER")
    if reason_class == "RISK":
        actions.append("ACT_ESCALATE_HUMAN")
    if reason_class == "ALREADY_PAID":
        actions.append("ACT_STOP")
    return tuple(actions)


def _self_recovery(rng: Random, reason_class: str, detected_at_dt) -> LatentTruth:
    model = SELF_RECOVERY[reason_class]
    if model.probability <= 0.0 or rng.random() >= model.probability:
        return LatentTruth(self_recovers_at=None)
    if model.median_lag_seconds == 0:
        lag = 0.0
    else:
        # Log-normal lag: a few recover almost immediately, most take a while,
        # and a long right tail. The median is the calibration target.
        lag = math.exp(
            math.log(model.median_lag_seconds) + model.sigma * _standard_normal(rng)
        )
    recovered_at = detected_at_dt + timedelta(seconds=int(lag))
    return LatentTruth(self_recovers_at=canonical.to_iso(recovered_at))


def _external_ref(rng: Random) -> str:
    """A Razorpay-shaped payment id.

    Present so the invariant has something real to protect: this is exactly the
    kind of value that must never appear in a prompt, and
    tests/test_prompt_canonical.py proves it does not.
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "pay_" + "".join(
        alphabet[int(rng.random() * len(alphabet)) % len(alphabet)] for _ in range(14)
    )


def generate(
    count: int,
    seed: int = 42,
    days: int = 0,
    degradation: "Optional[DegradationSpec]" = None,
    *,
    arms: Optional[_ArmAssigner] = None,
) -> List[RiskEvent]:
    """Generate a seeded batch of payment-failure RiskEvents.

    Deterministic: the same (count, seed, days) reproduces byte-identical events
    on any machine. Sorted by (detected_at, event_id) so ingestion order is the
    store's iteration order, which is what makes the ledger hash reproducible.

    ``degradation`` injects one incident (Day 4, ``sim/incident.py``). It
    defaults to None, and the None path is *exactly* the Day 3 code path: the
    injection happens after this loop finishes, appending events rather than
    perturbing draws, so every figure Day 3 pinned reproduces unchanged. That is
    asserted in ``tests/test_incident.py``, not merely intended -- the cheap way
    to add an incident would have been a conditional inside the loop, and it
    would have shifted the RNG stream for every event after the first branch and
    silently invalidated the arm-vector SHA-256 pins.

    ``arms`` is None for every Day 1-5 caller, which builds its own
    ``ArmAssigner(seed)`` exactly as before -- untouched, so every pinned hash
    in this project is unaffected. Day 6's ``generate_all_types`` is the one
    caller that passes an assigner in, so that a payment sub-batch shares its
    ``(source_type, band, segment)`` strata with the other four adapters'
    sub-batches rather than balancing arms within payment alone.
    """
    if count <= 0:
        raise ValueError("count must be positive")
    if days <= 0:
        # Roughly 30 events a day, so a dev batch spans a week and the full batch
        # a month. Enough calendar spread for day-of-week and hour-bucket variety.
        days = max(1, count // 30)

    rng = Random(seed)
    assigner = arms if arms is not None else ArmAssigner(seed)
    epoch = canonical.parse_iso(SIM_EPOCH)
    horizon_seconds = days * 86_400

    events: List[RiskEvent] = []
    # A counterparty pool smaller than the batch, so counterparties repeat. That
    # is what makes contact budgets (S2) and the promise state machine (Day 6)
    # testable at all -- a stream of one-shot payers would never hit a cap.
    pool_size = max(1, count // 3)

    for index in range(count):
        code = _pick_code(rng)
        reason_class = BY_CODE[code].reason_class
        amount_paise = _lognormal_paise(rng)
        segment = _weighted_choice(rng, SEGMENT_WEIGHTS)

        day_offset = int(rng.random() * days)
        hour = _weighted_choice(
            rng, tuple((str(h), w) for h, w in enumerate(HOUR_WEIGHTS))
        )
        minute = int(rng.random() * 60)
        second = int(rng.random() * 60)
        detected_dt = epoch + timedelta(
            days=day_offset, hours=int(hour), minutes=minute, seconds=second
        )
        detected_at = canonical.to_iso(detected_dt)

        counterparty = Counterparty(
            id="cp_%05d" % (int(rng.random() * pool_size) % pool_size),
            kind="payer",
            segment=segment,
        )

        band = canonical.amount_band(amount_paise)
        hour_bucket_value = canonical.hour_bucket(detected_at)
        # A payment-failure retry link is a *service* message, not marketing and
        # not debt collection (PRD 3.1). Day 1 ships payment failures only, so
        # legal_context is uniformly "service" today; collection arrives with the
        # receivables adapter on Day 6. Stating that is more honest than
        # sprinkling contexts across a stream that does not have them.
        legal_context = "service"
        eligibility = canonical.channel_eligibility(
            reason_class, legal_context, hour_bucket_value
        )

        event = RiskEvent(
            event_id="evt_%04d_%06d" % (seed, index),
            source_type="payment",
            amount_at_risk_paise=amount_paise,
            counterparty=counterparty,
            detected_at=detected_at,
            decay_profile=canonical.DECAY_BY_SOURCE_TYPE["payment"],
            cause_signal=code,
            legal_context=legal_context,
            available_actions=_available_actions(reason_class, eligibility, band),
            arm=assigner.assign("payment", band, segment),
            external_ref=_external_ref(rng),
            latent=_self_recovery(rng, reason_class, detected_dt),
        )
        events.append(event)

    # Webhooks arrive out of order in production (NFR-5); a sorted stream here is
    # the store's canonical order, and tests/test_idempotent_replay.py shuffles it
    # to prove ingestion does not depend on arrival order.
    events.sort(key=lambda e: (e.detected_at, e.event_id))

    # Day 3: fill in the capability/intent latents. Done here, unconditionally,
    # rather than left to the caller -- because a caller who forgot would get
    # events whose capability_clears_at is None, which the oracle reads as "this
    # block never clears", which makes every intervention fail silently and the
    # incremental figure zero. A wrong number that looks like a real one is the
    # worst available outcome, so there is no unenriched path to forget.
    #
    # The import is deferred: sim.latent reads SELF_RECOVERY out of this module,
    # so a module-level import either way round would be circular.
    from sim.latent import enrich

    # The incident, if one was asked for. After the base loop and after the
    # sort, so the base stream is untouched; before enrichment, so injected
    # events get their capability/intent latents like any other event. An
    # injected event without them would be read by the oracle as "this block
    # never clears", making the incident invisible to every arm.
    if degradation is not None:
        from sim.incident import inject

        events, _truth = inject(events, degradation, seed, arms=assigner)

    events = [replace(e, latent=enrich(e, seed)) for e in events]
    return events


def dev_batch(seed: int = 42) -> List[RiskEvent]:
    """The 200-event batch everything is built and debugged against.

    BUILD-PLAN 1.7 rule 1: never iterate on the full batch. The full 6,000-event
    run happens once, on Day 7, and its cache is committed.
    """
    return generate(DEV_BATCH_SIZE, seed=seed)


def full_batch(seed: int = 42) -> List[RiskEvent]:
    return generate(FULL_BATCH_SIZE, seed=seed)


#: The investigator's batch. Larger than the 200-event dev batch, and the reason
#: is rows rather than tokens: one injected incident costs one investigation
#: either way, and SQL is free. See ``sim.incident.DEV_INCIDENT``.
INVESTIGATE_BATCH_SIZE = 1_200
INVESTIGATE_BATCH_DAYS = 8


def dev_batch_degraded(
    seed: int = 42,
    count: int = INVESTIGATE_BATCH_SIZE,
    days: int = INVESTIGATE_BATCH_DAYS,
):
    """The investigator's batch: a clean stream plus one injected incident.

    Returns ``(events, truth)``.

    A *separate* batch, not a replacement for ``dev_batch``. Day 3's figures are
    pinned against ``dev_batch`` and its 68/67/65 arm vector; the investigator
    needs a world where something is actually wrong. Two batches -- one pinned,
    one degraded -- is the only arrangement in which both statements stay true.
    """
    from sim.incident import DEV_INCIDENT, truth_for

    events = generate(count, seed=seed, days=days, degradation=DEV_INCIDENT)
    return events, truth_for(DEV_INCIDENT)


# --------------------------------------------------------------------------
# Observed distribution -- reported, and asserted in tests
# --------------------------------------------------------------------------

CODE_TO_BUCKET: Dict[str, str] = {}
for _bucket, _codes in BUCKET_CODES.items():
    for _code, _ in _codes:
        CODE_TO_BUCKET[_code] = _bucket


def bucket_of(code: str) -> str:
    """Which PRD 5.1 bucket a code belongs to, or long_tail."""
    return CODE_TO_BUCKET.get(code, "long_tail")


def bucket_shares(events: Sequence[RiskEvent]) -> Dict[str, float]:
    counts: Dict[str, int] = {b: 0 for b in BUCKET_WEIGHTS}
    for event in events:
        counts[bucket_of(event.cause_signal)] += 1
    total = len(events) or 1
    return {b: n / total for b, n in counts.items()}


def class_shares(events: Sequence[RiskEvent]) -> Dict[str, float]:
    counts: Dict[str, int] = {}
    for event in events:
        counts[event.reason_class] = counts.get(event.reason_class, 0) + 1
    total = len(events) or 1
    return {c: n / total for c, n in sorted(counts.items())}


def self_recovery_rate(events: Sequence[RiskEvent]) -> float:
    """Share of events that would have recovered with no intervention at all.

    The number that makes gross recovered-rupees a category error (HANDOFF 1).
    """
    if not events:
        return 0.0
    recovering = sum(
        1 for e in events if e.latent is not None and e.latent.self_recovers
    )
    return recovering / len(events)


def band_histogram(events: Sequence[RiskEvent]) -> Dict[int, int]:
    counts = {band: 0 for band in canonical.AMOUNT_BANDS}
    for event in events:
        counts[event.amount_band] += 1
    return counts


def iter_shuffled(events: Sequence[RiskEvent], seed: int) -> Iterator[RiskEvent]:
    """The same events in a seeded arrival order, and duplicated.

    Models what a webhook endpoint actually receives: at-least-once delivery,
    out of order (NFR-5). Used by tests/test_idempotent_replay.py.
    """
    shuffled = list(events)
    Random(seed ^ 0xBEEF).shuffle(shuffled)
    return iter(shuffled)


# ============================================================================
# Day 6 -- the four adapters that complete the five event types (PRD 3, 5)
# ============================================================================
#
# Each function below draws a raw, adapter-shaped signal -- the same kind of
# seeded, weighted, deterministic thing the payment loop above draws -- and
# hands it to the matching normaliser in ``pramaan.sense.adapters``, exactly
# the step a real webhook would go through. Nothing here re-derives
# reason_class, decay_profile or legal_context: that decision belongs to the
# adapter, once, and is asserted by ``tests/test_sense_adapters.py``.
#
# Deliberately **not** wired into ``generate()``/``dev_batch()``/``full_batch()``.
# Every pinned hash in this project -- the arm vectors, the golden ledger, the
# 22.0x memoisation ratio -- is measured against the payment-only stream those
# three produce, and folding four more source types into that loop would
# invalidate all of it for a batch nobody asked for (the ADR-033 append-not-
# perturb precedent). ``generate_all_types`` below is new and purely additive.

from pramaan.sense.adapters import checkout as _checkout_adapter
from pramaan.sense.adapters import mandate as _mandate_adapter
from pramaan.sense.adapters import receivable as _receivable_adapter
from pramaan.sense.adapters import subscription as _subscription_adapter

#: Checkout cart values skew lower than a payment's own amount distribution --
#: this is pre-conversion cart value, not a completed order.
CHECKOUT_BODY_MU = math.log(900.0)
CHECKOUT_BODY_SIGMA = 1.05

#: PRD 5: which stage the customer abandoned at. Weighted so that
#: method-selection -- the UX problem -- is the plurality, matching the
#: intuition that most drop-off happens before a customer has committed to a
#: method at all.
CHECKOUT_STAGE_WEIGHTS: Sequence[Tuple[str, float]] = (
    ("checkout_stage_method_selection", 0.45),
    ("checkout_stage_otp_entry", 0.35),
    ("checkout_stage_processing", 0.20),
)

SUBSCRIPTION_INSTALMENT_MU = math.log(650.0)
SUBSCRIPTION_INSTALMENT_SIGMA = 0.55

#: Razorpay auto-retries the day after ``pending`` and halts once retries are
#: exhausted (PRD 5, [A]) -- so most pending subscriptions are on their first
#: attempt, a smaller share on their second, fewer still on the last one
#: before the intervention window closes.
SUBSCRIPTION_RETRY_ATTEMPT_WEIGHTS: Sequence[Tuple[int, float]] = (
    (1, 0.55),
    (2, 0.30),
    (3, 0.15),
)

MANDATE_DEBIT_MU = math.log(1_800.0)
MANDATE_DEBIT_SIGMA = 0.60

#: B2B invoices: far larger and far more heavy-tailed than a consumer payment.
RECEIVABLE_MU = math.log(45_000.0)
RECEIVABLE_SIGMA = 0.95
RECEIVABLE_DAYS_OVERDUE_MIN = 1
RECEIVABLE_DAYS_OVERDUE_MAX = 60

#: The share of overdue receivables already reconciled by the time recovery
#: would otherwise fire -- deliberately small and deliberately non-zero,
#: because it is the population S1 exists to protect (Day 6 DoD: "a paid
#: invoice is never chased").
RECEIVABLE_RECONCILED_PROBABILITY = 0.08

#: How the 6,000-event full batch (and the 200-event dev batch) is split
#: across the five source types, once ``generate_all_types`` does the split.
#: Payment stays the plurality -- it is the type every published Day 1-5
#: figure is measured on, and PRD 5.1's own volume argument is about payment
#: failures specifically -- with the other four sized so each clears a few
#: hundred events on the full batch: enough to report a per-type count that
#: means something, not enough to dilute the payment-only story those figures
#: already tell.
SOURCE_TYPE_WEIGHTS: Sequence[Tuple[str, float]] = (
    ("payment", 0.55),
    ("checkout", 0.20),
    ("subscription", 0.11),
    ("mandate", 0.08),
    ("receivable", 0.06),
)


def _detected_at_iso(rng: Random, epoch, days: int) -> str:
    """The same day/hour/minute/second draw the payment loop uses (above),
    isolated here so every Day 6 adapter's timestamp shares one hour-of-day
    shape (``HOUR_WEIGHTS``) without re-deriving payment-specific logic."""
    day_offset = int(rng.random() * days)
    hour = _weighted_choice(rng, tuple((str(h), w) for h, w in enumerate(HOUR_WEIGHTS)))
    minute = int(rng.random() * 60)
    second = int(rng.random() * 60)
    dt = epoch + timedelta(
        days=day_offset, hours=int(hour), minutes=minute, seconds=second
    )
    return canonical.to_iso(dt)


def _lognormal_paise_with(rng: Random, mu: float, sigma: float) -> int:
    value = math.exp(mu + sigma * _standard_normal(rng))
    return max(1, int(round(value * 100)))


def _counterparty_id(rng: Random, prefix: str, pool_size: int) -> str:
    return "%s_%05d" % (prefix, int(rng.random() * pool_size) % pool_size)


def _attach_self_recovery(event: RiskEvent, rng: Random) -> RiskEvent:
    """Draw ``self_recovers_at`` for an adapter-built event, same as the
    payment loop's own inline draw. Without this every Day 6 event would
    reach ``sim.latent.enrich`` with ``latent=None`` and raise there -- an
    adapter event needs the same counterfactual a payment event gets, or the
    whole measurement pipeline (outcomes, potential_outcomes, metrics) has
    nothing to resolve it against.
    """
    detected_dt = canonical.parse_iso(event.detected_at)
    return replace(event, latent=_self_recovery(rng, event.reason_class, detected_dt))


def _enrich_batch(events: List[RiskEvent], seed: int) -> List[RiskEvent]:
    """The same Day 3 capability/intent enrichment ``generate()`` applies,
    factored out so every Day 6 generator calls it identically."""
    from sim.latent import enrich

    return [replace(e, latent=enrich(e, seed)) for e in events]


def generate_checkout(
    count: int, seed: int = 42, days: int = 0, *, arms: Optional[_ArmAssigner] = None
) -> List[RiskEvent]:
    """A seeded batch of abandoned-checkout beacons, normalised via the
    checkout adapter. Deterministic for a fixed (count, seed, days), on its
    own independent ``Random`` stream (seed XORed) so it never shares draws
    with ``generate()``'s payment stream."""
    if count <= 0:
        raise ValueError("count must be positive")
    days = days if days > 0 else max(1, count // 30)
    rng = Random(seed ^ 0xC4EC)
    assigner = arms if arms is not None else ArmAssigner(seed)
    epoch = canonical.parse_iso(SIM_EPOCH)
    pool_size = max(1, count // 3)

    events: List[RiskEvent] = []
    for index in range(count):
        stage = _weighted_choice(rng, CHECKOUT_STAGE_WEIGHTS)
        amount_paise = _lognormal_paise_with(rng, CHECKOUT_BODY_MU, CHECKOUT_BODY_SIGMA)
        segment = _weighted_choice(rng, SEGMENT_WEIGHTS)
        detected_at = _detected_at_iso(rng, epoch, days)
        counterparty_id = _counterparty_id(rng, "cp", pool_size)
        band = canonical.amount_band(amount_paise)
        arm = assigner.assign("checkout", band, segment)
        beacon = _checkout_adapter.CheckoutBeacon(
            order_id="ord_%d_%06d" % (seed, index),
            counterparty_id=counterparty_id,
            segment=segment,
            amount_paise=amount_paise,
            detected_at=detected_at,
            stage=stage,
            arm=arm,
        )
        event = _attach_self_recovery(_checkout_adapter.build_event(beacon), rng)
        events.append(event)
    return _enrich_batch(events, seed)


def generate_subscription(
    count: int, seed: int = 42, days: int = 0, *, arms: Optional[_ArmAssigner] = None
) -> List[RiskEvent]:
    """A seeded batch of ``subscription.pending`` signals, normalised via the
    subscription adapter."""
    if count <= 0:
        raise ValueError("count must be positive")
    days = days if days > 0 else max(1, count // 30)
    rng = Random(seed ^ 0x5E5B)
    assigner = arms if arms is not None else ArmAssigner(seed)
    epoch = canonical.parse_iso(SIM_EPOCH)
    pool_size = max(1, count // 3)

    events: List[RiskEvent] = []
    for index in range(count):
        attempt = int(_weighted_choice(rng, tuple((str(a), w) for a, w in SUBSCRIPTION_RETRY_ATTEMPT_WEIGHTS)))
        amount_paise = _lognormal_paise_with(
            rng, SUBSCRIPTION_INSTALMENT_MU, SUBSCRIPTION_INSTALMENT_SIGMA
        )
        segment = _weighted_choice(rng, SEGMENT_WEIGHTS)
        detected_at = _detected_at_iso(rng, epoch, days)
        counterparty_id = _counterparty_id(rng, "cp", pool_size)
        band = canonical.amount_band(amount_paise)
        arm = assigner.assign("subscription", band, segment)
        raw = _subscription_adapter.SubscriptionPendingEvent(
            subscription_id="sub_%d_%06d" % (seed, index),
            counterparty_id=counterparty_id,
            segment=segment,
            instalment_amount_paise=amount_paise,
            detected_at=detected_at,
            retry_attempt=attempt,
            arm=arm,
        )
        event = _attach_self_recovery(_subscription_adapter.build_event(raw), rng)
        events.append(event)
    return _enrich_batch(events, seed)


def generate_mandate(
    count: int, seed: int = 42, days: int = 0, *, arms: Optional[_ArmAssigner] = None
) -> List[RiskEvent]:
    """A seeded batch of due e-mandate debits, normalised via the mandate
    adapter."""
    if count <= 0:
        raise ValueError("count must be positive")
    days = days if days > 0 else max(1, count // 30)
    rng = Random(seed ^ 0x1A4D)
    assigner = arms if arms is not None else ArmAssigner(seed)
    epoch = canonical.parse_iso(SIM_EPOCH)
    pool_size = max(1, count // 3)

    events: List[RiskEvent] = []
    for index in range(count):
        amount_paise = _lognormal_paise_with(rng, MANDATE_DEBIT_MU, MANDATE_DEBIT_SIGMA)
        segment = _weighted_choice(rng, SEGMENT_WEIGHTS)
        detected_at = _detected_at_iso(rng, epoch, days)
        counterparty_id = _counterparty_id(rng, "cp", pool_size)
        band = canonical.amount_band(amount_paise)
        arm = assigner.assign("mandate", band, segment)
        raw = _mandate_adapter.MandateDebitDue(
            mandate_id="mnd_%d_%06d" % (seed, index),
            counterparty_id=counterparty_id,
            segment=segment,
            debit_amount_paise=amount_paise,
            detected_at=detected_at,
            arm=arm,
        )
        event = _attach_self_recovery(_mandate_adapter.build_event(raw), rng)
        events.append(event)
    return _enrich_batch(events, seed)


def generate_receivable(
    count: int, seed: int = 42, days: int = 0, *, arms: Optional[_ArmAssigner] = None
) -> List[RiskEvent]:
    """A seeded batch of overdue B2B invoices, normalised via the receivable
    adapter. A small, deliberate share arrive already reconciled -- see
    ``RECEIVABLE_RECONCILED_PROBABILITY``."""
    if count <= 0:
        raise ValueError("count must be positive")
    days = days if days > 0 else max(1, count // 30)
    rng = Random(seed ^ 0x7B0B)
    assigner = arms if arms is not None else ArmAssigner(seed)
    epoch = canonical.parse_iso(SIM_EPOCH)
    pool_size = max(1, count // 3)

    events: List[RiskEvent] = []
    for index in range(count):
        amount_paise = _lognormal_paise_with(rng, RECEIVABLE_MU, RECEIVABLE_SIGMA)
        segment = _weighted_choice(rng, SEGMENT_WEIGHTS)
        detected_at = _detected_at_iso(rng, epoch, days)
        counterparty_id = _counterparty_id(rng, "biz", pool_size)
        days_overdue = RECEIVABLE_DAYS_OVERDUE_MIN + int(
            rng.random() * (RECEIVABLE_DAYS_OVERDUE_MAX - RECEIVABLE_DAYS_OVERDUE_MIN)
        )
        reconciled = rng.random() < RECEIVABLE_RECONCILED_PROBABILITY
        band = canonical.amount_band(amount_paise)
        arm = assigner.assign("receivable", band, segment)
        raw = _receivable_adapter.ReceivableSignal(
            invoice_id="inv_%d_%06d" % (seed, index),
            counterparty_id=counterparty_id,
            segment=segment,
            invoice_amount_paise=amount_paise,
            detected_at=detected_at,
            days_overdue=days_overdue,
            arm=arm,
            reconciled=reconciled,
        )
        event = _attach_self_recovery(_receivable_adapter.build_event(raw), rng)
        events.append(event)
    return _enrich_batch(events, seed)


GENERATORS_BY_SOURCE_TYPE = {
    "checkout": generate_checkout,
    "subscription": generate_subscription,
    "mandate": generate_mandate,
    "receivable": generate_receivable,
}


def generate_all_types(
    total_count: int, seed: int = 42, days: int = 0
) -> List[RiskEvent]:
    """All five event types in one batch, split by ``SOURCE_TYPE_WEIGHTS``.

    A single ``ArmAssigner(seed)`` is shared across every source type, so the
    three-way balance stratifies on ``(source_type, band, segment)`` across
    the whole batch -- the same guarantee ``generate()`` gives payment alone,
    extended rather than duplicated.

    This is additive, not a replacement: ``generate()``/``dev_batch()``/
    ``full_batch()`` are untouched, and every hash Days 1-5 pinned against
    them still reproduces (``tests/test_arms.py``, ``tests/test_incident.py``).
    """
    if total_count <= 0:
        raise ValueError("total_count must be positive")
    days = days if days > 0 else max(1, total_count // 30)

    counts: Dict[str, int] = {}
    remaining = total_count
    types = [t for t, _ in SOURCE_TYPE_WEIGHTS]
    for source_type, weight in SOURCE_TYPE_WEIGHTS[:-1]:
        n = int(round(total_count * weight))
        counts[source_type] = n
        remaining -= n
    counts[types[-1]] = max(0, remaining)  # the last type absorbs the rounding remainder

    assigner = ArmAssigner(seed)
    events: List[RiskEvent] = []
    if counts["payment"] > 0:
        events.extend(generate(counts["payment"], seed=seed, days=days, arms=assigner))
    for source_type, generator in GENERATORS_BY_SOURCE_TYPE.items():
        if counts[source_type] > 0:
            events.extend(generator(counts[source_type], seed=seed, days=days, arms=assigner))

    events.sort(key=lambda e: (e.detected_at, e.event_id))
    return events


def per_type_counts(events: Sequence[RiskEvent]) -> Dict[str, int]:
    """Per-``source_type`` counts, in ``canonical.SOURCE_TYPES`` order --
    what the Day 6 DoD's "per-type counts print" refers to."""
    counts = {source_type: 0 for source_type in canonical.SOURCE_TYPES}
    for event in events:
        counts[event.source_type] += 1
    return counts


def dev_batch_all_types(seed: int = 42) -> List[RiskEvent]:
    """The 200-event, five-source-type batch: breadth's own dev batch."""
    return generate_all_types(DEV_BATCH_SIZE, seed=seed)


def full_batch_all_types(seed: int = 42) -> List[RiskEvent]:
    """The 6,000-event, five-source-type batch (Day 6 DoD: "scale to the
    6,000-event full batch")."""
    return generate_all_types(FULL_BATCH_SIZE, seed=seed)
