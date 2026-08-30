"""Every value in ``LEDGER_KINDS`` is actually emitted by a real writer.

PRD 12.2's discipline is that the ledger enum must not be aspirational: "a
shorter enum where every value appears in the golden file is more convincing
than a longer one." Days 1-7 added kinds one at a time, each with a writer and a
per-kind test. This is the whole-enum check: it drives **every** real writer into
one hash-chained ledger, asserts the chain verifies, and asserts the set of kinds
emitted is exactly ``LEDGER_KINDS`` -- so a kind added to the enum without a
writer, or a writer quietly stopping, is a test failure rather than a silent
aspiration.

Nothing here is hand-appended. Each kind is produced by the same function that
produces it in production, with the same fakes the per-subsystem tests already
use (``FakeTransport`` for a Razorpay ACTION, ``ScriptedLLM`` for an investigator
DIAGNOSIS, a deliberately-wrong ``decompose`` for a canary RETRACTION). The
committed artifact this produces is ``tests/golden/all_kinds.jsonl``.
"""
from __future__ import annotations

import json
from pathlib import Path

from pramaan.cli import gate_events
from pramaan.config import Config
from pramaan.converse import voice
from pramaan.eval.resolve import resolve_batch
from pramaan.investigate import agent as A
from pramaan.investigate import canary as C
from pramaan.investigate.tools import ToolBelt, build_agent_db
from pramaan.ledger.chain import LEDGER_KINDS, Ledger
from pramaan.sense.store import EventStore, connect, ingest
from sim.generate import dev_batch, dev_batch_degraded
from sim.incident import DEV_INCIDENT, build_downtime, build_traffic, truth_for
from tests.scripted import ScriptedLLM, conclusion, tool_call
from tests.test_execute_razorpay import FakeTransport

GOLDEN = Path(__file__).resolve().parent / "golden" / "all_kinds.jsonl"
TS = "2026-08-03T10:00:00+05:30"


def _test_config() -> Config:
    """A test-mode config that lets the executor attempt against a fake transport."""
    return Config(
        seed=42,
        mode="shadow",
        llm_offline=True,
        razorpay_key_id="rzp_test_kindcoverage",
        razorpay_key_secret="secret_kindcoverage",
    )


def build_all_kinds_ledger() -> Ledger:
    """Drive every real writer into one ledger. Deterministic: no key, no network.

    The order is the natural pipeline order, so the chain reads like a run:
    sense -> gate -> resolve -> plan -> execute -> investigate -> converse ->
    canary.
    """
    from pramaan.execute.runner import run_execute, run_shadow

    conn = connect(None)  # in-memory
    store = EventStore(conn)
    ledger = Ledger(conn)

    events = dev_batch(42)

    # SENSE -> DETECT
    ingest(store, ledger, events)
    # ENVELOPE gate -> GATE
    gate_events(events, ledger)
    # OUTCOME (+ EXCEPTION, from the one event whose arm wants a shut channel)
    resolve_batch(events, ledger=ledger)
    # PLAN -- one row per distinct planner signature (deterministic fallback, no key)
    run_shadow(events, ledger=ledger)
    # ACTION -- a real create_order/create_payment_link against a fake transport
    run_execute(_test_config(), transport=FakeTransport(), ledger=ledger)

    # DIAGNOSIS + RECEIPT_AUDIT -- a real investigator session over a scripted model
    d_events, _truth = dev_batch_degraded()
    traffic = build_traffic(d_events, 42, DEV_INCIDENT)
    downtime = build_downtime(DEV_INCIDENT)
    belt = ToolBelt(build_agent_db(d_events, traffic, downtime))
    incident = A.detect_incidents(belt)[0]
    client = ScriptedLLM(
        [
            tool_call("get_downtime", window=[3, 4]),
            conclusion(
                "issuer_degradation",
                claims=[{"claim_id": "c1", "statement": "tier2 rate rose", "receipt_ids": []}],
            ),
        ]
    )
    session = A.investigate(incident, belt, client, max_turns=8)
    ledger.append(
        "DIAGNOSIS",
        ts=TS,
        payload=session.as_ledger_payload(),
        llm_call_ids=[t.llm_call_id for t in session.turns],
    )
    ledger.append(
        "RECEIPT_AUDIT",
        ts=TS,
        payload=session.audit.as_ledger_payload(),
    )

    # CONVERSE + PROMISE -- the canonical Hinglish call
    call = voice.run_demo_call()
    voice.write_call_to_ledger(ledger, call, ts=voice.DEMO_CALL_AT)

    # CANARY (confirming) -- the real incident's own decompose call
    truth = truth_for(DEV_INCIDENT)
    real_call = belt.call("decompose", {"window": [3, 4], "dimension": "segment"})
    C.write_canary_result(ledger, ts=TS, arm="A", verdict=C.run_canary([real_call], truth))

    # CANARY + RETRACTION (refuting) -- a decompose result built to disagree
    from tests.test_canary import _fake_decompose_call

    wrong = _fake_decompose_call(rate_segment="tier3", mix_segment="tier2")
    C.write_canary_result(ledger, ts=TS, arm="B", verdict=C.run_canary([wrong], truth))

    return ledger


def test_every_enum_kind_is_emitted_by_a_real_writer():
    ledger = build_all_kinds_ledger()
    emitted = {kind for kind, _ in ledger.kind_counts()}
    missing = set(LEDGER_KINDS) - emitted
    extra = emitted - set(LEDGER_KINDS)
    assert missing == set(), "enum kinds with no writer in this run: %r" % sorted(missing)
    assert extra == set(), "kinds emitted that are not in the enum: %r" % sorted(extra)
    assert ledger.verify_chain().ok, "the all-kinds ledger must be a valid hash chain"


def test_the_committed_all_kinds_golden_covers_the_enum():
    """The golden file the DoD names: every enum kind appears in it, and it
    verifies as a chain. Regenerate it with ``make golden-kinds``."""
    assert GOLDEN.exists(), "run `make golden-kinds` to generate %s" % GOLDEN.name
    kinds = set()
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if line.strip():
            kinds.add(json.loads(line)["kind"])
    assert kinds == set(LEDGER_KINDS), (
        "golden all_kinds.jsonl kinds %r != enum %r" % (sorted(kinds), sorted(LEDGER_KINDS))
    )
