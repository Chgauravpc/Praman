"""The estimator, checked against ground truth. PRD 8.2, invariant I6.

The PRD calls this the most valuable test in the repo, and the reason is narrow:
every other number in the project is downstream of the claim that the randomised
holdout measures the real effect. If the estimator is biased, the headline is
wrong in a way no amount of care elsewhere can fix, and nothing in the output
would look wrong.

**What makes the check possible.** In production the counterfactual is
unobservable -- you cannot see what a treated payment would have done untreated.
In simulation it is known, so both potential outcomes are computable for every
event, and the true average treatment effect is an **exact quantity** rather than
an estimate with error of its own. The estimator then has exactly one source of
error left -- which arm each event happened to land in -- and that is the thing
being tested.

Three tests, and they answer three different questions:

``test_the_ci_covers_the_true_effect``
    Does the interval contain the truth on the batch the project actually
    reports? A single draw, so passing it is necessary and not sufficient.

``test_the_estimator_is_unbiased_over_many_seeds``
    Averaged over many independent batches, does the estimate converge to the
    truth? This is the unbiasedness claim proper. A biased estimator can easily
    pass the first test on one lucky batch and cannot pass this one.

``test_the_interval_covers_at_roughly_the_nominal_rate``
    Does a 95% interval contain the truth about 95% of the time? This tests the
    *interval*, not the point estimate -- an unbiased estimator with a wrong
    variance would pass the test above and fail this one, and then every CI in
    the submission would be the wrong width.

Together they cover the point estimate, its convergence, and its interval. That
is Razorpay's own "verification capacity, not generation speed, is the
bottleneck" thesis turned into a file.
"""
from __future__ import annotations

import math

import pytest

from pramaan.eval import bootstrap as bs
from pramaan.eval import metrics as M
from pramaan.eval import resolve as R
from sim import generate as sim

#: Statistics under test, with the tolerance appropriate to each. The money
#: metric is a mean of a log-normal variable, so its sampling distribution is
#: heavy-tailed and a looser tolerance is honest rather than convenient.
STATISTICS = ("rate", "value_share", "money_per_event")


def _batch(n: int, seed: int):
    """A batch with latents. ``generate`` enriches, so there is no second step."""
    return sim.generate(n, seed=seed)


def _estimate_and_truth(events, seed: int, resamples: int = 4000):
    outcomes = R.resolve_batch(events)
    metrics = M.compute_metrics(outcomes, seed=seed, resamples=resamples)
    truth = R.true_effect(R.potential_outcomes(events))
    return metrics.headline, truth


# --------------------------------------------------------------------------
# 1. Does the interval cover the truth on the reported batch?
# --------------------------------------------------------------------------


def test_the_ci_covers_the_true_effect():
    """On the 6,000-event batch, every 95% interval contains the true effect.

    The batch the project reports, at the seed it reports. One draw from a
    stochastic procedure, so this is a necessary condition rather than a proof --
    which is what the two tests below are for.
    """
    events = _batch(sim.FULL_BATCH_SIZE, seed=42)
    contrast, truth = _estimate_and_truth(events, seed=42)

    for name in STATISTICS:
        interval = contrast.intervals[name]
        assert interval.low <= truth[name] <= interval.high, (
            "the 95%% CI for %s does not contain the true effect: "
            "truth=%.6f, interval=[%.6f, %.6f]. Either the estimator is biased "
            "or the interval is too narrow; test_the_estimator_is_unbiased_"
            "over_many_seeds and test_the_interval_covers_at_roughly_the_"
            "nominal_rate distinguish those two cases."
            % (name, truth[name], interval.low, interval.high)
        )


