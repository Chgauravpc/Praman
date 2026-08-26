"""The hash-chained ledger. Append-only, tamper-evident, event-time only.

PRD 12.2. This is what turns an audit trail from a log file into a **financial
control**: reconcilable against the settlement report, and therefore the artifact
a finance controller would actually accept.

Three design rules, each of which has cost somebody a debugging session
somewhere:

**Event time, never wall-clock.** ``ts`` is the event's own ``detected_at``.
A ``datetime.now()`` at write time would put the run's date into the hash, and
NFR-3 -- same seed plus same cache produces a byte-identical ledger -- becomes
unsatisfiable. There is no ``now()`` in this module, and there should never be.

**The hash covers every field, including prev_hash.** So mutating any row breaks
that row's own hash *and* every hash after it. Invariant I7.

**The kind enum holds only kinds the system actually emits.** Day 1 emits
DETECT and nothing else, so the enum contains DETECT and nothing else. PRD 12.2
is explicit about this and it is a presentation decision as much as an
engineering one: a ledger whose schema advertises events the system never writes
reads as aspirational, and a shorter enum where every value appears in the golden
file is more convincing than a longer one. Add each kind on the day it starts
being written.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from pramaan.canonical import GENESIS_HASH, canonical_json, parse_iso, sha256_hex

#: Kinds emitted as of Day 1. Grows one entry at a time, on the day the writer
#: lands. Planned, in the order the days add them: DIAGNOSIS and RECEIPT_AUDIT
#: (Day 4), PLAN and GATE (Day 2/5), ACTION (Day 5), CONVERSE and PROMISE
#: (Day 6/7), OUTCOME and EXCEPTION (Day 3/5).
LEDGER_KINDS: Tuple[str, ...] = ("DETECT",)

#: Verdicts a GATE row may carry. Empty until Day 2 writes the first one --
#: same discipline as LEDGER_KINDS.
DECISIONS: Tuple[str, ...] = ("ALLOW", "AMEND", "REJECT")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq          INTEGER PRIMARY KEY,   -- 1-based, dense, no gaps
    ts           TEXT    NOT NULL,      -- EVENT time. Never wall-clock.
    kind         TEXT    NOT NULL,
    arm          TEXT,                  -- recorded at assignment, never re-derived
    payload      TEXT    NOT NULL,      -- canonical JSON
    llm_call_ids TEXT    NOT NULL,      -- canonical JSON array of cache keys
    rule_fired   TEXT,                  -- for GATE: which of R1..R11 decided
    decision     TEXT,                  -- ALLOW | AMEND | REJECT
    cost_paise   INTEGER NOT NULL,      -- attributed cost of this action
    prev_hash    TEXT    NOT NULL,
    row_hash     TEXT    NOT NULL
);
"""


@dataclass(frozen=True)
class LedgerRow:
    seq: int
    ts: str
    kind: str
    arm: Optional[str]
    payload: Dict[str, Any]
    llm_call_ids: List[str]
    rule_fired: Optional[str]
    decision: Optional[str]
    cost_paise: int
    prev_hash: str
    row_hash: str


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    rows_checked: int
    head_hash: str
    error: Optional[str] = None

    def __bool__(self) -> bool:  # so callers can write `if verify_chain():`
        return self.ok


def _hash_row(
    seq: int,
    ts: str,
    kind: str,
    arm: Optional[str],
    payload_json: str,
    llm_call_ids_json: str,
    rule_fired: Optional[str],
    decision: Optional[str],
    cost_paise: int,
    prev_hash: str,
) -> str:
    """SHA-256 over the canonical JSON of every field, plus prev_hash.

    ``payload`` and ``llm_call_ids`` are hashed as their already-serialised
    strings rather than being re-serialised from objects. That is deliberate:
    the bytes stored in the row and the bytes fed to the hash are then the same
    bytes by construction, so a round-trip through SQLite cannot change the
    hash. Re-serialising would leave room for exactly that class of bug.
    """
    return sha256_hex(
        canonical_json(
            {
                "seq": seq,
                "ts": ts,
                "kind": kind,
                "arm": arm,
                "payload": payload_json,
                "llm_call_ids": llm_call_ids_json,
                "rule_fired": rule_fired,
                "decision": decision,
                "cost_paise": cost_paise,
                "prev_hash": prev_hash,
            }
        )
    )


