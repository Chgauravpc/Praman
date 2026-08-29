"""Per-counterparty advisory lock with a TTL. PRD 12.1.

So that two detections cannot both fire on one payer -- a payment-failure event
and, moments later, a subscription-retry event for the same counterparty,
racing to both propose a contact. An advisory lock is not a database
constraint: nothing stops a caller from ignoring it, which is exactly why it is
named advisory rather than exclusive. It works because there is exactly one
caller, ``pramaan.execute.runner``, and it always asks.

**Keyed on event time, not wall-clock.** Every other clock-reading component in
this project is forbidden from calling ``datetime.now()`` (``config.py``'s
first paragraph, and ``tests/test_redteam_envelope.py`` enforces the same rule
structurally for the envelope). A lock table is state that persists across
calls, so it is tempting to reach for the wall clock the way a real service
would -- but this system replays a batch of events with their own timestamps,
and a lock timed against the machine's clock would expire at a rate that has
nothing to do with the simulated timeline. So every method here takes ``at``,
an ISO-8601 event time, and the TTL is measured against it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from pramaan.canonical import parse_iso, to_iso


@dataclass
class CounterpartyLock:
    """One process's view of who currently holds the lock, and until when."""

    _expires_at: Dict[str, str] = field(default_factory=dict)

    def acquire(self, counterparty_id: str, at: str, ttl_seconds: int) -> bool:
        """Try to take the lock. Returns whether it was acquired.

        Re-entrant for the holder that is already inside the TTL window: two
        steps of the *same* thread's own plan firing in sequence should not
        deadlock each other. What this refuses is a *second* counterparty-id
        being handed the lock while the first holder's TTL has not elapsed --
        the concurrent-detection race PRD 12.1 names.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = parse_iso(at)
        held_until = self._expires_at.get(counterparty_id)
        if held_until is not None and parse_iso(held_until) > now:
            return False
        from datetime import timedelta

        self._expires_at[counterparty_id] = to_iso(now + timedelta(seconds=ttl_seconds))
        return True

    def locked(self, counterparty_id: str, at: str) -> bool:
        """Is the lock currently held, without attempting to acquire it."""
        held_until = self._expires_at.get(counterparty_id)
        if held_until is None:
            return False
        return parse_iso(held_until) > parse_iso(at)

    def release(self, counterparty_id: str) -> None:
        self._expires_at.pop(counterparty_id, None)

    def held_count(self) -> int:
        return len(self._expires_at)
