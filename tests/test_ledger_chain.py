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
        # Not "DETECT": that is the only kind Day 1 emits, so the row already
        # holds it and the skip guard below fired every run -- leaving the kind
        # column, alone among the hashed columns, never actually tamper-tested.
        # Raw SQL has no CHECK constraint to satisfy, so any string works here.
        ("kind", "GATE"),
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


def test_a_truncated_tail_is_invisible_without_an_anchor(ledger):
    """The limit of a hash chain, asserted rather than left implicit.

    Deleting the last rows leaves every surviving row correct and every link
    intact, so re-hashing cannot find it. This test exists so that the day
    somebody "fixes" verify_chain to be anchor-free, the reason the anchor is
    there is written down in a failing test.
    """
    _fill(ledger, 5)
    ledger.conn.execute("DELETE FROM ledger WHERE seq > 2")
    assert ledger.verify_chain().ok  # internally perfect, and still wrong
    assert ledger.verify_chain().rows_checked == 2


def test_the_length_anchor_catches_a_truncated_tail(ledger):
    _fill(ledger, 5)
    head = ledger.head_hash()
    assert ledger.verify_chain(expected_rows=5, expected_head=head).ok

    ledger.conn.execute("DELETE FROM ledger WHERE seq > 3")
    result = ledger.verify_chain(expected_rows=5)
    assert not result.ok
    assert "length mismatch" in result.error


def test_the_head_anchor_catches_a_truncated_tail(ledger):
    """Either anchor alone is sufficient; the head is the stronger of the two.

    A row count can be restored by appending plausible filler. The head hash
    cannot be, which is why the golden file records it.
    """
    _fill(ledger, 5)
    head = ledger.head_hash()
    ledger.conn.execute("DELETE FROM ledger WHERE seq > 3")
    result = ledger.verify_chain(expected_head=head)
    assert not result.ok
    assert "head mismatch" in result.error

    # Refilling to the original length satisfies the count but not the head.
    _fill(ledger, 2)
    assert ledger.verify_chain(expected_rows=5).ok
    assert not ledger.verify_chain(expected_head=head).ok


def test_the_anchors_are_optional_and_default_to_the_old_behaviour(ledger):
    _fill(ledger, 3)
    assert ledger.verify_chain().ok
    assert ledger.verify_chain(expected_rows=3).ok
    assert ledger.verify_chain(expected_head=ledger.head_hash()).ok


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
    """The golden file. A behaviour change must show up as a diff.

    Regenerate deliberately, and read the diff before accepting it:
        python -m pramaan.cli demo --dev
        cp build/ledger-dev.jsonl tests/golden/ledger.jsonl

    Note that this reproduces the demo's *whole* write path -- ingest, the
    envelope gate, then outcome resolution -- rather than just ingest. It has to:
    ``make golden`` copies what the demo wrote, so a golden test that built a
    shorter ledger would go green while comparing against a file it could never
    produce.

    The write path has grown once per day it gained a writer, and the order is
    load-bearing because the hash chain is order-dependent. DETECT and GATE from
    Days 1-2; OUTCOME and EXCEPTION from Day 3, which must run *after* the gate
    because that is the order ``run_demo`` writes them in. Getting this wrong
    fails loudly here rather than quietly in the golden file, which is the point
    of comparing whole files instead of row counts.
    """
    from pathlib import Path

    from pramaan.cli import gate_events
    from pramaan.config import GOLDEN_DIR
    from pramaan.eval.resolve import resolve_batch

    golden = GOLDEN_DIR / "ledger.jsonl"
    if not golden.exists():
        pytest.skip("golden ledger not yet generated")

    conn = connect(None)
    store, ledger = EventStore(conn), Ledger(conn)
    events = sim.dev_batch(42)
    ingest(store, ledger, events)
    gate_events(events, ledger)
    resolve_batch(events, ledger=ledger)
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

