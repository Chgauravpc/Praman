"""The simulator draws from the PRD 5.1 distribution, not from a uniform.

Anti-pattern A2, made into a test. The failure this guards against is not a
crash -- a uniform draw over the 69 codes runs perfectly happily. It puts ~1.4%
of events on ``bank_technical_error`` when reality is 35-45%, and from there
every downstream number measures a workload that does not exist.

A note on what is asserted where, because the distinction is the statistically
honest part:

- **Declared weights** are checked against the published ranges *exactly*. They
  are constants, so this is a property of the configuration and it either holds
  or it does not.
- **Observed shares** are checked against the declared weights at three standard
  errors. A 200-draw multinomial has a ~3.4pp standard error on a 0.37 share, so
  requiring a small sample to land inside a 10pp-wide *population* range would be
  requiring the simulator not to be random. The full batch, where the standard
  error is ~0.6pp, is held to the published range directly.
"""
from __future__ import annotations

import collections

import pytest

from pramaan.taxonomy import (
    BY_CODE,
    NOT_RETRYABLE_CODES,
    REASON_CLASSES,
    RETRY_ELIGIBLE_CODES,
    TOTAL_CODES,
)
from sim import generate as sim

UNIFORM_SHARE = 1.0 / TOTAL_CODES  # 1.45% -- what A2 would produce


@pytest.fixture(scope="module")
def dev():
    return sim.dev_batch(42)


@pytest.fixture(scope="module")
def full():
    return sim.full_batch(42)


# -- the configuration ------------------------------------------------------


def test_declared_weights_sit_inside_the_published_ranges():
    for bucket, (low, high) in sim.PUBLISHED_RANGES.items():
        weight = sim.BUCKET_WEIGHTS[bucket]
        assert low <= weight <= high, "%s declared at %.3f, outside %s" % (
            bucket,
            weight,
            (low, high),
        )


def test_no_declared_weight_sits_on_a_range_boundary():
    """A weight on its floor lands below it half the time.

    This is a real defect the build hit: ``network`` was declared at exactly
    0.10, its published floor, and the 6,000-event batch came out at 0.0923 --
    outside the range the README quotes, for no reason other than the boundary.
    """
    for bucket, (low, high) in sim.PUBLISHED_RANGES.items():
        weight = sim.BUCKET_WEIGHTS[bucket]
        margin = min(weight - low, high - weight)
        assert margin >= 0.005, (
            "%s is declared at %.3f, only %.4f from an edge of %s -- give it "
            "headroom or the sample will fall outside the published range"
            % (bucket, weight, margin, (low, high))
        )


def test_the_weights_are_a_distribution():
    assert sum(sim.BUCKET_WEIGHTS.values()) == pytest.approx(1.0)
    for bucket, codes in sim.BUCKET_CODES.items():
        assert sum(w for _, w in codes) == pytest.approx(1.0), bucket
    assert sum(w for _, w in sim.LONG_TAIL_CLASS_WEIGHTS) == pytest.approx(1.0)


def test_every_weighted_code_exists_in_the_taxonomy():
    """A typo in a code name would silently reweight the distribution."""
    for bucket, codes in sim.BUCKET_CODES.items():
        for code, _ in codes:
            assert code in BY_CODE, "%s in bucket %s is not a real code" % (code, bucket)


def test_bucket_classes_are_what_the_prd_says_they_are():
    """The five published buckets must map onto the classes PRD 5.1 describes."""
    expected = {
        "bank_timeout": {"TECH_TRANSIENT"},
        "wrong_pin": {"AUTH_DROPOFF"},
        "insufficient_balance": {"FUNDS"},
        "network": {"TECH_TRANSIENT"},
        "account_blocked": {"INSTRUMENT_DEAD"},
    }
    for bucket, classes in expected.items():
        observed = {BY_CODE[c].reason_class for c, _ in sim.BUCKET_CODES[bucket]}
        assert observed == classes, "%s maps to %r, expected %r" % (
            bucket,
            observed,
            classes,
        )


# -- the draw ---------------------------------------------------------------


def _within_three_standard_errors(shares, n: int) -> None:
    for bucket, weight in sim.BUCKET_WEIGHTS.items():
        standard_error = (weight * (1 - weight) / n) ** 0.5
        tolerance = max(3 * standard_error, 0.01)
        assert abs(shares[bucket] - weight) <= tolerance, (
            "%s observed %.4f, declared %.4f, tolerance %.4f at n=%d"
            % (bucket, shares[bucket], weight, tolerance, n)
        )


def test_dev_batch_matches_the_declared_weights(dev):
    _within_three_standard_errors(sim.bucket_shares(dev), len(dev))


def test_full_batch_matches_the_declared_weights(full):
    _within_three_standard_errors(sim.bucket_shares(full), len(full))


def test_full_batch_lands_inside_every_published_range(full):
    """At n=6,000 the sample is precise enough to be held to the published range.

    This is the assertion the README's distribution claim actually rests on.
    """
    shares = sim.bucket_shares(full)
    for bucket, (low, high) in sim.PUBLISHED_RANGES.items():
        assert low <= shares[bucket] <= high, "%s observed %.4f, outside %s" % (
            bucket,
            shares[bucket],
            (low, high),
        )


