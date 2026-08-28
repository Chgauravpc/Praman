"""The injected incident, and the guarantee that injecting it changed nothing.

Two jobs, and the first is the more important one.

**The default path is untouched.** Day 3 pinned a lot: arm vectors by SHA-256, a
22.0x memoisation ratio, an 18,028-row full-batch ledger, a 4,905/1,095/0
envelope split, and a headline incremental figure with a bootstrap interval.
Every one of those is a function of ``generate(...)`` output. So the first thing
this file establishes is that ``generate(degradation=None)`` produces byte-for-
byte what it produced before ``sim/incident.py`` existed. The obvious way to add
an incident -- a conditional inside the generation loop -- would have shifted the
RNG stream for every event after the first branch taken and invalidated all of
it, silently, in a way that reads as "the simulator changed slightly".

**The injection has the shape it claims.** A rate shift raises a slice's own
failure rate; a mix shift raises a slice's share of attempts while leaving its
rate alone. Those are different statements about the world and the whole
diagnostic difficulty rests on them being distinguishable, so both are asserted
directly against the traffic table rather than inferred from the decomposition
the tool computes.
"""
from __future__ import annotations

import hashlib

import pytest

from pramaan import canonical
from sim import incident as I
from sim.generate import (
    DEV_BATCH_SIZE,
    dev_batch,
    dev_batch_degraded,
    full_batch,
    generate,
)


def _fingerprint(events):
    """A SHA-256 over the canonical rows of a batch, latents included.

    Latents are in scope deliberately: they are what the outcome oracle reads,
    so a batch whose events are identical but whose latents moved would produce
    a different headline number while passing an events-only comparison.
    """
    blob = canonical.canonical_json(
        [
            {
                "row": e.to_row(),
                "latent": {
                    "self_recovers_at": e.latent.self_recovers_at,
                    "capability_clears_at": e.latent.capability_clears_at,
                    "has_intent": e.latent.has_intent,
                    "route_would_succeed": e.latent.route_would_succeed,
                    "message_response_lag_seconds": e.latent.message_response_lag_seconds,
                    "voice_response_lag_seconds": e.latent.voice_response_lag_seconds,
                }
                if e.latent
                else None,
            }
            for e in events
        ]
    )
    return hashlib.sha256(blob.encode()).hexdigest()


# --------------------------------------------------------------------------
# The default path is unchanged
# --------------------------------------------------------------------------

#: Fingerprints of the undegraded batches, measured after ``sim/incident.py``
#: landed and asserted to be stable from here on.
#:
#: These are recorded constants rather than a comparison against a second call,
#: which would only prove the function agrees with itself (ADR-023). They pin the
#: *values*, so a future edit to the generation loop -- an extra RNG draw, a
#: reordered field, a new latent -- fails here with a diff rather than showing up
#: three days later as a headline number that moved for no stated reason.
DEV_FINGERPRINT = "286e2fbc1cc302f22f301b8e0ddfc67331fc15cc32a42851ecfa403c5e3a6e4f"
FULL_FINGERPRINT = "19b5a1b61357f96e0d24f0c2cd056ab4fdf51dd5f6e19ab7edc3276050207d8f"


def test_default_path_is_deterministic_across_calls():
    """The baseline property, and the one every other pin depends on."""
    assert _fingerprint(dev_batch(seed=42)) == _fingerprint(dev_batch(seed=42))
    assert _fingerprint(generate(300, seed=7, days=5)) == _fingerprint(
        generate(300, seed=7, days=5)
    )


def test_the_undegraded_batches_match_their_recorded_fingerprints():
    """The pin. If this fails, a Day 3 figure has moved and needs re-measuring.

    Both batches, because the dev batch is what ``make demo`` reproduces and the
    full batch is what the headline number comes from, and an edit could
    plausibly affect one without the other -- anything keyed on batch size or on
    the day span would.
    """
    assert _fingerprint(dev_batch(seed=42)) == DEV_FINGERPRINT
    assert _fingerprint(full_batch(seed=42)) == FULL_FINGERPRINT


def test_degradation_none_is_identical_to_no_degradation_argument():
    """Passing the new parameter explicitly as None changes nothing.

    The cheap version of this check compares ``generate(n)`` to ``generate(n)``,
    which passes whatever the parameter does. This compares the two *call shapes*,
    which is what a caller upgrading across the change actually does.
    """
    assert _fingerprint(generate(DEV_BATCH_SIZE, seed=42)) == _fingerprint(
        generate(DEV_BATCH_SIZE, seed=42, degradation=None)
    )