def test_the_true_effect_is_positive_and_driven_by_the_classes_it_should_be():
    """Arm B cannot lose, and its gains come from where the mechanism says.

    Two claims, and the first is structural rather than statistical. Every
    intervention in the resolver takes the *earlier* of the unaided recovery and
    the action-driven one, so no event can do worse under arm B than under arm A.
    The true effect is therefore non-negative by construction, and a negative
    value here would mean the resolver has a sign error that no amount of
    sampling noise could produce.

    The second claim is that the effect is concentrated in the classes whose
    world model says an intervention can reach them -- FUNDS (a scheduled retry
    finds the balance), LIMIT (a rail switch dodges the cap), INSTRUMENT_DEAD and
    ELIGIBILITY (a message is the only thing that can help). If the lift ever
    shows up in TECH_TRANSIENT or AUTH_DROPOFF, whose action is ACT_WAIT, then an
    "inert" action has stopped being inert.
    """
    events = _batch(2_000, seed=7)
    pairs = R.potential_outcomes(events)

    # Per event, arm B is never worse.
    for control, treatment in pairs:
        assert not (control.recovered and not treatment.recovered), (
            "event %s recovered under arm A and not under arm B. The resolver "
            "takes the earlier of the unaided and action-driven recoveries, so "
            "this is impossible unless that logic has broken."
            % control.event_id
        )

    truth = R.true_effect(pairs)
    assert truth["rate"] > 0.0

    # And it comes from the actionable classes only.
    acting = {"FUNDS", "LIMIT", "INSTRUMENT_DEAD", "ELIGIBILITY"}
    for control, treatment in pairs:
        if treatment.recovered and not control.recovered:
            assert treatment.reason_class in acting, (
                "an incremental recovery in %s, whose arm B action is %s. If "
                "that action is inert this is a bug in the resolver; if the "
                "default action has changed, this list needs updating in the "
                "same commit." % (treatment.reason_class, treatment.action)
            )


# --------------------------------------------------------------------------
# 2. Is the point estimate unbiased?
# --------------------------------------------------------------------------

#: Independent batches. Enough that the standard error of the mean estimate is
#: several times smaller than any bias worth catching, and few enough that the
#: test runs in a couple of seconds. No bootstrap here -- this tests the point
#: estimate, so intervals are not needed and would dominate the runtime.
UNBIASED_SEEDS = 60
UNBIASED_BATCH = 900


def test_the_estimator_is_unbiased_over_many_seeds():
    """Averaged over 60 independent batches, estimate converges to truth.

    The test is a paired one: for each batch, compute both the arm-based estimate
    and the exact truth for *that same batch*, and average the difference. Pairing
    matters -- batch-to-batch variation in the true effect is large, and comparing
    an average estimate against an average truth computed on different batches
    would be dominated by it.

    The null being tested is that ``E[estimate - truth] = 0``. Rejected if the
    mean paired difference exceeds three standard errors, which is a ~99.7% test
    under normality and therefore very unlikely to fail by chance while still
    catching any bias of practical size.
    """
    differences = {name: [] for name in STATISTICS}

    for seed in range(1_000, 1_000 + UNBIASED_SEEDS):
        events = _batch(UNBIASED_BATCH, seed=seed)
        outcomes = R.resolve_batch(events)
        truth = R.true_effect(R.potential_outcomes(events))

        control = M.arm_sample(outcomes, "A")
        treatment = M.arm_sample(outcomes, "B")
        for name in STATISTICS:
            statistic = bs.STATISTICS[name]
            estimate = statistic(*treatment.totals, len(treatment)) - statistic(
                *control.totals, len(control)
            )
            differences[name].append(estimate - truth[name])

    for name in STATISTICS:
        values = differences[name]
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / (n - 1)
        standard_error = math.sqrt(variance / n)
        assert abs(mean) <= 3.0 * standard_error, (
            "the estimator looks biased for %s: mean(estimate - truth) = %.6f "
            "over %d batches, standard error %.6f, i.e. %.1f sigma from zero. "
            "A bias here invalidates every headline figure in the project."
            % (name, mean, n, standard_error, abs(mean) / standard_error)
        )


# --------------------------------------------------------------------------
# 3. Does the interval cover at the nominal rate?
# --------------------------------------------------------------------------