# -- the anti-pattern itself ------------------------------------------------


@pytest.mark.parametrize("batch", ["dev", "full"])
def test_the_draw_is_emphatically_not_uniform(batch, dev, full):
    """A2, stated as a hypothesis test rather than as a comment.

    ``bank_technical_error`` is the single code the anti-pattern names: a uniform
    draw over the 69 codes gives it 1.45% of events, reality gives it 35-45% via
    its bucket, and the declared weights put it near 22%.

    The assertion rejects the uniform null at five standard errors *of the null*,
    which scales with batch size instead of being a fixed multiple. An earlier
    version demanded "more than ten times uniform" (14.5%) and failed on the
    200-event batch at 13.0% -- a -3.1 sigma draw against an expectation of 22%,
    i.e. the sampling distribution behaving normally. Requiring a small sample to
    clear a tight fixed bound is the same mistake as requiring it to land inside
    a published population range: it demands a simulator that is not random. The
    full batch, where the standard error is small, is held to the absolute figure
    as well.
    """
    events = dev if batch == "dev" else full
    n = len(events)
    counts = collections.Counter(e.cause_signal for e in events)
    bank_share = counts["bank_technical_error"] / n

    null_se = (UNIFORM_SHARE * (1 - UNIFORM_SHARE) / n) ** 0.5
    z = (bank_share - UNIFORM_SHARE) / null_se
    assert z > 5.0, (
        "bank_technical_error is %.4f of events, only %.1f sigma above the %.4f a "
        "uniform draw over the 69 codes would give -- that is anti-pattern A2"
        % (bank_share, z, UNIFORM_SHARE)
    )
    if n >= 2000:
        assert bank_share > 0.15


def test_the_dominant_code_is_close_to_its_declared_share_at_scale(full):
    """The intra-bucket weighting, checked where the sample supports it.

    Separated from the uniformity test on purpose: this one is about whether the
    weights are being applied correctly, and it needs the full batch to say
    anything. The dev batch cannot distinguish a 22% weight from a 13% one.
    """
    n = len(full)
    counts = collections.Counter(e.cause_signal for e in full)
    observed = counts["bank_technical_error"] / n
    expected = sim.BUCKET_WEIGHTS["bank_timeout"] * dict(
        sim.BUCKET_CODES["bank_timeout"]
    )["bank_technical_error"]
    standard_error = (expected * (1 - expected) / n) ** 0.5
    assert abs(observed - expected) <= 3 * standard_error, (
        "bank_technical_error observed %.4f, declared implies %.4f" % (observed, expected)
    )


@pytest.mark.parametrize("batch", ["dev", "full"])
def test_volume_concentrates_in_retry_eligible_classes(batch, dev, full):
    """The divergence that makes the tail dangerous.

    By volume, most failures are recoverable; by code count, 45 of 69 are not. A
    simulator that lost this divergence would test nothing interesting, because
    the trap it exists to expose would be gone.
    """
    events = dev if batch == "dev" else full
    retryable_volume = sum(1 for e in events if BY_CODE[e.cause_signal].retry_eligible)
    volume_share = retryable_volume / len(events)
    code_share = RETRY_ELIGIBLE_CODES / TOTAL_CODES

    assert volume_share > 0.80, "expected most volume to be retry-eligible"
    assert code_share < 0.40, "expected most codes not to be retry-eligible"
    assert NOT_RETRYABLE_CODES == 45


@pytest.mark.parametrize("batch", ["dev", "full"])
def test_the_dangerous_long_tail_is_actually_present(batch, dev, full):
    """The classes where a naive agent does damage must appear in the batch.

    RISK (retrying a risk decline penalises the merchant) and ALREADY_PAID (the
    humiliation case, stopping rule S1) are the two that matter most. A batch
    containing none of them could not exercise the branches the envelope exists
    for.
    """
    events = dev if batch == "dev" else full
    classes = collections.Counter(e.reason_class for e in events)
    for dangerous in ("RISK", "ALREADY_PAID", "MERCHANT_CONFIG", "INTEGRATION_BUG"):
        assert classes[dangerous] > 0, "%s absent from the %s batch" % (dangerous, batch)


def test_the_long_tail_stays_a_tail(full):
    """Present, but small: it is 4% of a real error stream, not 40%."""
    shares = sim.class_shares(full)
    tail = sum(
        shares.get(c, 0.0)
        for c in ("MERCHANT_CONFIG", "INTEGRATION_BUG", "RISK", "ALREADY_PAID")
    )
    assert 0.005 < tail < 0.06


def test_all_ten_reason_classes_are_reachable(full):
    observed = set(sim.class_shares(full))
    assert observed == set(REASON_CLASSES), "unreachable classes: %r" % (
        set(REASON_CLASSES) - observed,
    )


# -- the counterfactual ----------------------------------------------------


