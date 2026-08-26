"""Invariant I1 -- replaying the stream twice produces an identical ledger.

Razorpay webhooks are at-least-once and out-of-order. So this is not a tidiness
property: it is the difference between an audit trail and a pile of duplicates,
and it is what NFR-5 requires.

The mechanism is one line in ``store.ingest``: a DETECT row is appended if and
only if ``EventStore.insert`` reports the event was new. Everything here exists
to make sure that line cannot be quietly broken.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pramaan.ledger.chain import Ledger
from pramaan.sense.store import EventStore, connect, ingest
from sim import generate as sim


@pytest.fixture()
def fresh():
    conn = connect(None)  # in-memory
    yield EventStore(conn), Ledger(conn)
    conn.close()


def test_replaying_the_stream_leaves_the_ledger_head_unchanged(fresh):
    store, ledger = fresh
    events = sim.dev_batch(42)

    new, duplicates = ingest(store, ledger, events)
    assert (new, duplicates) == (len(events), 0)
    head_after_first = ledger.head_hash()
    rows_after_first = ledger.count()

    new, duplicates = ingest(store, ledger, events)
    assert (new, duplicates) == (0, len(events))
    assert ledger.head_hash() == head_after_first
    assert ledger.count() == rows_after_first


def test_out_of_order_redelivery_is_also_idempotent(fresh):
    """The realistic case: the same events arriving shuffled, twice over."""
    store, ledger = fresh
    events = sim.dev_batch(42)

    ingest(store, ledger, events)
    head = ledger.head_hash()

    for replay_seed in (1, 2, 3):
        new, duplicates = ingest(store, ledger, sim.iter_shuffled(events, replay_seed))
        assert new == 0
        assert duplicates == len(events)
    assert ledger.head_hash() == head


def test_arrival_order_does_not_change_the_ledger():
    """Two fresh stores fed the same events in different orders must agree.

    Stronger than replay-idempotency and the property that actually matters in
    production: a webhook endpoint has no control over arrival order, so if the
    ledger hash depended on it, the hash would be unreproducible even without any
    duplicate delivery.

    It holds because ``ingest`` walks the store's canonical order rather than
    arrival order, and because ``ts`` is the event's own ``detected_at``.
    """
    events = sim.dev_batch(42)
    heads = []
    for arrival_seed in (7, 8):
        conn = connect(None)
        store, ledger = EventStore(conn), Ledger(conn)
        ingest(store, ledger, sim.iter_shuffled(events, arrival_seed))
        heads.append(_ordered_ledger_digest(ledger))
        conn.close()
    assert heads[0] == heads[1]


def _ordered_ledger_digest(ledger: Ledger) -> str:
    """Digest of the ledger's *content*, independent of insertion order.

    The chain hash necessarily depends on the order rows were appended -- that is
    what a chain is. The claim this test makes is about the recorded facts, so it
    compares the sorted set of (ts, kind, payload) instead.
    """
    from pramaan.canonical import canonical_json, sha256_hex

    rows = sorted(
        (row["ts"], row["kind"], row["payload"]) for row in ledger.iter_raw()
    )
    return sha256_hex(canonical_json(rows))


def test_a_fresh_run_reproduces_the_same_head_hash(tmp_path: Path):
    """Same seed, same events, two separate databases, same head hash.

    NFR-3. This is the property a reviewer is really checking when they run
    ``make demo`` twice.
    """
    heads = []
    for index in (0, 1):
        conn = connect(tmp_path / ("run%d.db" % index))
        store, ledger = EventStore(conn), Ledger(conn)
        ingest(store, ledger, sim.dev_batch(42))
        heads.append(ledger.head_hash())
        conn.close()
    assert heads[0] == heads[1]


def test_a_different_seed_gives_a_different_ledger(fresh):
    """The determinism claim needs its negative case.

    Without it, a ledger hash that ignored its inputs entirely would pass every
    other test in this file.
    """
    store, ledger = fresh
    ingest(store, ledger, sim.dev_batch(42))
    head_42 = ledger.head_hash()

    conn = connect(None)
    other_store, other_ledger = EventStore(conn), Ledger(conn)
    ingest(other_store, other_ledger, sim.dev_batch(43))
    head_43 = other_ledger.head_hash()
    conn.close()

    assert head_42 != head_43


def test_the_same_seed_reproduces_an_identical_batch():
    """Determinism of the simulator itself, before any storage is involved."""
    first = sim.dev_batch(42)
    second = sim.dev_batch(42)
    assert len(first) == len(second)
    for left, right in zip(first, second):
        assert left == right  # frozen dataclasses compare by value, latent included


def test_insert_reports_novelty_correctly(fresh):
    """The single return value the whole invariant hangs on."""
    store, _ = fresh
    event = sim.dev_batch(42)[0]
    assert store.insert(event) is True
    assert store.insert(event) is False
    assert store.count() == 1


def test_ledger_rejects_an_unknown_kind(fresh):
    """The enum holds only kinds the system emits (PRD 12.2)."""
    _, ledger = fresh
    with pytest.raises(ValueError, match="unknown ledger kind"):
        ledger.append("CANARY", ts="2026-08-01T10:00:00+05:30", payload={})


def test_ledger_refuses_a_naive_timestamp(fresh):
    """No offset means no way to know which clock produced it."""
    _, ledger = fresh
    with pytest.raises(ValueError, match="no timezone offset"):
        ledger.append("DETECT", ts="2026-08-01T10:00:00", payload={})


def test_latent_truth_is_not_returned_by_default(fresh):
    """The agent-facing read path must not hand back the answer key."""
    store, ledger = fresh
    events = sim.dev_batch(42)
    ingest(store, ledger, events)

    assert any(e.latent and e.latent.self_recovers for e in events), (
        "the batch must contain self-recovering events or this proves nothing"
    )
    assert all(e.latent is None for e in store.iter_events())
    assert any(
        e.latent is not None for e in store.iter_events(with_latent=True)
    ), "the estimator must still be able to ask for ground truth explicitly"


def test_detect_payload_carries_no_latent_field(fresh):
    """What lands in the ledger, checked directly.

    The ledger is public inside the repo, and a reviewer will read it. Ground
    truth appearing there would invalidate the experiment just as surely as
    putting it in a prompt.
    """
    store, ledger = fresh
    ingest(store, ledger, sim.dev_batch(42))
    for row in ledger.iter_raw():
        assert "self_recover" not in row["payload"]
        assert "latent" not in row["payload"]
