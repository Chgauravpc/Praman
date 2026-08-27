"""Confidence intervals for the headline numbers. BCa, hand-rolled.

**Why not a normal approximation.** Order amounts in this system are log-normal
(``sim/generate.py``), so incremental rupees-per-event is a difference in means
of a heavy-tailed variable. A symmetric interval on skewed data is visibly wrong
to anyone who checks: it puts equal mass either side of the point estimate when
the sampling distribution is plainly not symmetric, and on a batch with a few
band-5 events it can even produce a lower bound that no resample achieves. PRD
8.1 calls this out as anti-pattern A6, and a suspiciously tight rupee interval on
heavy-tailed data is one of the few things a reviewer can falsify from the
printed output alone.

**Why BCa rather than plain percentile.** The percentile bootstrap is honest but
biased when the statistic's sampling distribution is skewed or its standard error
varies with its value -- both true here. BCa corrects for exactly those two
things, with two extra quantities:

``z0`` -- **bias correction.** The normal quantile of the share of resamples
falling below the point estimate. If the bootstrap distribution is centred off
the estimate, this shifts the interval back.

``a`` -- **acceleration.** How fast the standard error changes with the parameter,
estimated from the jackknife's third moment. This is what makes the interval
asymmetric in the right direction rather than merely wide.

Reference: Efron & Tibshirani, *An Introduction to the Bootstrap* (1993), ch. 14;
DiCiccio & Efron, *Bootstrap Confidence Intervals*, Statist. Sci. 11(3), 1996.

**Why hand-rolled.** ADR-001: no numpy, no scipy. The whole build is SQLite plus
the standard library, so a reviewer can ``git clone && make demo`` with three pip
packages. BCa is about forty lines and the inverse normal CDF about fifteen, and
having written them means ``tests/test_bootstrap.py`` can check them against
closed-form answers -- which matters far more here than the dependency question,
because an interval nobody has verified is worse than no interval at all.

**Determinism.** Resampling is seeded, and the seed is derived from the batch
rather than from a clock. Invariant I8 diffs two ``make demo`` runs byte for byte,
so a bootstrap that drifted between runs would fail the build.

**The statistics are all ratios of sums**, which is what makes this fast enough
in pure Python and exact enough for the jackknife. A recovery rate is
``sum(recovered) / n``; a value share is ``sum(recovered_paise) /
sum(at_risk_paise)``; money per event is ``sum(recovered_paise) / n``. So one pass
per resample accumulates three sums and every statistic falls out, and
leave-one-out is arithmetic rather than a re-evaluation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from random import Random
from typing import Callable, Dict, List, Optional, Sequence, Tuple

#: 10,000, per PRD 8.1. Enough that the 2.5th and 97.5th percentiles are stable
#: to about a tenth of a percentage point, which is finer than anything reported.
DEFAULT_RESAMPLES = 10_000

#: Reduced count for the sensitivity sweep, where four extra scenarios each need
#: an interval and the point of the table is the *direction* of movement rather
#: than a third significant figure. Always printed alongside the number so the
#: reader knows which of the two produced it.
SENSITIVITY_RESAMPLES = 2_000


# --------------------------------------------------------------------------
# Normal CDF and its inverse
# --------------------------------------------------------------------------


def norm_cdf(x: float) -> float:
    """Phi(x). ``math.erf`` is exact enough that nothing else is needed."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


#: Acklam's rational approximation to the inverse normal CDF, with the standard
#: coefficients. Relative error below 1.15e-9 over the whole range, which is
#: several orders finer than the resample noise it is applied to.
_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_P_LOW = 0.02425


