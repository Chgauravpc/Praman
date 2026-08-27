"""The interval machinery, checked against closed forms rather than itself.

ADR-001 keeps numpy and scipy out, so BCa and the inverse normal CDF are
hand-rolled -- which means they have to be tested against answers computed
somewhere other than this repo. An interval nobody has verified is worse than no
interval, because it looks like rigour.

Four things get checked, in increasing order of how badly a failure would matter:

1. ``norm_cdf`` / ``norm_ppf`` against published quantiles and by round-trip.
2. The percentile bootstrap against a case with a known answer.
3. BCa against the closed-form Wald interval on a *proportion*, where PRD 8.1
   says the normal approximation is adequate -- so agreement there is evidence
   the bootstrap is right, and disagreement is evidence it is not.
4. That the machinery is deterministic, because invariant I8 diffs two demo runs
   byte for byte.
"""
from __future__ import annotations

import math
import random

import pytest

from pramaan.eval import bootstrap as bs

# --------------------------------------------------------------------------
# The normal distribution
# --------------------------------------------------------------------------

#: Quantiles every statistics table publishes. If these are wrong, every
#: interval in the project is the wrong width.
KNOWN_QUANTILES = {
    0.500: 0.0,
    0.750: 0.6744897501960817,
    0.800: 0.8416212335729143,
    0.900: 1.2815515655446004,
    0.950: 1.6448536269514722,
    0.975: 1.9599639845400545,
    0.990: 2.3263478740408408,
    0.995: 2.5758293035489004,
}


@pytest.mark.parametrize("p,expected", sorted(KNOWN_QUANTILES.items()))
def test_norm_ppf_matches_published_quantiles(p, expected):
    """Acklam's approximation claims <1.15e-9 relative error. Held to 1e-7."""
    assert abs(bs.norm_ppf(p) - expected) < 1e-7


def test_norm_ppf_is_antisymmetric():
    """Phi-inverse(p) = -Phi-inverse(1-p). Catches a coefficient typo in one tail.

    Worth its own test because the implementation has three branches -- lower
    tail, central, upper tail -- with different coefficient tables, and a typo in
    the tail used only by the 2.5th percentile would leave the central region
    perfect and every lower interval bound wrong.
    """
    for p in (0.001, 0.01, 0.02425, 0.1, 0.3, 0.49):
        assert abs(bs.norm_ppf(p) + bs.norm_ppf(1.0 - p)) < 1e-9


def test_norm_cdf_and_ppf_round_trip():
    for p in (0.0001, 0.001, 0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 0.9999):
        assert abs(bs.norm_cdf(bs.norm_ppf(p)) - p) < 1e-8


