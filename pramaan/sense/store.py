"""The event store. SQLite, and SQLite is enough (F8, anti-pattern A10).

Two properties earn their keep here.

**Idempotent insert on event_id.** Razorpay webhooks are at-least-once and
out-of-order (NFR-5). So ingestion is INSERT OR IGNORE and returns whether the
row was new; a DETECT ledger row is written only for genuinely new events. That
one detail is what makes invariant I1 hold -- replay the stream twice and the
ledger is byte-identical, because the second pass writes nothing.

**Latent ground truth lives in its own table.** Not a column on ``events``, and
there is no view joining them. The investigator gets a read-only SQL tool belt
on Day 4 and writes its own queries (F2), which means the schema itself has to
make reading the answer key hard. A separate table does that: a
``SELECT * FROM events`` -- the natural thing for an agent to write -- cannot
return it.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple

from pramaan import canonical
from pramaan.sense.models import LatentTruth, RiskEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id             TEXT PRIMARY KEY,
    source_type          TEXT NOT NULL,
    amount_at_risk_paise INTEGER NOT NULL,
    counterparty_id      TEXT NOT NULL,
    counterparty_kind    TEXT NOT NULL,
    segment              TEXT NOT NULL,
    detected_at          TEXT NOT NULL,
    decay_profile        TEXT NOT NULL,
    cause_signal         TEXT NOT NULL,
    legal_context        TEXT NOT NULL,
    available_actions    TEXT NOT NULL,
    arm                  TEXT NOT NULL,
    external_ref         TEXT
);

-- Deterministic iteration order. detected_at alone is not unique -- a busy
-- second holds several events -- so event_id breaks the tie. Without the
-- tiebreak, SQLite could legally return a different order on a different
-- machine and the ledger hash would stop reproducing.
CREATE INDEX IF NOT EXISTS idx_events_order ON events (detected_at, event_id);
CREATE INDEX IF NOT EXISTS idx_events_arm ON events (arm);
CREATE INDEX IF NOT EXISTS idx_events_counterparty ON events (counterparty_id);

-- Simulator ground truth. Deliberately NOT a column on events, and
-- deliberately not exposed through any view. See models.LatentTruth.
CREATE TABLE IF NOT EXISTS latent (
    event_id         TEXT PRIMARY KEY,
    self_recovers_at TEXT,            -- NULL means: never recovers on its own

    -- Day 3. The capability/intent decomposition (sim/latent.py): what an
    -- intervention actually interacts with, so that arm B's effect is derived
    -- rather than declared.
    --
    -- These live in the quarantined table for exactly the same reason
    -- self_recovers_at does (ADR-010), and one of them is arguably worse: an
    -- investigator that could read has_intent would know which customers will
    -- respond before contacting any of them, which is not a diagnosis, it is the
    -- answer key with extra steps.
    capability_clears_at         TEXT,     -- NULL means the block never clears
    has_intent                   INTEGER,  -- 0/1
    route_would_succeed          INTEGER,  -- 0/1
    message_response_lag_seconds INTEGER,  -- NULL means they would ignore it
    voice_response_lag_seconds   INTEGER   -- NULL means they would not answer
);

-- Run provenance. This is why the ledger does not need a run-header row: the
-- reproducibility inputs (seed, batch size, code version) are recorded here,
-- and the ledger stays a pure stream of domain events whose enum contains only
-- kinds the system actually emits (PRD 12.2).
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    seed        INTEGER NOT NULL,
    batch       TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    sim_epoch   TEXT NOT NULL,
    notes       TEXT
);
"""


def connect(path: Optional[Path]) -> sqlite3.Connection:
    """Open (or create) a store. ``None`` gives an in-memory store, for tests."""
    target = ":memory:" if path is None else str(path)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    # Integrity over throughput: this is a financial control, and a truncated
    # write would break the hash chain in a way that reads as tampering.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