def test_the_dev_batch_still_has_its_pinned_arm_vector():
    """68 / 67 / 65, the vector ``tests/test_arms.py`` pins by SHA-256.

    Restated here rather than left to that file, because this is the module that
    could have broken it and a failure should point at the change that caused it.
    """
    events = dev_batch(seed=42)
    counts = {"A": 0, "B": 0, "C": 0}
    for event in events:
        counts[event.arm] += 1
    assert (counts["A"], counts["B"], counts["C"]) == (68, 67, 65)
    assert len(events) == DEV_BATCH_SIZE


def test_injection_does_not_consume_a_draw_from_the_base_stream():
    """The degraded batch contains the undegraded batch, event for event.

    This is the structural claim the "default is off" guarantee rests on: the
    injection appends, so every base event survives with the same id, the same
    amount, the same timestamp and the same arm. If injection had happened inside
    the loop, the base events would still be 1,200 in number and every one of
    them would be different.
    """
    base = generate(1_200, seed=42, days=8)
    degraded, _truth = dev_batch_degraded(seed=42, count=1_200, days=8)

    base_by_id = {e.event_id: e for e in base}
    degraded_by_id = {e.event_id: e for e in degraded}
    assert set(base_by_id).issubset(set(degraded_by_id))
    for event_id, original in base_by_id.items():
        assert degraded_by_id[event_id].to_row() == original.to_row(), event_id


# --------------------------------------------------------------------------
# The injection has the shape it claims
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def degraded():
    events, truth = dev_batch_degraded()
    traffic = I.build_traffic(events, 42, I.DEV_INCIDENT)
    return events, truth, traffic


def _totals(traffic, days):
    out = {}
    for row in traffic:
        if row.day_index in days:
            attempts, failures = out.get(row.segment, (0, 0))
            out[row.segment] = (attempts + row.attempts, failures + row.failures)
    return out


def test_truth_for_agrees_with_the_truth_inject_returns():
    """Two paths to one value must not become two values."""
    base = generate(400, seed=42, days=8)
    _events, from_inject = I.inject(base, I.DEV_INCIDENT, 42)
    assert from_inject.as_dict() == I.truth_for(I.DEV_INCIDENT).as_dict()


def test_the_rate_shift_slice_has_a_higher_failure_rate_in_the_window(degraded):
    """tier2's own rate rises. Something broke there."""
    _events, truth, traffic = degraded
    base = _totals(traffic, {0, 1, 2})
    window = _totals(traffic, {3, 4})
    segment = truth.rate_segment
    base_rate = base[segment][1] / base[segment][0]
    window_rate = window[segment][1] / window[segment][0]
    assert window_rate - base_rate > 0.08, (base_rate, window_rate)


def test_the_mix_shift_slice_keeps_its_own_failure_rate(degraded):
    """tier3's rate is flat while its volume triples. Nothing broke there.

    The single most important assertion in this file. If a mix shift also moved
    the slice's own rate, the two shifts would be indistinguishable and the
    decomposition would be measuring nothing -- and the Day 4 demonstration
    ("+3.8pp is real, +3.5pp is mix, I am acting on the first only") would be an
    illustration rather than a finding.
    """
    _events, truth, traffic = degraded
    base = _totals(traffic, {0, 1, 2})
    window = _totals(traffic, {3, 4})
    segment = truth.mix_segment
    base_rate = base[segment][1] / base[segment][0]
    window_rate = window[segment][1] / window[segment][0]
    assert abs(window_rate - base_rate) < 0.04, (base_rate, window_rate)

    # And its share of attempts rises substantially.
    base_share = base[segment][0] / sum(a for a, _f in base.values())
    window_share = window[segment][0] / sum(a for a, _f in window.values())
    assert window_share - base_share > 0.10, (base_share, window_share)


def test_the_untouched_slice_is_untouched(degraded):
    """metro's rate does not move. A control inside the world itself."""
    _events, _truth, traffic = degraded
    base = _totals(traffic, {0, 1, 2})
    window = _totals(traffic, {3, 4})
    base_rate = base["metro"][1] / base["metro"][0]
    window_rate = window["metro"][1] / window["metro"][0]
    assert abs(window_rate - base_rate) < 0.03, (base_rate, window_rate)


def test_no_failure_rate_exceeds_one(degraded):
    """A rate above 100% is not a rounding artefact, it is an impossible world.

    Worth a test because the first sizing of this incident produced exactly that:
    180 injected failures onto a 200-event base drove tier2 to a clamped 1.000,
    which the decomposition cannot represent, so the incident was invisible to the
    tool built to find it. Nothing raised.
    """
    _events, _truth, traffic = degraded
    for row in traffic:
        assert 0.0 <= row.failure_rate <= 1.0, row
        assert row.attempts >= row.failures, row