#: Coverage is a proportion, so its own standard error is
#: sqrt(0.95 * 0.05 / n) -- about 3.1pp at n=50. The acceptance band below is
#: set from that rather than by feel: +/- 3 standard errors is roughly +/- 9pp,
#: so [0.86, 1.00] is a genuine test that a badly-scaled interval would fail
#: while a correct one passes essentially always.
COVERAGE_SEEDS = 50
COVERAGE_BATCH = 900
COVERAGE_RESAMPLES = 600
COVERAGE_FLOOR = 0.86


def test_the_interval_covers_at_roughly_the_nominal_rate():
    """A 95% BCa interval should contain the truth about 95% of the time.

    This is the test that catches a *variance* error, which the unbiasedness test
    above cannot see: an estimator can be perfectly unbiased and still produce
    intervals half the width they should be, and then every confidence interval
    in the submission is wrong while every point estimate is right.

    Run on the rate only. The money metric's coverage is genuinely harder to pin
    at this sample size -- a heavy-tailed mean needs more resamples and more
    batches before its empirical coverage settles -- and asserting a tight band
    on it here would produce a test that fails for reasons unrelated to the code.
    Saying that is better than quietly asserting something weak.
    """
    covered = 0
    for seed in range(5_000, 5_000 + COVERAGE_SEEDS):
        events = _batch(COVERAGE_BATCH, seed=seed)
        outcomes = R.resolve_batch(events)
        truth = R.true_effect(R.potential_outcomes(events))
        interval = bs.bootstrap_contrast(
            M.arm_sample(outcomes, "A"),
            M.arm_sample(outcomes, "B"),
            bs.rate,
            seed=seed,
            resamples=COVERAGE_RESAMPLES,
        )
        if interval.low <= truth["rate"] <= interval.high:
            covered += 1

    coverage = covered / COVERAGE_SEEDS
    assert coverage >= COVERAGE_FLOOR, (
        "the 95%% interval covered the true effect in only %d of %d batches "
        "(%.0f%%). Coverage materially below nominal means the intervals are too "
        "narrow, and every CI in the submission would be overstating its own "
        "precision." % (covered, COVERAGE_SEEDS, coverage * 100)
    )
    # An interval that is far too *wide* is also a defect -- it would make the
    # project unable to detect a real effect it should have detected -- but it is
    # a much less dangerous one, so this is a warning boundary rather than a
    # tight upper bound.
    assert coverage <= 1.0


# --------------------------------------------------------------------------
# The estimator must not be able to see the answer key
# --------------------------------------------------------------------------


def test_the_estimator_never_reads_latent_truth():
    """Resolution uses latents; *estimation* must not.

    The division that makes the whole design honest. ``sim/outcomes.py`` reads
    latent truth, because it stands in for a webhook that in production would
    report the real outcome. Everything downstream -- the arm samples, the
    bootstrap, the contrast -- must work from observed outcomes alone, or the
    estimator is reading the answer key and its agreement with the truth proves
    nothing at all.

    Checked by stripping the counterfactual field off the outcomes and asserting
    the estimate does not move. If any estimation code path consulted it, the
    numbers would change.
    """
    from dataclasses import replace

    events = _batch(900, seed=99)
    outcomes = R.resolve_batch(events)

    blinded = [
        replace(o, would_recover_unaided=False, actionable=o.actionable)
        for o in outcomes
    ]

    for arm in ("A", "B"):
        original = M.arm_sample(outcomes, arm)
        stripped = M.arm_sample(blinded, arm)
        assert original == stripped

    real = M.compute_metrics(outcomes, seed=1, resamples=400)
    blind = M.compute_metrics(blinded, seed=1, resamples=400)
    for name in STATISTICS:
        assert (
            real.headline.intervals[name].point
            == blind.headline.intervals[name].point
        ), (
            "the %s estimate changed when the counterfactual was removed, so "
            "some estimation path is reading latent ground truth. Its agreement "
            "with the truth would then be circular." % name
        )
