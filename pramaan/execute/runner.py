"""Wire plan -> envelope -> execute. Shadow mode is the default and the demo.

BUILD-PLAN Day 5 block D. Two entry points, and they must never be confused
for one another:

``run_shadow``    the whole pipeline runs -- planner, envelope, oracle -- and
                  **nothing is sent anywhere**. Every arm-C event is resolved
                  against the simulator exactly as arm B is, which is what
                  makes the C-B ablation and the C-A headline real bootstrap
                  contrasts rather than a narrative. This is the default, and
                  it is what ``make demo`` and every other offline target
                  exercise.
``run_execute``   the one path in this file that may touch a socket. It
                  creates real objects in Razorpay TEST mode, subject to the
                  idempotency store and the terminal-state guard
                  (``pramaan.execute.razorpay``). It is opt-in, requires real
                  credentials, and is never called by anything that must run
                  keyless (NFR-4).

PRD 12.1: "Shadow mode is the default during development ... a diff: 'here is
what I would have done.'" The one-page report ``shadow_mode_report`` produces
is that diff, shaped so a reviewer with thirty seconds gets the headline
sentence and a reviewer with five minutes gets the breakdown behind it
(BUILD-PLAN 4a's "design for three time budgets", applied one day early to the
artefact this file is a smaller cousin of).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from pramaan.config import SIM_EPOCH, Config, load_config
from pramaan.eval import bootstrap as bs
from pramaan.eval.metrics import BatchMetrics, RefusalBreakdown, compute_metrics, refusals
from pramaan.eval.resolve import Outcome, potential_outcomes, resolve_batch, true_effect
from pramaan.ledger.chain import Ledger
from pramaan.llm.client import LLMClient
from pramaan.plan.planner import Planner
from pramaan.sense.models import RiskEvent


def _rupees(paise: int) -> str:
    """Paise to rupees with Indian digit grouping. Duplicated from ``cli.py``.

    A copy, not an import: ``cli.py`` will come to import from this module for
    its ``execute`` subcommand, and importing back would be a cycle. The same
    trade as ``plan.planner.DEFAULT_CHANNEL`` -- a small, stable formatting
    helper is cheaper to duplicate once than to restructure a package boundary
    around.
    """
    whole, fraction = divmod(abs(paise), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    sign = "-" if paise < 0 else ""
    return "%sRs %s.%02d" % (sign, digits, fraction)


# --------------------------------------------------------------------------
# Shadow mode
# --------------------------------------------------------------------------


@dataclass
class ShadowResult:
    """Everything one shadow-mode run produced. The report is derived from it,
    never the other way round -- so a caller inspecting ``metrics`` directly
    sees exactly the numbers the report quotes."""

    events: List[RiskEvent]
    outcomes: List[Outcome]
    metrics: BatchMetrics
    planner: Planner
    refusal_c: RefusalBreakdown
    report: str


def run_shadow(
    events: Sequence[RiskEvent],
    *,
    client: Optional[Any] = None,
    seed: int = 42,
    resamples: int = bs.DEFAULT_RESAMPLES,
    ledger: Optional[Ledger] = None,
) -> ShadowResult:
    """Run the full pipeline in shadow mode: plan, judge, resolve. Execute nothing.

    ``client`` defaults to a fresh ``LLMClient`` built from ``load_config()`` --
    which, with no key present, means every planner call is the NFR-2
    deterministic fallback and zero tokens are spent, exactly like every other
    offline target in this project. ``ledger``, if given, receives one PLAN row
    per distinct signature the planner built.
    """
    planner = Planner(client or LLMClient(load_config()))
    outcomes = resolve_batch(events, planner=planner)
    metrics = compute_metrics(outcomes, seed=seed, resamples=resamples)
    refusal_c = refusals(outcomes, "C")
    per_plan_rate = per_signature_violation_rate(events, outcomes)

    # The EXACT C-B effect, from both potential outcomes -- no sampling error,
    # simulation only (PRD 8.2's estimator-validation move, applied to the
    # ablation instead of to B-A). While the planner has no LLM key, arm C's
    # policy is byte-identical to arm B's for every event (NFR-2 fallback), so
    # this is 0.00pp by construction -- which is the fact that makes the
    # *estimated* C-B contrast above legible: a nonzero, CI-excluding-zero
    # point estimate on a truly-zero effect is arm-assignment sampling
    # variability, not the LLM doing anything, and printing the exact number
    # next to the estimate is what stops that from being mistaken for a
    # finding. This also asks the planner for a plan under every event's
    # counterfactual arm-C assignment, not only the ones actually drawn into
    # arm C -- so it must run *before* the PLAN rows are written, or the
    # ledger would miss the signatures this diagnostic alone discovers.
    true_cb = true_effect(
        potential_outcomes(events, "B", "C", planner=planner)
    )["rate"]

    if ledger is not None:
        _write_plan_rows(ledger, planner)

    report = shadow_mode_report(
        events, metrics, planner, refusal_c, per_plan_rate, true_cb
    )
    return ShadowResult(
        events=list(events),
        outcomes=outcomes,
        metrics=metrics,
        planner=planner,
        refusal_c=refusal_c,
        report=report,
    )


def per_signature_violation_rate(
    events: Sequence[RiskEvent], outcomes: Sequence[Outcome]
) -> float:
    """The organic violation rate, once per DISTINCT signature rather than per
    event -- the planner's own proposal quality, unweighted by traffic.

    ``eval.metrics.refusals(outcomes, "C")`` already gives the *event-weighted*
    rate: how often an arm-C action was amended or refused, as real traffic
    would actually experience it. That is the right number for "how much did
    compliance cost this batch". It is the wrong number for "how good is the
    planner", because BUILD-PLAN 1.6 is explicit that the planner reasons once
    per situation, not once per event -- three reason classes carry 70-100% of
    volume (PRD 5.1), so an event-weighted rate is dominated by whichever
    signatures happen to be common, not by how many of the DISTINCT plans the
    planner wrote were clean.

    This reduces the arm-C outcomes to one per first-seen signature and reuses
    ``refusals`` on that reduced set -- no separate bookkeeping, just a
    different slice of the same recorded decisions.
    """
    from pramaan.eval.metrics import refusals as _refusals

    seen: Dict[str, Outcome] = {}
    for event, outcome in zip(events, outcomes):
        if outcome.arm != "C":
            continue
        signature = event.signature()
        seen.setdefault(signature, outcome)
    reduced = list(seen.values())
    breakdown = _refusals(reduced, "C")
    if not breakdown.total_actions_proposed:
        return 0.0
    return (breakdown.refused + breakdown.amended) / breakdown.total_actions_proposed


def _write_plan_rows(ledger: Ledger, planner: Planner) -> None:
    """One PLAN row per distinct signature. Never one per event.

    ``Planner.newly_built`` holds exactly the plans this run actually
    constructed -- a cache hit contributes nothing here, which is the ledger
    saying the same thing the memoisation ratio says in numbers: the planner
    reasoned once per situation, not once per event (BUILD-PLAN 1.6).
    """
    for signature, plan, situation_ts in planner.newly_built:
        ledger.append(
            "PLAN",
            ts=situation_ts or SIM_EPOCH,
            payload={"signature": signature, **plan.model_dump()},
            llm_call_ids=plan.llm_call_ids,
        )
    if planner.newly_built:
        ledger.conn.commit()


def shadow_mode_report(
    events: Sequence[RiskEvent],
    metrics: BatchMetrics,
    planner: Planner,
    refusal_c: RefusalBreakdown,
    per_plan_violation_rate: float = 0.0,
    true_cb_rate: float = 0.0,
) -> str:
    """The one-page, human-readable artefact. Not a log dump.

    BUILD-PLAN Day 5 gives the target shape verbatim: *"In shadow mode over
    6,000 events, Pramaan would have contacted 1,412 customers, recovered Rs X
    incremental, and been blocked 214 times by R9 on evening collection
    attempts."* That sentence leads; everything after it is the breakdown a
    reviewer with more than thirty seconds would ask for next.
    """
    n = len(events)
    a = metrics.summaries["A"]
    b = metrics.summaries["B"]
    c = metrics.summaries["C"]
    c_a = metrics.contrasts["C-A"]
    c_b = metrics.contrasts["C-B"]
    r9_blocked = refusal_c.by_rule.get("R9", 0)
    incremental_paise = c_a.intervals["money_per_event"].point * c.n

    lines: List[str] = []
    lines.append("PRAMAAN -- SHADOW MODE REPORT")
    lines.append("=" * 29)
    lines.append("")
    lines.append(
        "In shadow mode over %s events, Pramaan would have contacted %s "
        "customers, recovered an incremental %s versus taking no action, "
        "and been blocked %s times by R9 on evening collection attempts."
        % (
            format(n, ","),
            format(c.contacts, ","),
            _rupees(int(round(incremental_paise))),
            format(r9_blocked, ","),
        )
    )
    lines.append("")
    lines.append(
        "Nothing above was sent. Shadow mode runs the planner, the envelope "
        "and the outcome oracle and stops there -- zero calls to Razorpay, "
        "zero messages, zero calls placed."
    )
    lines.append("")

    lines.append("THE HEADLINE, TWO WAYS")
    lines.append("-" * 23)
    lines.append(
        "  %-28s %s" % ("C - A (does it recover money)", _fmt_pp(c_a.intervals["rate"]))
    )
    lines.append(
        "  %-28s %s" % ("C - B (did the LLM earn its place)", _fmt_pp(c_b.intervals["rate"]))
    )
    lines.append(
        "  Both are event-weighted (a proportion of events), 95%% BCa, %s "
        "resamples. C-B is the ablation PRD 8.1 asks for -- either result is "
        "publishable: a positive C-B says the LLM beat the lookup table; a "
        "null C-B says the lookup table was already capturing the recoverable "
        "value, which is itself a finding about where the headroom is." % format(
            c_a.intervals["rate"].resamples, ","
        )
    )
    lines.append("")
    lines.append(
        "  TRUE C-B effect (potential outcomes, exact, no sampling error): "
        "%+.2f pp" % (true_cb_rate * 100)
    )
    if abs(true_cb_rate) < 1e-9:
        lines.append(
            "  This is exactly zero because arm C's plan is currently the "
            "NFR-2 deterministic fallback, which is byte-identical to arm "
            "B's policy for every event. So the ESTIMATED C-B above, "
            "whatever it reads, is arm-assignment sampling variability on "
            "this one random split -- not a measurement of an LLM, because "
            "there is no LLM decision in this run to measure. A CI that "
            "happens to exclude zero here is exactly the ~1-in-20 event a "
            "95% interval produces around a null, not a discovery."
        )
    else:
        lines.append(
            "  Non-zero: arm C's plan differs from arm B's for at least one "
            "signature in this run (a live or cached LLM reply was used "
            "somewhere), so the estimated C-B above is now measuring a real "
            "policy difference rather than pure sampling noise."
        )
    lines.append("")

    lines.append("WHAT PRAMAAN WOULD HAVE DONE (arm C)")
    lines.append("-" * 37)
    lines.append("  %-24s %s" % ("events", format(c.n, ",")))
    lines.append("  %-24s %s" % ("actions taken", format(c.actions_taken, ",")))
    lines.append("  %-24s %s" % ("customer contacts", format(c.contacts, ",")))
    lines.append("  %-24s %s" % ("recovered (arm C)", format(c.recovered, ",")))
    lines.append("  %-24s %s" % ("recovered value", _rupees(c.recovered_paise)))
    lines.append("  %-24s %s" % ("cost", _rupees(c.cost_paise)))
    lines.append(
        "  %-24s %d of %d (%.1f%%)"
        % (
            "false interventions",
            c.false_interventions,
            c.contacts,
            c.false_intervention_rate * 100,
        )
    )
    lines.append("")
    lines.append(
        "  For scale, arm A (control, no action) recovers %.1f%% and arm B "
        "(the deterministic table) recovers %.1f%% on the same batch."
        % (a.rate * 100, b.rate * 100)
    )
    lines.append("")

    lines.append("BLOCKED, BY RULE -- the guardrail price list (PRD 10.4)")
    lines.append("-" * 55)
    if refusal_c.by_rule:
        for rule, count in sorted(
            refusal_c.by_rule.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(
                "  %-6s %6d blocked, %s at risk"
                % (rule, count, _rupees(refusal_c.by_rule_paise.get(rule, 0)))
            )
    else:
        lines.append("  No refusals on this batch. See the note below.")
    if refusal_c.amended_by_rule:
        lines.append("")
        for rule, count in sorted(
            refusal_c.amended_by_rule.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append("  %-6s %6d amended, not refused" % (rule, count))
    lines.append("")
    lines.append(
        "  A zero here is not evidence the envelope is inert -- "
        "tests/test_redteam_envelope.py engineers one violation per rule "
        "R1-R11 and every one is caught. This table is the *organic* rate: "
        "how often the planner itself, unprompted, proposed something the "
        "envelope had to correct. The two are reported separately on purpose "
        "(PRD 8) -- conflating them is the easiest way to make a compliance "
        "claim that sounds strong and proves nothing."
    )
    lines.append("")
    event_weighted_rate = (
        (refusal_c.refused + refusal_c.amended) / refusal_c.total_actions_proposed
        if refusal_c.total_actions_proposed
        else 0.0
    )
    lines.append(
        "  organic violation rate, EVENT-weighted   %.1f%%  (as traffic "
        "actually experienced it)" % (event_weighted_rate * 100)
    )
    lines.append(
        "  organic violation rate, PER-SIGNATURE     %.1f%%  (one vote per "
        "distinct plan the planner wrote, unweighted by traffic)"
        % (per_plan_violation_rate * 100)
    )
    lines.append(
        "  The two differ because volume concentrates on a few reason "
        "classes (PRD 5.1); a rate good for one purpose can mislead for the "
        "other, so both are printed rather than picking one."
    )
    lines.append("")

    lines.append("PLANNER -- signature memoisation (PRD 9.1, BUILD-PLAN 1.7)")
    lines.append("-" * 58)
    stats = planner.stats.as_dict()
    lines.append("  %-24s %s" % ("calls to plan_for", format(stats["signatures_seen"], ",")))
    lines.append("  %-24s %s" % ("distinct signatures", format(stats["distinct_signatures"], ",")))
    lines.append("  %-24s %.1fx" % ("memoisation ratio", stats["memoisation_ratio"]))
    lines.append("  %-24s %s" % ("built from an LLM reply", format(stats["llm_built"], ",")))
    lines.append(
        "  %-24s %s" % ("NFR-2 fallback, no key/cache", format(stats["fallback_built"], ","))
    )
    lines.append(
        "  %-24s %s"
        % ("NFR-2 fallback, provider wall", format(stats["provider_failures"], ","))
    )
    lines.append("  %-24s %s" % ("unreadable LLM replies", format(stats["parse_failures"], ",")))
    total_fallback = stats["fallback_built"] + stats["provider_failures"]
    if stats["llm_built"] == 0 and total_fallback > 0:
        lines.append("")
        lines.append(
            "  Every plan on this run came from an NFR-2 fallback: no usable "
            "planner response exists for any signature here, so arm C is "
            "currently the SAME lookup table arm B uses, applied through the "
            "same envelope -- confirmed above by the exact TRUE C-B effect "
            "reading 0.00pp. See STATE.md for what is blocked on an API key."
        )
    elif stats["provider_failures"] > 0:
        lines.append("")
        lines.append(
            "  %d signature(s) hit a provider wall (rate limit or timeout) "
            "after a network call was attempted, and fell back rather than "
            "crashing the run (NFR-2). Re-run later to give those signatures "
            "another attempt -- nothing about this run's other numbers is "
            "invalidated by it." % stats["provider_failures"]
        )
    return "\n".join(lines)


def _fmt_pp(interval) -> str:
    return "%+.2f pp  [%+.2f, %+.2f]  %s" % (
        interval.point * 100,
        interval.low * 100,
        interval.high * 100,
        interval.method,
    )


# --------------------------------------------------------------------------
# Live execution -- the one path that may touch Razorpay
# --------------------------------------------------------------------------


@dataclass
class ExecuteResult:
    """What ``run_execute`` actually did, or why it did nothing."""

    attempted: bool
    order: Optional[Dict[str, Any]] = None
    payment_link: Optional[Dict[str, Any]] = None
    idempotent_replay_was_noop: Optional[bool] = None
    terminal_state_guard_correct: Optional[bool] = None
    message: str = ""


def run_execute(
    config: Optional[Config] = None,
    *,
    transport: Optional[Any] = None,
    amount_paise: int = 150_000,
    at: str = SIM_EPOCH,
    ledger: Optional[Ledger] = None,
) -> ExecuteResult:
    """Create one real order and one real payment link, in Razorpay TEST mode.

    BUILD-PLAN Day 5's "real money-shaped calls hit Razorpay test mode", and
    the definition-of-done item "at least one real payment link created".
    Requires ``RAZORPAY_KEY_ID`` and ``RAZORPAY_KEY_SECRET`` in ``.env``; with
    neither present this returns ``attempted=False`` and a message explaining
    what is needed, rather than raising -- the same honest handling Day 4 gave
    a missing LLM key, and for the same reason: ``make demo`` and every other
    keyless command must keep working, and this function is never on that
    path.

    Demonstrates, in one call, the three PRD 12.1 properties the definition of
    done asks for: an idempotency key that makes a repeated call a no-op, and
    a terminal-state guard that re-reads order status before a second action
    would be taken.
    """
    from pramaan.execute.razorpay import (
        IdempotencyStore,
        RazorpayTestClient,
        TerminalStateGuard,
        idempotency_key,
    )

    cfg = config or load_config()
    client = RazorpayTestClient(cfg, transport=transport)
    if not client.available:
        return ExecuteResult(
            attempted=False,
            message=(
                "No Razorpay test-mode credentials configured "
                "(RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in .env). Nothing was "
                "attempted. This is the same kind of blocker Day 4 hit with "
                "the LLM key: everything that does not need a credential is "
                "built and tested; what is missing is a fact about "
                "provisioning, not about the code."
            ),
        )

    store = IdempotencyStore()
    demo_payment_id = "demo_shadow_payment_day5"
    order_key = idempotency_key(demo_payment_id, "create_order", 1)

    calls = {"n": 0}

    def _make_order() -> Dict[str, Any]:
        calls["n"] += 1
        return client.create_order(
            amount_paise, receipt="pramaan-day5-demo", idempotency_key_value=order_key
        )

    order, was_new = store.execute_once(order_key, _make_order)
    _, was_new_replay = store.execute_once(order_key, _make_order)
    idempotent_replay_was_noop = was_new and not was_new_replay and calls["n"] == 1

    link_key = idempotency_key(demo_payment_id, "create_payment_link", 1)
    link = client.create_payment_link(
        amount_paise,
        description="Pramaan recovery demo -- Razorpay TEST mode",
        idempotency_key_value=link_key,
    )

    guard = TerminalStateGuard(client)
    _, acted = guard.guard(order["id"], lambda: True)
    # A freshly-created order is not yet paid, so the guard must let this
    # through. If it did not, the guard would be refusing every action rather
    # than only the ones S1 actually governs -- the failure mode
    # ``tests/test_execute_razorpay.py`` checks for directly.
    terminal_state_guard_correct = acted

    if ledger is not None:
        ledger.append(
            "ACTION",
            ts=at,
            payload={
                "action_type": "create_order",
                "idempotency_key": order_key,
                "order_id": order.get("id"),
                "amount_paise": amount_paise,
                "idempotent_replay_was_noop": idempotent_replay_was_noop,
            },
        )
        ledger.append(
            "ACTION",
            ts=at,
            payload={
                "action_type": "create_payment_link",
                "idempotency_key": link_key,
                "payment_link_id": link.get("id"),
                "short_url": link.get("short_url"),
                "amount_paise": amount_paise,
            },
        )
        ledger.conn.commit()

    return ExecuteResult(
        attempted=True,
        order=order,
        payment_link=link,
        idempotent_replay_was_noop=idempotent_replay_was_noop,
        terminal_state_guard_correct=terminal_state_guard_correct,
        message="created 1 order and 1 payment link in Razorpay TEST mode",
    )