def test_baseline_failure_rates_match_the_published_ranges(degraded):
    """The quiet days sit where PRD 6.3's [B] ranges say they should.

    metro 78-82% success and tier3 55-62% are published figures; if the generated
    world does not reproduce them, the mix-shift confound is the wrong size and
    every decomposition on this dataset is quantitatively misleading even when
    qualitatively right.
    """
    _events, _truth, traffic = degraded
    base = _totals(traffic, {0, 1, 2})
    for segment, expected in I.BASELINE_FAILURE_RATE.items():
        observed = base[segment][1] / base[segment][0]
        assert abs(observed - expected) < 0.03, (segment, observed, expected)


def test_attempts_are_a_denominator_not_an_independent_draw(degraded):
    """failures/attempts must be consistent with the declared baseline.

    Generating attempts independently of failures would let the two drift, and a
    "failure rate" computed from unrelated numerators and denominators is not a
    failure rate. This is the property that makes the traffic table meaningful
    rather than decorative.
    """
    _events, _truth, traffic = degraded
    for row in traffic:
        if I.DEV_INCIDENT.covers(row.day_index) and row.segment == "tier2":
            continue      # the slice where something is deliberately wrong
        expected = I.BASELINE_FAILURE_RATE[row.segment]
        assert abs(row.failure_rate - expected) < 0.12, row


# --------------------------------------------------------------------------
# The spec itself
# --------------------------------------------------------------------------


def test_downtime_is_empty_unless_declared():
    from dataclasses import replace

    assert I.build_downtime(I.DEV_INCIDENT) == []
    assert I.build_downtime(None) == []
    declared = I.build_downtime(replace(I.DEV_INCIDENT, declares_downtime=True))
    assert len(declared) == 1
    assert declared[0].day_index_start == I.DEV_INCIDENT.start_day


def test_a_spec_with_an_unknown_segment_or_code_is_refused():
    with pytest.raises(ValueError):
        I.DegradationSpec(name="x", start_day=1, end_day=2, rate_segment="tier9")
    with pytest.raises(ValueError):
        I.DegradationSpec(
            name="x", start_day=1, end_day=2, rate_segment="tier2", rate_code="not_a_code"
        )
    with pytest.raises(ValueError):
        I.DegradationSpec(name="x", start_day=5, end_day=2)


def test_day_index_is_computed_in_ist_not_utc():
    """An event at 02:00 IST belongs to its IST day, not the previous UTC one.

    SQLite's ``strftime`` would normalise ``+05:30`` to UTC and move it, which is
    why ``day_index`` is computed in Python. Every window boundary in the
    investigator depends on this, so it is asserted rather than trusted.
    """
    from datetime import timedelta

    epoch = canonical.parse_iso("2026-08-01T00:00:00+05:30")
    early = canonical.to_iso(epoch + timedelta(days=3, hours=2))
    late = canonical.to_iso(epoch + timedelta(days=3, hours=23))
    assert I.day_index(early) == 3
    assert I.day_index(late) == 3
    assert I.hour_of_day(early) == 2
    assert I.hour_of_day(late) == 23


def test_injection_is_deterministic():
    first, truth_a = dev_batch_degraded(seed=42, count=600, days=8)
    second, truth_b = dev_batch_degraded(seed=42, count=600, days=8)
    assert _fingerprint(first) == _fingerprint(second)
    assert truth_a.as_dict() == truth_b.as_dict()


def test_injected_events_are_indistinguishable_by_amount_or_hour(degraded):
    """An injected event must not be identifiable except by its slice and window.

    If injected events were, say, uniformly distributed across the day while base
    events follow the evening-peak curve, then an investigator could find the
    incident by looking at the hour distribution -- and the test asserting it
    "found the degradation" would be passing on an artefact of the injection
    rather than on the elevated rate.
    """
    events, truth, _traffic = degraded
    # Injected ids carry a 9 in the leading position of their ordinal
    # (``evt_SEED_9NNNNN``); base ordinals on a 1,200-event batch never reach
    # 900000, so the prefix is unambiguous.
    injected = [e for e in events if e.event_id.rsplit("_", 1)[-1].startswith("9")]
    base = [e for e in events if not e.event_id.rsplit("_", 1)[-1].startswith("9")]
    expected = truth.rate_extra_failures + truth.mix_extra_failures
    assert len(injected) == expected == 162, (len(injected), expected)
    assert len(base) == 1_200

    def mean_hour(rows):
        return sum(I.hour_of_day(e.detected_at) for e in rows) / len(rows)

    def mean_band(rows):
        return sum(e.amount_band for e in rows) / len(rows)

    assert abs(mean_hour(injected) - mean_hour(base)) < 1.5
    assert abs(mean_band(injected) - mean_band(base)) < 0.3