class Ledger:
    """Append-only hash chain over a SQLite table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- writes ----------------------------------------------------------

    def append(
        self,
        kind: str,
        ts: str,
        payload: Dict[str, Any],
        *,
        arm: Optional[str] = None,
        llm_call_ids: Sequence[str] = (),
        rule_fired: Optional[str] = None,
        decision: Optional[str] = None,
        cost_paise: int = 0,
    ) -> LedgerRow:
        """Append one row and return it.

        ``ts`` is required and positional-ish on purpose. There is no default,
        and there is no ``now()`` fallback: a caller that does not know its
        event's time has a bug, and silently substituting the wall clock would
        hide it while breaking reproducibility.
        """
        if kind not in LEDGER_KINDS:
            raise ValueError(
                "unknown ledger kind %r. The enum holds only kinds the system "
                "actually emits (PRD 12.2) -- add %r on the day its writer "
                "lands, not before." % (kind, kind)
            )
        if decision is not None and decision not in DECISIONS:
            raise ValueError("unknown decision %r" % decision)
        if isinstance(cost_paise, bool) or not isinstance(cost_paise, int):
            raise TypeError("cost_paise must be an int (paise)")
        parse_iso(ts)  # reject a naive or malformed event time at the boundary

        prev_hash, seq = self._head()
        payload_json = canonical_json(payload)
        calls_json = canonical_json(list(llm_call_ids))
        row_hash = _hash_row(
            seq, ts, kind, arm, payload_json, calls_json,
            rule_fired, decision, cost_paise, prev_hash,
        )
        self.conn.execute(
            """
            INSERT INTO ledger (
                seq, ts, kind, arm, payload, llm_call_ids,
                rule_fired, decision, cost_paise, prev_hash, row_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq, ts, kind, arm, payload_json, calls_json,
                rule_fired, decision, cost_paise, prev_hash, row_hash,
            ),
        )
        return LedgerRow(
            seq=seq, ts=ts, kind=kind, arm=arm, payload=payload,
            llm_call_ids=list(llm_call_ids), rule_fired=rule_fired,
            decision=decision, cost_paise=cost_paise,
            prev_hash=prev_hash, row_hash=row_hash,
        )

    # -- reads -----------------------------------------------------------

    def _head(self) -> Tuple[str, int]:
        row = self.conn.execute(
            "SELECT seq, row_hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return GENESIS_HASH, 1
        return row["row_hash"], int(row["seq"]) + 1

    def head_hash(self) -> str:
        """The chain head -- one 64-hex-char string that fingerprints the run.

        This is the number invariant I1 compares and the one the demo prints.
        """
        return self._head()[0]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])

    def kind_counts(self) -> List[Tuple[str, int]]:
        return [
            (r["kind"], int(r["n"]))
            for r in self.conn.execute(
                "SELECT kind, COUNT(*) AS n FROM ledger GROUP BY kind ORDER BY kind"
            )
        ]

    def iter_raw(self) -> Iterator[sqlite3.Row]:
        """Rows in sequence order, with payloads still serialised.

        Verification and export both need the stored bytes rather than
        re-serialised objects, so they share this.
        """
        return iter(self.conn.execute("SELECT * FROM ledger ORDER BY seq"))

    # -- verification (invariant I7) --------------------------------------

    def verify_chain(self) -> ChainVerification:
        """Recompute every row hash and check the links.

        Detects three things, and it needs all three -- an attacker or a bug that
        only had to satisfy one of them would slip through:

        1. a mutated field, because the recomputed row_hash no longer matches;
        2. a broken link, because prev_hash no longer equals the previous
           row_hash -- which is what catches a *deleted* row;
        3. a re-sequenced or gapped chain, because seq is folded into the hash
           and also checked to be dense from 1.
        """
        expected_prev = GENESIS_HASH
        expected_seq = 1
        checked = 0
        for row in self.iter_raw():
            seq = int(row["seq"])
            if seq != expected_seq:
                return ChainVerification(
                    False, checked, expected_prev,
                    "sequence break: expected seq %d, found %d" % (expected_seq, seq),
                )
            if row["prev_hash"] != expected_prev:
                return ChainVerification(
                    False, checked, expected_prev,
                    "broken link at seq %d: prev_hash %s does not match the "
                    "previous row_hash %s" % (seq, row["prev_hash"][:12], expected_prev[:12]),
                )
            recomputed = _hash_row(
                seq, row["ts"], row["kind"], row["arm"], row["payload"],
                row["llm_call_ids"], row["rule_fired"], row["decision"],
                int(row["cost_paise"]), row["prev_hash"],
            )
            if recomputed != row["row_hash"]:
                return ChainVerification(
                    False, checked, expected_prev,
                    "row %d has been modified: stored hash %s, recomputed %s"
                    % (seq, row["row_hash"][:12], recomputed[:12]),
                )
            expected_prev = row["row_hash"]
            expected_seq = seq + 1
            checked += 1
        return ChainVerification(True, checked, expected_prev)

    # -- export ----------------------------------------------------------

    def export_jsonl(self, path: Path) -> Path:
        """Write the ledger as JSONL, one canonical-JSON row per line.

        Byte-stable, so it works as a golden file (NFR-6) and diffs cleanly. LF
        line endings are forced rather than left to the platform: on Windows the
        default would be CRLF and the golden comparison would fail for a reason
        that has nothing to do with the ledger.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for row in self.iter_raw():
                handle.write(
                    canonical_json(
                        {
                            "seq": int(row["seq"]),
                            "ts": row["ts"],
                            "kind": row["kind"],
                            "arm": row["arm"],
                            "payload": row["payload"],
                            "llm_call_ids": row["llm_call_ids"],
                            "rule_fired": row["rule_fired"],
                            "decision": row["decision"],
                            "cost_paise": int(row["cost_paise"]),
                            "prev_hash": row["prev_hash"],
                            "row_hash": row["row_hash"],
                        }
                    )
                )
                handle.write("\n")
        return path