def norm_ppf(p: float) -> float:
    """Phi inverse. Raises on 0 or 1 rather than returning an infinity.

    Returning +/-inf would propagate silently into a percentile index and produce
    an interval bound of 0 or n, which looks like a real answer. A bootstrap that
    has put every resample on one side of the estimate is a broken bootstrap and
    should say so.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(
            "norm_ppf needs 0 < p < 1, got %r. p at a boundary means every "
            "resample fell on one side of the estimate, which is a degenerate "
            "bootstrap rather than a wide interval." % p
        )
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        return (
            ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]
        ) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    if p <= 1.0 - _P_LOW:
        q = p - 0.5
        r = q * q
        return (
            (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5])
            * q
            / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(
        ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]
    ) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)


# --------------------------------------------------------------------------
# Samples and statistics
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmSample:
    """One arm's per-event vectors. Three parallel lists, same length, same order.

    Deliberately three flat lists rather than a list of objects: the resampling
    loop indexes them a hundred million times on the full batch, and
    ``sum(map(list.__getitem__, idx))`` runs in C where a comprehension over
    dataclasses does not.
    """

    #: 1.0 if the event recovered inside the observation window, else 0.0.
    recovered: Tuple[float, ...]

    #: Amount recovered, integer paise as float. Zero if it did not recover.
    recovered_paise: Tuple[float, ...]

    #: Amount at risk, integer paise as float. The denominator of the
    #: value-weighted figure.
    at_risk_paise: Tuple[float, ...]

    def __post_init__(self) -> None:
        n = len(self.recovered)
        if not (len(self.recovered_paise) == len(self.at_risk_paise) == n):
            raise ValueError("ArmSample vectors must be the same length")

    def __len__(self) -> int:
        return len(self.recovered)

    @property
    def totals(self) -> Tuple[float, float, float]:
        return (
            float(sum(self.recovered)),
            float(sum(self.recovered_paise)),
            float(sum(self.at_risk_paise)),
        )


#: A statistic is a function of (sum_recovered, sum_recovered_paise,
#: sum_at_risk_paise, n) -> float. Keeping them in this form is what lets one
#: resampling pass serve every statistic and lets the jackknife be arithmetic.
Statistic = Callable[[float, float, float, int], float]


def rate(s_rec: float, s_val: float, s_risk: float, n: int) -> float:
    """Event-weighted recovery rate. A proportion of **events**."""
    return s_rec / n if n else 0.0


def value_share(s_rec: float, s_val: float, s_risk: float, n: int) -> float:
    """Value-weighted recovery. A proportion of **rupees at risk**.

    PRD 10.2 is explicit that this and ``rate`` are different quantities under
    heavy tails and must never be quoted without saying which. They are separate
    functions here so a caller cannot accidentally pass one where the other
    belongs.
    """
    return s_val / s_risk if s_risk else 0.0


def money_per_event(s_rec: float, s_val: float, s_risk: float, n: int) -> float:
    """Rupees recovered per at-risk event, in paise. The money metric."""
    return s_val / n if n else 0.0


STATISTICS: Dict[str, Statistic] = {
    "rate": rate,
    "value_share": value_share,
    "money_per_event": money_per_event,
}

#: Which statistics are proportions (reported in percentage points) and which are
#: money (reported in rupees). The distinction drives formatting *and* the DoD
#: check that the money interval is wider than the rate interval.
STATISTIC_UNITS: Dict[str, str] = {
    "rate": "pp",
    "value_share": "pp",
    "money_per_event": "paise",
}


# --------------------------------------------------------------------------
# Intervals
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """A point estimate and an interval, carrying how it was produced."""

    point: float
    low: float
    high: float
    method: str
    resamples: int
    confidence: float = 0.95

    #: Set when BCa was requested but could not be computed, with the reason.
    #: Never silently downgraded: a percentile interval labelled BCa would be a
    #: lie about the method, and the method is the part being trusted.
    fallback_reason: Optional[str] = None

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0.0 or self.high < 0.0

    def scaled(self, factor: float) -> "Interval":
        """The same interval in different units. Order-preserving."""
        low, high = self.low * factor, self.high * factor
        if low > high:
            low, high = high, low
        return Interval(
            point=self.point * factor,
            low=low,
            high=high,
            method=self.method,
            resamples=self.resamples,
            confidence=self.confidence,
            fallback_reason=self.fallback_reason,
        )


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence."""
    if not sorted_values:
        raise ValueError("no resamples")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _resample_totals(
    sample: ArmSample, rng: Random, out: List[Tuple[float, float, float]], draws: int
) -> None:
    """Append ``draws`` bootstrap totals for one arm.

    One index list per replicate, then three C-level sums over it. Benchmarked
    against an explicit inner loop and a packed-tuple loop; this form is roughly
    twice as fast as either, which is what keeps the 6,000-event run at a few
    seconds rather than a minute.
    """
    n = len(sample)
    if n == 0:
        out.extend((0.0, 0.0, 0.0) for _ in range(draws))
        return
    g_rec = sample.recovered.__getitem__
    g_val = sample.recovered_paise.__getitem__
    g_risk = sample.at_risk_paise.__getitem__
    random_ = rng.random
    for _ in range(draws):
        idx = [int(random_() * n) for _ in range(n)]
        out.append(
            (sum(map(g_rec, idx)), sum(map(g_val, idx)), sum(map(g_risk, idx)))
        )


