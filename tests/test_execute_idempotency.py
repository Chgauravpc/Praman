"""PRD 12.1: idempotency key, the idempotency store, and the counterparty lock.

Three properties, each with its own test rather than one combined smoke test,
because each is a distinct way an execution layer double-charges or
double-messages somebody:

- the SAME logical action always hashes to the SAME key (so a retry is
  recognised as a retry),
- a DIFFERENT logical action never collides (so two real actions are never
  silently merged into one),
- and re-running a key already seen is a no-op -- the definition-of-done item
  this file exists to prove.
"""
from __future__ import annotations

import pytest

from pramaan.execute.locks import CounterpartyLock
from pramaan.execute.razorpay import IdempotencyStore, idempotency_key


# --------------------------------------------------------------------------
# idempotency_key
# --------------------------------------------------------------------------


def test_the_same_inputs_always_produce_the_same_key():
    a = idempotency_key("pay_ABC123", "create_order", 1)
    b = idempotency_key("pay_ABC123", "create_order", 1)
    assert a == b


def test_a_different_attempt_ordinal_produces_a_different_key():
    """The formula is hash(payment_id, action_type, attempt_ordinal) -- all
    three fields, or a real retry attempt would collide with the first."""
    first = idempotency_key("pay_ABC123", "create_order", 1)
    second = idempotency_key("pay_ABC123", "create_order", 2)
    assert first != second


def test_a_different_action_type_produces_a_different_key():
    order_key = idempotency_key("pay_ABC123", "create_order", 1)
    link_key = idempotency_key("pay_ABC123", "create_payment_link", 1)
    assert order_key != link_key


def test_a_different_payment_id_produces_a_different_key():
    a = idempotency_key("pay_ABC123", "create_order", 1)
    b = idempotency_key("pay_XYZ999", "create_order", 1)
    assert a != b


def test_an_unknown_action_type_is_refused():
    with pytest.raises(ValueError, match="unknown action_type"):
        idempotency_key("pay_ABC123", "send_carrier_pigeon", 1)


# --------------------------------------------------------------------------
# IdempotencyStore -- the no-op-on-replay guarantee
# --------------------------------------------------------------------------


def test_double_executing_the_same_key_calls_the_function_once():
    """The Day 5 definition-of-done item, as a unit test rather than an
    integration demo."""
    store = IdempotencyStore()
    key = idempotency_key("pay_ABC123", "create_order", 1)
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        return {"id": "order_%d" % calls["n"]}

    first, was_new_1 = store.execute_once(key, make)
    second, was_new_2 = store.execute_once(key, make)

    assert calls["n"] == 1
    assert was_new_1 is True
    assert was_new_2 is False
    assert first == second == {"id": "order_1"}
    assert store.seen(key)
    assert store.count() == 1


def test_two_different_keys_both_execute():
    store = IdempotencyStore()
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        return calls["n"]

    store.execute_once(idempotency_key("pay_A", "create_order", 1), make)
    store.execute_once(idempotency_key("pay_B", "create_order", 1), make)

    assert calls["n"] == 2
    assert store.count() == 2


# --------------------------------------------------------------------------
# CounterpartyLock -- event-time TTL, no wall clock
# --------------------------------------------------------------------------


def test_a_second_acquire_before_the_ttl_elapses_is_refused():
    lock = CounterpartyLock()
    assert lock.acquire("cp_1", "2026-08-03T10:00:00+05:30", ttl_seconds=300) is True
    # 4 minutes later -- still inside the 5-minute TTL
    assert lock.acquire("cp_1", "2026-08-03T10:04:00+05:30", ttl_seconds=300) is False


def test_acquiring_again_after_the_ttl_elapses_succeeds():
    lock = CounterpartyLock()
    lock.acquire("cp_1", "2026-08-03T10:00:00+05:30", ttl_seconds=300)
    # 5 minutes and one second later -- past the TTL
    assert lock.acquire("cp_1", "2026-08-03T10:05:01+05:30", ttl_seconds=300) is True


def test_the_lock_is_keyed_per_counterparty():
    """Two different counterparties detected at the same instant must not
    block each other -- the lock exists to stop one payer being double-fired
    on, not to serialise the whole batch."""
    lock = CounterpartyLock()
    at = "2026-08-03T10:00:00+05:30"
    assert lock.acquire("cp_1", at, ttl_seconds=300) is True
    assert lock.acquire("cp_2", at, ttl_seconds=300) is True


def test_locked_reports_without_acquiring():
    lock = CounterpartyLock()
    at = "2026-08-03T10:00:00+05:30"
    assert lock.locked("cp_1", at) is False
    lock.acquire("cp_1", at, ttl_seconds=300)
    assert lock.locked("cp_1", at) is True
    assert lock.locked("cp_1", "2026-08-03T10:06:00+05:30") is False


def test_release_frees_the_lock_immediately():
    lock = CounterpartyLock()
    at = "2026-08-03T10:00:00+05:30"
    lock.acquire("cp_1", at, ttl_seconds=300)
    lock.release("cp_1")
    assert lock.acquire("cp_1", at, ttl_seconds=300) is True


def test_a_non_positive_ttl_is_refused():
    lock = CounterpartyLock()
    with pytest.raises(ValueError):
        lock.acquire("cp_1", "2026-08-03T10:00:00+05:30", ttl_seconds=0)


def test_lock_never_reads_the_wall_clock():
    """Structural, like the envelope's own boundary test: this component
    replays a simulated timeline, so it must not import anything that could
    read the machine's clock."""
    import ast
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "pramaan"
        / "execute"
        / "locks.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today"):
            raise AssertionError("locks.py reads a wall clock at line %d" % node.lineno)