def test_norm_cdf_at_known_points():
    assert abs(bs.norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(bs.norm_cdf(1.959963984540054) - 0.975) < 1e-9
    assert abs(bs.norm_cdf(-1.959963984540054) - 0.025) < 1e-9


def test_norm_ppf_refuses_the_boundaries():
    """0 and 1 raise rather than returning an infinity.

    An infinity would propagate into a percentile index and produce a bound of
    exactly the smallest or largest resample, which looks like a real answer. A
    bootstrap that has put every resample on one side of its estimate is
    degenerate and should say so -- ``bootstrap_contrast`` catches that case and
    falls back to the percentile method with a stated reason.
    """
    for bad in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            bs.norm_ppf(bad)


# --------------------------------------------------------------------------
# Bootstrap sanity
# --------------------------------------------------------------------------


def _bernoulli_sample(p: float, n: int, seed: int) -> bs.ArmSample:
    """A rate-only sample: recovered 0/1, no money, unit denominators."""
    rng = random.Random(seed)
    recovered = tuple(1.0 if rng.random() < p else 0.0 for _ in range(n))
    return bs.ArmSample(
        recovered=recovered,
        recovered_paise=tuple(0.0 for _ in recovered),
        at_risk_paise=tuple(1.0 for _ in recovered),
    )


def test_the_point_estimate_is_the_plain_difference():
    """No resampling should move the point estimate. It is arithmetic."""
    control = _bernoulli_sample(0.30, 500, seed=1)
    treatment = _bernoulli_sample(0.50, 500, seed=2)
    interval = bs.bootstrap_contrast(
        control, treatment, bs.rate, seed=3, resamples=500
    )
    expected = sum(treatment.recovered) / 500 - sum(control.recovered) / 500
    assert abs(interval.point - expected) < 1e-12


def test_bca_agrees_with_the_wald_interval_on_a_proportion():
    """The one statistic with a trustworthy closed form, so the one that checks.

    PRD 8.1: "Recovery rate is a proportion, so a normal CI is fine." That makes
    the Wald interval an external answer for this case, and BCa should land close
    to it -- close, not identical, because if the bias correction and acceleration
    did nothing they would not be worth computing.

    Held to 15% of width, which is loose enough to allow the correction to do its
    job and tight enough that a genuinely mis-scaled bootstrap fails.
    """
    control = _bernoulli_sample(0.30, 1_200, seed=11)
    treatment = _bernoulli_sample(0.45, 1_200, seed=12)

    boot = bs.bootstrap_contrast(
        control, treatment, bs.rate, seed=13, resamples=4_000
    )
    wald = bs.normal_rate_interval(control, treatment)

    assert boot.method == "BCa"
    assert abs(boot.width - wald.width) / wald.width < 0.15
    assert abs(boot.point - wald.point) < 1e-12


def test_a_zero_effect_interval_contains_zero():
    """Two samples from the same distribution: the interval must span zero.

    A bootstrap that returned an interval excluding zero here would manufacture
    significance out of nothing, which is the single most damaging way this
    machinery could fail.
    """
    control = _bernoulli_sample(0.35, 800, seed=21)
    treatment = _bernoulli_sample(0.35, 800, seed=22)
    interval = bs.bootstrap_contrast(
        control, treatment, bs.rate, seed=23, resamples=3_000
    )
    assert interval.low <= 0.0 <= interval.high


def test_a_large_effect_interval_excludes_zero():
    """And the converse: a real, large effect must be detected."""
    control = _bernoulli_sample(0.20, 800, seed=31)
    treatment = _bernoulli_sample(0.60, 800, seed=32)
    interval = bs.bootstrap_contrast(
        control, treatment, bs.rate, seed=33, resamples=3_000
    )
    assert interval.excludes_zero
    assert interval.low > 0.0


def test_the_interval_narrows_as_the_sample_grows():
    """Width should fall roughly as 1/sqrt(n). Checked as a factor of ~2 over 4x.

    Deliberately loose. The point is not to verify the exact rate but to catch a
    bootstrap that ignores sample size altogether -- which is what resampling
    with the wrong n, or resampling the pooled sample rather than each arm, would
    look like.
    """
    widths = {}
    for n in (250, 1_000):
        control = _bernoulli_sample(0.30, n, seed=41)
        treatment = _bernoulli_sample(0.45, n, seed=42)
        widths[n] = bs.bootstrap_contrast(
            control, treatment, bs.rate, seed=43, resamples=2_000
        ).width
    ratio = widths[250] / widths[1_000]
    assert 1.5 < ratio < 2.7, (
        "quadrupling n changed the interval width by a factor of %.2f; "
        "1/sqrt(n) predicts about 2.0" % ratio
    )


# --------------------------------------------------------------------------
# Heavy tails -- the reason the bootstrap exists here at all
# --------------------------------------------------------------------------


def _lognormal_sample(
    n: int, seed: int, shift: float = 0.0, sigma: float = 1.15
) -> bs.ArmSample:
    """Money amounts drawn log-normal, as ``sim/generate.py`` draws them.

    ``sigma`` defaults to the simulator's body value (1.15) and is a parameter so
    the asymmetry tests can turn the tail weight up, which is where the skew is
    large enough to survive into a difference of means.
    """
    rng = random.Random(seed)
    recovered, value, risk = [], [], []
    for _ in range(n):
        u1 = rng.random() or 1e-12
        u2 = rng.random()
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        amount = math.exp(math.log(110_000.0) + sigma * z)
        got = rng.random() < 0.30 + shift
        recovered.append(1.0 if got else 0.0)
        value.append(amount if got else 0.0)
        risk.append(amount)
    return bs.ArmSample(tuple(recovered), tuple(value), tuple(risk))


def _asymmetry(interval) -> float:
    """Upper half-width over lower half-width. 1.0 is perfectly symmetric."""
    upper = interval.high - interval.point
    lower = interval.point - interval.low
    assert lower > 0 and upper > 0
    return upper / lower


def test_the_money_interval_is_asymmetric_on_heavy_tailed_data():
    """A symmetric interval on skewed data is the defect being avoided.

    Asymmetry is the property a normal approximation cannot have at all -- it is
    symmetric by construction -- so it is the observable difference between having
    used a bootstrap and having claimed to.

    **The sample size matters, and the first version of this test got it wrong.**
    It asserted asymmetry at n=1,000 per arm and measured 0.955, which is nearly
    symmetric, and the reason is not a bug: the statistic is a *difference of two
    means*, and by the central limit theorem that difference converges to normal
    as n grows however skewed the underlying amounts are. Testing for asymmetry in
    a regime where theory predicts near-symmetry is testing the wrong thing.

    So the test now uses a sample size where the skew genuinely survives into the
    contrast, and the companion test below asserts the convergence itself -- which
    is a stronger statement than either sample size alone, because it checks the
    mechanism rather than a threshold.
    """
    control = _lognormal_sample(300, seed=51, sigma=1.8)
    treatment = _lognormal_sample(300, seed=52, sigma=1.8, shift=0.10)
    interval = bs.bootstrap_contrast(
        control, treatment, bs.money_per_event, seed=53, resamples=4_000
    )
    asymmetry = _asymmetry(interval)
    assert abs(asymmetry - 1.0) > 0.20, (
        "the money interval came out near-symmetric (upper/lower = %.3f) at a "
        "sample size and tail weight where the skew should survive into the "
        "difference. The interval is probably not being read off the resample "
        "distribution." % asymmetry
    )


def test_interval_asymmetry_decays_as_the_sample_grows():
    """Skew in the contrast washes out with n, exactly as the CLT predicts.

    The positive control for the test above. A bootstrap that produced asymmetry
    from an implementation artefact rather than from the data would have no reason
    to become more symmetric as n grew, so this distinguishes real skew from a
    bug that merely looks like skew.
    """
    distances = {}
    for n in (300, 1_500):
        control = _lognormal_sample(n, seed=51, sigma=1.8)
        treatment = _lognormal_sample(n, seed=52, sigma=1.8, shift=0.10)
        interval = bs.bootstrap_contrast(
            control, treatment, bs.money_per_event, seed=53, resamples=3_000
        )
        distances[n] = abs(_asymmetry(interval) - 1.0)

    assert distances[1_500] < distances[300], (
        "asymmetry did not fall when the sample grew five-fold "
        "(|asym-1| went %.3f -> %.3f). Skew in a difference of means must decay "
        "with n; asymmetry that does not is an artefact rather than the data."
        % (distances[300], distances[1_500])
    )


def test_money_intervals_are_relatively_wider_than_rate_intervals():
    """The DoD check, on synthetic data where the answer is known in advance.

    Both intervals normalised by their own control-arm level, which is what makes
    two different units comparable. See ``metrics.Contrast.relative_widths`` for
    why normalising by the point estimate instead is wrong.
    """
    control = _lognormal_sample(1_500, seed=61)
    treatment = _lognormal_sample(1_500, seed=62, shift=0.08)

    rate_iv = bs.bootstrap_contrast(
        control, treatment, bs.rate, seed=63, resamples=3_000
    )
    money_iv = bs.bootstrap_contrast(
        control, treatment, bs.money_per_event, seed=64, resamples=3_000
    )
    n = len(control)
    baseline_rate = sum(control.recovered) / n
    baseline_money = sum(control.recovered_paise) / n

    assert (money_iv.width / baseline_money) > (rate_iv.width / baseline_rate)


# --------------------------------------------------------------------------
# Determinism and degenerate input
# --------------------------------------------------------------------------


def test_the_bootstrap_is_deterministic():
    """Same seed, same interval. Invariant I8 diffs two demo runs byte for byte."""
    control = _bernoulli_sample(0.30, 400, seed=71)
    treatment = _bernoulli_sample(0.45, 400, seed=72)
    first = bs.bootstrap_contrast(
        control, treatment, bs.rate, seed=73, resamples=1_500
    )
    second = bs.bootstrap_contrast(
        control, treatment, bs.rate, seed=73, resamples=1_500
    )
    assert (first.point, first.low, first.high) == (
        second.point,
        second.low,
        second.high,
    )


def test_a_different_seed_gives_a_different_interval():
    """The converse -- otherwise the seed is not being used and I8 proves nothing."""
    control = _bernoulli_sample(0.30, 400, seed=71)
    treatment = _bernoulli_sample(0.45, 400, seed=72)
    a = bs.bootstrap_contrast(control, treatment, bs.rate, seed=1, resamples=800)
    b = bs.bootstrap_contrast(control, treatment, bs.rate, seed=2, resamples=800)
    assert (a.low, a.high) != (b.low, b.high)


def test_a_degenerate_sample_falls_back_and_says_so():
    """All-identical data cannot support BCa, and must not silently claim to.

    With every observation identical, every resample is identical, the bias
    correction z0 is undefined, and the acceleration divides by zero. The right
    behaviour is to fall back to the percentile method *and relabel* -- a
    percentile interval reported as BCa would be a lie about the method, and the
    method is the part being trusted.
    """
    flat = bs.ArmSample(
        recovered=tuple([1.0] * 50),
        recovered_paise=tuple([100.0] * 50),
        at_risk_paise=tuple([100.0] * 50),
    )
    interval = bs.bootstrap_contrast(flat, flat, bs.rate, seed=81, resamples=200)
    assert interval.method != "BCa"
    assert interval.fallback_reason is not None
    assert interval.point == 0.0


def test_an_empty_arm_is_reported_as_degenerate():
    empty = bs.ArmSample((), (), ())
    full = _bernoulli_sample(0.3, 100, seed=91)
    interval = bs.bootstrap_contrast(empty, full, bs.rate, seed=92, resamples=100)
    assert interval.method == "degenerate"
    assert interval.fallback_reason is not None


def test_arm_sample_rejects_ragged_vectors():
    with pytest.raises(ValueError):
        bs.ArmSample((1.0, 0.0), (0.0,), (1.0, 1.0))


def test_statistics_are_the_quantities_they_claim_to_be():
    """rate, value_share and money_per_event are three different things.

    PRD 10.2 is explicit that conflating the event-weighted and value-weighted
    figures is a defect rather than a rounding issue, so this pins each formula
    against a hand-computed case where all three differ.
    """
    # Two events. One recovers, worth 900 paise of a 1,000-paise total at risk.
    sample = bs.ArmSample(
        recovered=(1.0, 0.0),
        recovered_paise=(900.0, 0.0),
        at_risk_paise=(900.0, 100.0),
    )
    totals = sample.totals
    assert bs.rate(*totals, 2) == 0.5                    # 1 of 2 events
    assert bs.value_share(*totals, 2) == 0.9             # 900 of 1,000 paise
    assert bs.money_per_event(*totals, 2) == 450.0       # 900 paise over 2 events