def test_the_demo_tamper_probe_actually_tampers():
    """The probe the demo prints must not be able to become a no-op.

    This is a regression test for a real Day 2 failure. ``_tamper_probe`` picked
    "the middle row" and edited an ``amount_at_risk_paise`` field in it. That
    worked while every row was a DETECT row. The moment GATE rows joined the
    ledger the middle row was a GATE row, which carries no amount, so the SQL
    ``replace()`` matched nothing, the chain verified correctly, and the demo
    printed "FAILED -- a mutated row went undetected" -- reporting a
    tamper-evidence failure that was really an inert probe.

    A demo that prints a tamper probe is making a claim on screen, so the probe
    needs its own test: it must pick a row it can actually change, confirm the
    row changed, and only then report on detection. Two assertions here, and the
    second is the one that would have caught the original bug: the outcome must
    be "detected", and it must never be the inconclusive branch.
    """
    from pramaan.cli import _tamper_probe, gate_events

    conn = connect(None)
    store, ledger = EventStore(conn), Ledger(conn)
    events = sim.dev_batch(42)
    ingest(store, ledger, events)
    gate_events(events, ledger)

    # A mixed-kind ledger, which is the condition that broke the probe.
    kinds = dict(ledger.kind_counts())
    assert kinds == {"DETECT": len(events), "GATE": len(events)}

    outcome = _tamper_probe(conn)
    conn.close()
    assert outcome.startswith("detected"), outcome
    assert "inconclusive" not in outcome
    assert "FAILED" not in outcome


def test_a_gate_row_carries_its_verdict_and_its_rule_in_dedicated_columns():
    """"How often did R9 refuse an evening collection attempt?" is a GROUP BY.

    That is the whole reason ``rule_fired`` and ``decision`` are columns rather
    than payload keys: a compliance question should be a query against the
    ledger, not a grep through a log.
    """
    from pramaan.envelope import EnvelopeContext, Step, judge

    conn = connect(None)
    ledger = Ledger(conn)
    judgement = judge(
        Step("ACT_MESSAGE", "sms"),
        EnvelopeContext(
            at="2026-08-03T19:05:00+05:30",
            legal_context="collection",
            reason_code="insufficient_funds",
            dlt_template_id="1207x",
            self_identification_scripted=True,
        ),
    )
    fields = judgement.ledger_fields()
    row = ledger.append(
        "GATE",
        ts="2026-08-03T19:05:00+05:30",
        payload=fields["payload"],
        rule_fired=fields["rule_fired"],
        decision=fields["decision"],
    )
    assert row.decision == "REJECT"
    assert row.rule_fired == "R9"
    assert ledger.verify_chain().ok

    grouped = conn.execute(
        "SELECT rule_fired, decision, COUNT(*) AS n FROM ledger "
        "WHERE kind = 'GATE' GROUP BY rule_fired, decision"
    ).fetchall()
    assert [(r["rule_fired"], r["decision"], r["n"]) for r in grouped] == [
        ("R9", "REJECT", 1)
    ]
    conn.close()


def test_an_unknown_ledger_kind_is_still_rejected(ledger):
    """ADR-011 holds with four kinds as it did with one.

    The pin is deliberately exact rather than a membership check. A kind joins
    the enum on the day its writer lands, so the set growing is a decision
    somebody made and should show up as a diff in this line -- ``in
    LEDGER_KINDS`` would let a kind be added with no writer and no reviewer.

    Day 3 added OUTCOME (one row per event per run) and EXCEPTION (written only
    when an arm wanted to act and could not). PLAN is still absent because
    nothing writes it until Day 5, which is what the second half of this test
    asserts.
    """
    from pramaan.ledger.chain import LEDGER_KINDS

    assert LEDGER_KINDS == ("DETECT", "GATE", "OUTCOME", "EXCEPTION")
    with pytest.raises(ValueError):
        ledger.append("PLAN", ts=TS, payload={})