def _jackknife_contrasts(
    control: ArmSample, treatment: ArmSample, statistic: Statistic
) -> List[float]:
    """Leave-one-out values of the contrast, over the pooled observations.

    For a two-sample contrast the acceleration is estimated from the pooled
    empirical influence values -- drop one observation from whichever arm it
    belongs to, recompute the contrast, and take the third moment of the result
    (DiCiccio & Efron 1996 §3). Each evaluation is O(1) because every statistic
    is a ratio of sums, so this is n_A + n_B arithmetic operations rather than
    n_A + n_B re-evaluations.
    """
    c_rec, c_val, c_risk = control.totals
    t_rec, t_val, t_risk = treatment.totals
    nc, nt = len(control), len(treatment)
    out: List[float] = []

    if nc > 1:
        base_t = statistic(t_rec, t_val, t_risk, nt)
        for i in range(nc):
            out.append(
                base_t
                - statistic(
                    c_rec - control.recovered[i],
                    c_val - control.recovered_paise[i],
                    c_risk - control.at_risk_paise[i],
                    nc - 1,
                )
            )
    if nt > 1:
        base_c = statistic(c_rec, c_val, c_risk, nc)
        for i in range(nt):
            out.append(
                statistic(
                    t_rec - treatment.recovered[i],
                    t_val - treatment.recovered_paise[i],
                    t_risk - treatment.at_risk_paise[i],
                    nt - 1,
                )
                - base_c
            )
    return out


def _acceleration(jackknife: Sequence[float]) -> Optional[float]:
    """BCa's ``a``, from the jackknife's third moment. None if undefined."""
    if len(jackknife) < 2:
        return None
    mean = sum(jackknife) / len(jackknife)
    num = sum((mean - v) ** 3 for v in jackknife)
    den = sum((mean - v) ** 2 for v in jackknife)
    if den <= 0.0:
        return None
    return num / (6.0 * den ** 1.5)