class EventStore:
    """Ingestion, idempotent on event_id."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- writes ----------------------------------------------------------

    def insert(self, event: RiskEvent) -> bool:
        """Insert an event. Returns True only if it was genuinely new.

        The return value is load-bearing, not a convenience: the caller writes a
        DETECT ledger row if and only if this returns True. That is the whole
        mechanism behind invariant I1.
        """
        row = event.to_row()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO events (
                event_id, source_type, amount_at_risk_paise, counterparty_id,
                counterparty_kind, segment, detected_at, decay_profile,
                cause_signal, legal_context, available_actions, arm, external_ref
            ) VALUES (
                :event_id, :source_type, :amount_at_risk_paise, :counterparty_id,
                :counterparty_kind, :segment, :detected_at, :decay_profile,
                :cause_signal, :legal_context, :available_actions, :arm, :external_ref
            )
            """,
            row,
        )
        inserted = cur.rowcount == 1
        if inserted and event.latent is not None:
            self.conn.execute(
                "INSERT OR IGNORE INTO latent ("
                " event_id, self_recovers_at, capability_clears_at, has_intent,"
                " route_would_succeed, message_response_lag_seconds,"
                " voice_response_lag_seconds"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.latent.self_recovers_at,
                    event.latent.capability_clears_at,
                    int(event.latent.has_intent),
                    int(event.latent.route_would_succeed),
                    event.latent.message_response_lag_seconds,
                    event.latent.voice_response_lag_seconds,
                ),
            )
        return inserted

    def record_run(
        self,
        run_id: str,
        seed: int,
        batch: str,
        event_count: int,
        sim_epoch: str,
        notes: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO runs
                (run_id, seed, batch, event_count, sim_epoch, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, seed, batch, event_count, sim_epoch, notes),
        )

    # -- reads -----------------------------------------------------------

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def get(self, event_id: str, *, with_latent: bool = False) -> Optional[RiskEvent]:
        row = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        return RiskEvent.from_row(
            dict(row), self._latent(event_id) if with_latent else None
        )

    def iter_events(self, *, with_latent: bool = False) -> Iterator[RiskEvent]:
        """Every stored event, in the one deterministic order.

        ``with_latent`` defaults to False, and that default is the point: the
        agent-facing path has to be the short one to write. Only the estimator
        (Day 3) passes True, and only to validate itself against ground truth.
        """
        for row in self.conn.execute(
            "SELECT * FROM events ORDER BY detected_at, event_id"
        ):
            latent = self._latent(row["event_id"]) if with_latent else None
            yield RiskEvent.from_row(dict(row), latent)

    def _latent(self, event_id: str) -> Optional[LatentTruth]:
        """Read ground truth back out of the quarantined table.

        Every Day 3 column is read explicitly rather than with ``SELECT *``. The
        reason is a failure mode this project has already met once: a reader that
        silently returned the old two-field shape would give the oracle
        ``capability_clears_at = None`` for every event, the oracle would read
        that as "this block never clears", every intervention would fail, and the
        incremental figure would come out at zero. Nothing would raise. Naming the
        columns means a schema that has drifted fails here instead.
        """
        row = self.conn.execute(
            "SELECT self_recovers_at, capability_clears_at, has_intent,"
            " route_would_succeed, message_response_lag_seconds,"
            " voice_response_lag_seconds"
            " FROM latent WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return LatentTruth(
            self_recovers_at=row["self_recovers_at"],
            capability_clears_at=row["capability_clears_at"],
            has_intent=bool(row["has_intent"]),
            route_would_succeed=bool(row["route_would_succeed"]),
            message_response_lag_seconds=row["message_response_lag_seconds"],
            voice_response_lag_seconds=row["voice_response_lag_seconds"],
        )

    # -- projections used by the demo summary ----------------------------

    def reason_code_counts(self) -> List[Tuple[str, int]]:
        return [
            (r["cause_signal"], int(r["n"]))
            for r in self.conn.execute(
                """
                SELECT cause_signal, COUNT(*) AS n FROM events
                GROUP BY cause_signal ORDER BY n DESC, cause_signal
                """
            )
        ]

    def arm_counts(self) -> List[Tuple[str, int]]:
        return [
            (r["arm"], int(r["n"]))
            for r in self.conn.execute(
                "SELECT arm, COUNT(*) AS n FROM events GROUP BY arm ORDER BY arm"
            )
        ]

    def amount_at_risk_paise(self) -> int:
        total = self.conn.execute(
            "SELECT COALESCE(SUM(amount_at_risk_paise), 0) FROM events"
        ).fetchone()[0]
        return int(total)


def ingest(store: EventStore, ledger, events: Iterable[RiskEvent]) -> Tuple[int, int]:
    """Ingest a stream. Returns (new, duplicates).

    The one write path, so the idempotency rule cannot be forgotten at a call
    site: a DETECT row is appended if and only if the event was new. Replay the
    same stream and the second pass appends nothing, which is invariant I1.

    Note the ledger timestamp: ``event.detected_at``, the event's own time. Never
    a wall-clock read (PRD 12.2). This is the single most load-bearing line in
    the determinism story, and it is one argument.
    """
    new = duplicates = 0
    for event in events:
        if store.insert(event):
            ledger.append("DETECT", ts=event.detected_at, payload=event.detect_payload(),
                          arm=event.arm)
            new += 1
        else:
            duplicates += 1
    store.conn.commit()
    return new, duplicates
