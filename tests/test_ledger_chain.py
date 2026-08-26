"""Invariant I7 -- the hash chain detects any row mutation.

Three distinct attacks, because a chain that only caught one of them would give
false assurance:

- **edit** a field in place                  -> the row's own hash stops matching
- **delete** a row                           -> the next row's prev_hash stops matching
- **append** a plausible row at the end      -> its prev_hash cannot be forged
  without recomputing, which is the property that makes the ledger append-only
  rather than merely append-mostly.
"""
from __future__ import annotations

import pytest

from pramaan.canonical import GENESIS_HASH
from pramaan.ledger.chain import Ledger
from pramaan.sense.store import EventStore, connect, ingest
from sim import generate as sim

TS = "2026-08-01T10:00:00+05:30"


@pytest.fixture()
def ledger():
    conn = connect(None)
    yield Ledger(conn)
    conn.close()


def _fill(ledger: Ledger, count: int = 5) -> None:
    for index in range(count):
        ledger.append(
            "DETECT",
            ts="2026-08-0%dT1%d:00:00+05:30" % (index + 1, index),
            payload={"event_id": "evt_0042_00000%d" % index, "amount_at_risk_paise": 10_000},
            arm="ABC"[index % 3],
        )


def test_an_empty_chain_verifies(ledger):
    result = ledger.verify_chain()
    assert result.ok
    assert result.rows_checked == 0
    assert result.head_hash == GENESIS_HASH


def test_a_populated_chain_verifies(ledger):
    _fill(ledger, 5)
    result = ledger.verify_chain()
    assert result.ok
    assert result.rows_checked == 5
    assert bool(result) is True


def test_the_first_row_links_to_the_genesis_hash(ledger):
    row = ledger.append("DETECT", ts=TS, payload={"a": 1})
    assert row.prev_hash == GENESIS_HASH
    assert row.seq == 1


def test_each_row_links_to_the_previous(ledger):
    first = ledger.append("DETECT", ts=TS, payload={"a": 1})
    second = ledger.append("DETECT", ts=TS, payload={"a": 2})
    assert second.prev_hash == first.row_hash
    assert second.seq == first.seq + 1


def test_editing_a_payload_breaks_the_chain(ledger):
    """The realistic tamper: someone quietly inflates a recovered amount."""
    _fill(ledger, 5)
    assert ledger.verify_chain().ok

    ledger.conn.execute(
        "UPDATE ledger SET payload = ? WHERE seq = 3",
        ('{"amount_at_risk_paise":99999900,"event_id":"evt_0042_000002"}',),
    )
    result = ledger.verify_chain()
    assert not result.ok
    assert "modified" in result.error
    assert "3" in result.error


@pytest.mark.parametrize(
    "column,value",
    [
        ("ts", "2026-09-09T09:09:09+05:30"),
        ("kind", "DETECT"),
        ("arm", "A"),
        ("cost_paise", 4200),
        ("rule_fired", "R9"),
        ("decision", "ALLOW"),
        ("llm_call_ids", '["deadbeef"]'),
    ],
)
def test_editing_any_hashed_column_breaks_the_chain(ledger, column, value):
    """Every field is inside the hash, not just the payload.

    Parameterised so a future refactor that drops a column out of the hash fails
    here instead of silently creating a field an auditor could rewrite. Note that
    ``arm`` is in the list: PRD 12.2 records the arm at assignment and never
    re-derives it, so a mutable arm column would be a way to rewrite the
    experiment after the fact.
    """
    _fill(ledger, 4)
    before = ledger.conn.execute(
        "SELECT %s FROM ledger WHERE seq = 2" % column
    ).fetchone()[0]
    if before == value:
        pytest.skip("value is already what it would be set to")
    ledger.conn.execute("UPDATE ledger SET %s = ? WHERE seq = 2" % column, (value,))
    assert not ledger.verify_chain().ok


def test_deleting_a_row_breaks_the_chain(ledger):
    """Caught by the link check and the sequence check, not by the row hash.

    A deleted row leaves every surviving row internally consistent, which is
    exactly why prev_hash has to be verified separately.
    """
    _fill(ledger, 5)
    ledger.conn.execute("DELETE FROM ledger WHERE seq = 3")
    result = ledger.verify_chain()
    assert not result.ok
    assert "sequence break" in result.error or "broken link" in result.error


def test_reordering_rows_breaks_the_chain(ledger):
    _fill(ledger, 4)
    ledger.conn.execute("UPDATE ledger SET seq = 99 WHERE seq = 2")
    assert not ledger.verify_chain().ok


def test_an_appended_row_cannot_be_forged(ledger):
    """Someone adds a row directly via SQL, guessing the hash."""
    _fill(ledger, 3)
    ledger.conn.execute(
        """
        INSERT INTO ledger (seq, ts, kind, arm, payload, llm_call_ids,
                            rule_fired, decision, cost_paise, prev_hash, row_hash)
        VALUES (4, ?, 'DETECT', 'A', '{}', '[]', NULL, NULL, 0, ?, ?)
        """,
        (TS, "0" * 64, "f" * 64),
    )
    assert not ledger.verify_chain().ok


def test_the_head_hash_changes_with_every_append(ledger):
    seen = {ledger.head_hash()}
    for index in range(6):
        ledger.append("DETECT", ts=TS, payload={"n": index})
        seen.add(ledger.head_hash())
    assert len(seen) == 7  # genesis plus six distinct heads


def test_export_round_trips_byte_stably(tmp_path):
    """The golden-file property (NFR-6)."""
    conn = connect(None)
    store, ledger = EventStore(conn), Ledger(conn)
    ingest(store, ledger, sim.dev_batch(42))

    first = ledger.export_jsonl(tmp_path / "a.jsonl").read_bytes()
    second = ledger.export_jsonl(tmp_path / "b.jsonl").read_bytes()
    assert first == second
    # LF, not CRLF: forced in export_jsonl so the golden file compares equal on
    # Windows and Linux alike.
    assert b"\r\n" not in first
    assert first.count(b"\n") == ledger.count()
    conn.close()


def test_the_exported_ledger_matches_the_golden_file():
    """The Day 1 golden file. A behaviour change must show up as a diff.

    Regenerate deliberately, and read the diff before accepting it:
        python -m pramaan.cli demo --dev
        cp build/ledger-dev.jsonl tests/golden/ledger.jsonl
    """
    from pathlib import Path

    from pramaan.config import GOLDEN_DIR

    golden = GOLDEN_DIR / "ledger.jsonl"
    if not golden.exists():
        pytest.skip("golden ledger not yet generated")

    conn = connect(None)
    store, ledger = EventStore(conn), Ledger(conn)
    ingest(store, ledger, sim.dev_batch(42))
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        produced = ledger.export_jsonl(Path(tmp) / "ledger.jsonl").read_text(
            encoding="utf-8"
        )
    conn.close()
    assert produced == golden.read_text(encoding="utf-8")


def test_cost_paise_must_be_an_integer(ledger):
    """Money is integer paise everywhere, including here."""
    with pytest.raises(TypeError):
        ledger.append("DETECT", ts=TS, payload={}, cost_paise=1.5)


def test_an_unknown_decision_is_rejected(ledger):
    with pytest.raises(ValueError, match="unknown decision"):
        ledger.append("DETECT", ts=TS, payload={}, decision="MAYBE")
