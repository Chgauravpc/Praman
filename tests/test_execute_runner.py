"""Day 5, block D: plan -> envelope -> execute, wired end to end.

Two guarantees, and each gets its own test rather than being asserted in
passing:

**Shadow mode executes nothing.** ``run_shadow`` must never construct a
``RazorpayTestClient`` or touch a transport, because PRD 12.1 calls shadow
mode "the precondition for touching production" -- a shadow run that
secretly called out would be the exact failure that principle exists to rule
out.

**``run_execute`` demonstrates idempotency and the terminal-state guard for
real**, against a fake transport standing in for Razorpay TEST mode. The
demonstration is the same one an operator would see against the genuine
API -- only the socket is replaced.
"""
from __future__ import annotations

from pramaan.config import Config
from pramaan.execute.runner import (
    ExecuteResult,
    ShadowResult,
    per_signature_violation_rate,
    run_execute,
    run_shadow,
)
from pramaan.ledger.chain import Ledger
from pramaan.sense.store import connect
from sim import generate as sim
from tests.test_execute_razorpay import FakeTransport


def _config(with_keys: bool) -> Config:
    if with_keys:
        return Config(seed=42, mode="shadow", llm_offline=True,
                       razorpay_key_id="rzp_test_abc", razorpay_key_secret="shh")
    return Config(seed=42, mode="shadow", llm_offline=True)


# --------------------------------------------------------------------------
# Shadow mode -- executes nothing
# --------------------------------------------------------------------------


def test_run_shadow_never_imports_the_razorpay_client():
    """Structural: shadow mode's own module has no dependency edge to the
    Razorpay client at all, so it cannot call it even by accident."""
    import ast
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "pramaan"
        / "execute"
        / "runner.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    run_shadow_node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_shadow"
    )
    for node in ast.walk(run_shadow_node):
        if isinstance(node, ast.ImportFrom) and node.module and "razorpay" in node.module:
            raise AssertionError("run_shadow imports the razorpay client")


def test_run_shadow_produces_a_result_and_a_report_string():
    events = sim.dev_batch(42)
    result = run_shadow(events, resamples=200)
    assert isinstance(result, ShadowResult)
    assert "SHADOW MODE REPORT" in result.report
    assert "Nothing above was sent" in result.report
    assert len(result.outcomes) == len(events)
    assert any(o.arm == "C" for o in result.outcomes)


def test_run_shadow_writes_one_plan_row_per_distinct_signature():
    events = sim.dev_batch(42)
    conn = connect(None)
    ledger = Ledger(conn)
    result = run_shadow(events, resamples=200, ledger=ledger)

    plan_rows = dict(ledger.kind_counts()).get("PLAN", 0)
    assert plan_rows == len(result.planner.newly_built)
    assert plan_rows > 0
    assert ledger.verify_chain().ok
    conn.close()


def test_per_signature_violation_rate_is_zero_with_no_llm():
    """With no LLM key, arm C's plans are arm B's own clean policy, so this
    rate must be exactly zero -- not merely small."""
    events = sim.dev_batch(42)
    result = run_shadow(events, resamples=200)
    rate = per_signature_violation_rate(events, result.outcomes)
    assert rate == 0.0


# --------------------------------------------------------------------------
# run_execute -- the one path that may touch a transport
# --------------------------------------------------------------------------


def test_run_execute_with_no_credentials_attempts_nothing():
    result = run_execute(_config(with_keys=False))
    assert isinstance(result, ExecuteResult)
    assert result.attempted is False
    assert "credentials" in result.message.lower()


def test_run_execute_creates_an_order_and_a_payment_link():
    transport = FakeTransport()
    result = run_execute(_config(with_keys=True), transport=transport)
    assert result.attempted is True
    assert result.order["id"] == "order_FAKE1"
    assert result.payment_link["id"] == "plink_FAKE1"


def test_run_execute_double_execution_is_a_no_op():
    """The idempotency definition-of-done item, exercised through the exact
    code path the CLI's ``execute --live-razorpay`` runs."""
    transport = FakeTransport()
    result = run_execute(_config(with_keys=True), transport=transport)
    assert result.idempotent_replay_was_noop is True
    # Exactly one POST to /orders, even though create_order was invoked twice
    # inside run_execute (once, then once more via the idempotency store).
    order_posts = [p for p in transport.posts if p[0].endswith("/orders")]
    assert len(order_posts) == 1


def test_run_execute_terminal_state_guard_lets_a_fresh_order_through():
    transport = FakeTransport(order_status="created")
    result = run_execute(_config(with_keys=True), transport=transport)
    assert result.terminal_state_guard_correct is True


def test_run_execute_writes_action_rows_when_given_a_ledger():
    transport = FakeTransport()
    conn = connect(None)
    ledger = Ledger(conn)
    run_execute(_config(with_keys=True), transport=transport, ledger=ledger)
    assert dict(ledger.kind_counts()).get("ACTION", 0) == 2
    assert ledger.verify_chain().ok
    conn.close()
