"""Day 6, block A: the simulator spans all five event types.

The one thing this file must never do is touch ``generate()``/``dev_batch()``/
``full_batch()``'s own output -- every hash Days 1-5 pinned (arm vectors, the
golden ledger, the 22.0x memoisation ratio) is measured against exactly that
payment-only stream, and ``generate_all_types`` is additive precisely so none
of it has to move for breadth to land.
"""
from __future__ import annotations

from pramaan import canonical
from sim import generate as sim


def test_generate_all_types_spans_all_five_source_types_and_prints_counts():
    """The Day 6 DoD line: "a run spans all five event types; per-type counts
    print"."""
    events = sim.generate_all_types(600, seed=42)
    counts = sim.per_type_counts(events)
    assert set(counts) == set(canonical.SOURCE_TYPES)
    for source_type in canonical.SOURCE_TYPES:
        assert counts[source_type] > 0, "%s produced no events" % source_type
    assert sum(counts.values()) == 600


def test_generate_all_types_scales_to_the_6000_event_full_batch():
    events = sim.full_batch_all_types(seed=42)
    assert len(events) == sim.FULL_BATCH_SIZE
    counts = sim.per_type_counts(events)
    assert counts["payment"] > counts["checkout"] > counts["subscription"]
    assert sum(counts.values()) == sim.FULL_BATCH_SIZE


def test_generate_all_types_is_deterministic():
    a = sim.generate_all_types(300, seed=7)
    b = sim.generate_all_types(300, seed=7)
    assert [e.event_id for e in a] == [e.event_id for e in b]
    assert [e.detected_at for e in a] == [e.detected_at for e in b]


def test_events_are_sorted_by_detected_at_then_event_id():
    events = sim.generate_all_types(400, seed=42)
    keys = [(e.detected_at, e.event_id) for e in events]
    assert keys == sorted(keys)


def test_every_event_carries_a_valid_reason_class_and_a_real_latent():
    """Every source type must reach ``sim.latent.enrich`` successfully -- a
    reason class the world model has no entry for would raise there, not
    silently produce an inert event."""
    from pramaan.taxonomy import REASON_CLASSES

    events = sim.generate_all_types(500, seed=42)
    for event in events:
        assert event.reason_class in REASON_CLASSES
        assert event.latent is not None


def test_a_reconciled_receivable_never_offers_contact_or_retry():
    events = sim.generate_receivable(2_000, seed=42)
    reconciled = [e for e in events if e.cause_signal == "receivable_reconciled"]
    assert reconciled, "the reconciled share must actually appear at this scale"
    for event in reconciled:
        assert event.available_actions == ("ACT_WAIT", "ACT_STOP")


def test_receivables_are_all_collection_context_others_are_service():
    events = sim.generate_all_types(600, seed=42)
    for event in events:
        expected = "collection" if event.source_type == "receivable" else "service"
        assert event.legal_context == expected


def test_generate_all_types_does_not_perturb_the_pinned_payment_only_batches():
    """The additivity guarantee, asserted rather than assumed: calling the new
    function first must not consume any RNG state the old ones depend on,
    because they build their own generators/assigners from the seed alone."""
    before = sim.dev_batch(42)
    sim.generate_all_types(300, seed=42)  # exercised in between, must not leak state
    after = sim.dev_batch(42)
    assert [e.event_id for e in before] == [e.event_id for e in after]
    assert [e.detected_at for e in before] == [e.detected_at for e in after]
    assert [e.arm for e in before] == [e.arm for e in after]