def bootstrap_contrast(
    control: ArmSample,
    treatment: ArmSample,
    statistic: Statistic,
    *,
    seed: int,
    resamples: int = DEFAULT_RESAMPLES,
    method: str = "bca",
    confidence: float = 0.95,
) -> Interval:
    """A CI for ``statistic(treatment) - statistic(control)``.

    Both arms are resampled **independently**, which is the correct scheme for a
    two-sample design: the arms are separate random samples, not paired
    observations, so resampling them jointly would understate the variance of the
    difference.
    """
    if resamples <= 0:
        raise ValueError("resamples must be positive")
    if method not in ("bca", "percentile"):
        raise ValueError("method must be bca or percentile, got %r" % method)

    nc, nt = len(control), len(treatment)
    c_tot, t_tot = control.totals, treatment.totals
    point = statistic(*t_tot, nt) - statistic(*c_tot, nc)

    if nc == 0 or nt == 0:
        return Interval(
            point=point,
            low=point,
            high=point,
            method="degenerate",
            resamples=0,
            confidence=confidence,
            fallback_reason="an arm is empty; there is nothing to resample",
        )

    # One shared RNG, control then treatment per replicate, so the stream is a
    # deterministic function of (seed, resamples, n_c, n_t) alone.
    rng = Random(seed)
    control_totals: List[Tuple[float, float, float]] = []
    treatment_totals: List[Tuple[float, float, float]] = []
    _resample_totals(control, rng, control_totals, resamples)
    _resample_totals(treatment, rng, treatment_totals, resamples)

    replicates = [
        statistic(*treatment_totals[b], nt) - statistic(*control_totals[b], nc)
        for b in range(resamples)
    ]
    replicates.sort()

    alpha = (1.0 - confidence) / 2.0
    fallback: Optional[str] = None

    if method == "bca":
        below = sum(1 for v in replicates if v < point)
        share = below / resamples
        acceleration = _acceleration(
            _jackknife_contrasts(control, treatment, statistic)
        )
        if share <= 0.0 or share >= 1.0:
            fallback = (
                "every resample fell on one side of the point estimate, so the "
                "bias correction z0 is undefined"
            )
        elif acceleration is None:
            fallback = "the jackknife variance is zero, so acceleration is undefined"
        else:
            z0 = norm_ppf(share)
            z_lo, z_hi = norm_ppf(alpha), norm_ppf(1.0 - alpha)
            adjusted = []
            for z in (z_lo, z_hi):
                denominator = 1.0 - acceleration * (z0 + z)
                if abs(denominator) < 1e-12:
                    adjusted = []
                    fallback = (
                        "the BCa denominator 1 - a(z0+z) vanished, which makes the "
                        "adjusted percentile undefined"
                    )
                    break
                adjusted.append(norm_cdf(z0 + (z0 + z) / denominator))
            if adjusted:
                q_lo, q_hi = adjusted
                # Clamp rather than fail: with a large acceleration the adjusted
                # quantile can land outside (0,1), and the honest reading is that
                # the interval reaches the extreme resample.
                q_lo = min(max(q_lo, 0.0), 1.0)
                q_hi = min(max(q_hi, 0.0), 1.0)
                if q_lo > q_hi:
                    q_lo, q_hi = q_hi, q_lo
                return Interval(
                    point=point,
                    low=_percentile(replicates, q_lo),
                    high=_percentile(replicates, q_hi),
                    method="BCa",
                    resamples=resamples,
                    confidence=confidence,
                )

    return Interval(
        point=point,
        low=_percentile(replicates, alpha),
        high=_percentile(replicates, 1.0 - alpha),
        method="percentile",
        resamples=resamples,
        confidence=confidence,
        fallback_reason=fallback,
    )


def normal_rate_interval(
    control: ArmSample, treatment: ArmSample, *, confidence: float = 0.95
) -> Interval:
    """The textbook two-proportion Wald interval, for cross-checking only.

    PRD 8.1 says a normal CI is fine for a *rate*, and it is -- which makes this
    the one place the bootstrap can be validated against a closed form on the
    project's own data. If the BCa rate interval and this one disagree
    materially, the bootstrap is wrong, and the printed output says so.

    Never used for the money metric, where the same approximation is the defect
    the bootstrap exists to avoid.
    """
    nc, nt = len(control), len(treatment)
    if nc == 0 or nt == 0:
        return Interval(0.0, 0.0, 0.0, "degenerate", 0, confidence)
    p_c = sum(control.recovered) / nc
    p_t = sum(treatment.recovered) / nt
    diff = p_t - p_c
    se = math.sqrt(p_c * (1.0 - p_c) / nc + p_t * (1.0 - p_t) / nt)
    z = norm_ppf(1.0 - (1.0 - confidence) / 2.0)
    return Interval(
        point=diff,
        low=diff - z * se,
        high=diff + z * se,
        method="normal (Wald)",
        resamples=0,
        confidence=confidence,
    )