def test_organic_self_recovery_is_calibrated_near_the_prd_baseline(full):
    """PRD 8.1 does its power calculation at a baseline of p about 0.30.

    If the simulator's organic recovery rate were far from that, the power
    analysis that chose N=6,000 would be describing a different experiment.
    """
    rate = sim.self_recovery_rate(full)
    assert 0.25 <= rate <= 0.36, "organic self-recovery is %.3f" % rate


def test_self_recovery_follows_the_class_ordering(full):
    """AUTH_DROPOFF recovers most; the dead classes do not recover at all.

    PRD Appendix A calls AUTH_DROPOFF the highest organic self-recovery class --
    a wrong UPI PIN is the case where the customer is most likely fixing it
    themselves right now. That ordering is the reason ACT_WAIT exists, so it is
    worth asserting rather than assuming.
    """
    by_class = collections.defaultdict(lambda: [0, 0])
    for event in full:
        bucket = by_class[event.reason_class]
        bucket[0] += 1
        if event.latent is not None and event.latent.self_recovers:
            bucket[1] += 1

    rate = {c: v[1] / v[0] for c, v in by_class.items() if v[0] >= 20}
    assert rate["AUTH_DROPOFF"] > rate["TECH_TRANSIENT"] > rate["FUNDS"]
    for dead in ("MERCHANT_CONFIG", "INTEGRATION_BUG", "RISK"):
        if dead in by_class:
            assert by_class[dead][1] == 0, "%s must never self-recover" % dead


def test_already_paid_events_have_already_recovered(full):
    """S1's premise. If these did not self-recover at once, S1 would be wrong."""
    already = [e for e in full if e.reason_class == "ALREADY_PAID"]
    assert already
    for event in already:
        assert event.latent is not None and event.latent.self_recovers
        assert event.latent.self_recovers_at == event.detected_at


def test_self_recovery_never_precedes_detection(full):
    """A counterfactual that fires before the failure would be a sign error."""
    from pramaan.canonical import parse_iso

    for event in full:
        if event.latent is not None and event.latent.self_recovers:
            assert parse_iso(event.latent.self_recovers_at) >= parse_iso(
                event.detected_at
            )


# -- structure -------------------------------------------------------------


def test_amounts_are_heavy_tailed_and_cover_every_band(full):
    """Log-normal amounts are why the money CI needs a bootstrap (A6)."""
    histogram = sim.band_histogram(full)
    for band, count in histogram.items():
        assert count > 0, "band %d is empty" % band

    amounts = sorted(e.amount_at_risk_paise for e in full)
    median = amounts[len(amounts) // 2]
    mean = sum(amounts) / len(amounts)
    # The signature of a heavy right tail: the mean sits well above the median.
    assert mean > 1.5 * median


def test_arms_are_assigned_in_near_equal_thirds(full):
    counts = collections.Counter(e.arm for e in full)
    assert set(counts) == {"A", "B", "C"}
    expected = len(full) / 3
    for arm, count in counts.items():
        assert abs(count - expected) / expected < 0.02, "%s: %d" % (arm, count)


def test_arms_are_balanced_within_each_stratum(full):
    """Stratification is the point, so check inside the strata, not just overall.

    Heavy-tailed amounts otherwise stack in one arm by luck (PRD 8.1), and an
    overall-balanced split can still be badly unbalanced in band 5 -- which is
    where the money is.
    """
    strata = collections.defaultdict(collections.Counter)
    for event in full:
        strata[(event.amount_band, event.counterparty.segment)][event.arm] += 1
    for stratum, counts in strata.items():
        total = sum(counts.values())
        if total < 30:
            continue
        # Permuted blocks of three: the residual is at most one per open block.
        assert max(counts.values()) - min(counts.values()) <= 2, "%r: %r" % (
            stratum,
            dict(counts),
        )


def test_counterparties_repeat_so_contact_budgets_are_testable(full):
    """S2 and the promise state machine need a payer to appear more than once."""
    counts = collections.Counter(e.counterparty.id for e in full)
    assert len(counts) < len(full)
    assert max(counts.values()) > 1


def test_every_event_offers_act_wait(full):
    """PRD 5.1's action-space argument, as an invariant.

    An agent whose action space cannot express "wait, they are probably already
    retrying" will pay to message people who have already paid.
    """
    for event in full:
        assert "ACT_WAIT" in event.available_actions


def test_no_event_offers_contact_when_the_class_forbids_it(full):
    """MERCHANT_CONFIG, INTEGRATION_BUG, RISK and ALREADY_PAID never reach a customer."""
    for event in full:
        if event.reason_class in ("MERCHANT_CONFIG", "INTEGRATION_BUG", "RISK", "ALREADY_PAID"):
            assert "ACT_MESSAGE" not in event.available_actions
            assert "ACT_VOICE" not in event.available_actions


def test_never_retry_classes_are_never_offered_a_retry(full):
    """Anti-pattern A5, at the adapter boundary rather than only in the envelope."""
    for event in full:
        if event.reason_class in ("INSTRUMENT_DEAD", "MERCHANT_CONFIG", "INTEGRATION_BUG",
                                  "ALREADY_PAID", "RISK", "ELIGIBILITY"):
            assert "ACT_RETRY" not in event.available_actions
