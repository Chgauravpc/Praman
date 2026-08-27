"""Outcome resolution: the properties that make B - A mean anything.

Four claims are load-bearing, and each is a way the comparison could be silently
invalid rather than merely imprecise:

1. **The window is identical across arms.** Censor the control less than the
   treatment and you manufacture an effect out of the measurement.
2. **An inert action changes nothing.** ACT_WAIT must return the control outcome
   unchanged, per event, or ~72% of the volume contributes a modelling artefact.
3. **No arm can do worse than the control.** The resolver takes the earlier of
   the unaided and action-driven recoveries, so this is structural.
4. **Latent truth never reaches the ledger.** The ledger is the artifact a
   reviewer audits and the table Day 4's SQL tool can read.
"""
from __future__ import annotations

import pytest

from pramaan import canonical
from pramaan.eval import resolve as R
from pramaan.ledger.chain import Ledger
from pramaan.sense.store import EventStore, connect, ingest
from sim import generate as sim
from sim import outcomes as O

BATCH = 1_200


@pytest.fixture(scope="module")
def events():
    return sim.generate(BATCH, seed=42)


# --------------------------------------------------------------------------
# 1. The window is a measurement, not a treatment
# --------------------------------------------------------------------------


def test_the_window_is_applied_identically_to_every_arm(events):
    """Same event, same window, whichever arm it is resolved in.

    Checked by resolving each event in *both* arms at the same window and
    asserting the unaided outcome -- the part the window governs -- is identical.
    An asymmetry here would put a difference into B - A that has nothing to do
    with recovery.
    """
    for event in events[:400]:
        a = R.resolve_one(event, "A")
        b = R.resolve_one(event, "B")
        assert a.would_recover_unaided == b.would_recover_unaided, event.event_id


def test_a_longer_window_never_reduces_recovery(events):
    """Monotone in the window, per arm. Recoveries do not un-happen.

    Catches an off-by-one in the censoring comparison, and the class of bug where
    a longer window changes which action fires rather than only how long the
    outcome is observed for.
    """
    previous = None
    for window in R.OBSERVATION_WINDOW_SWEEP:
        outcomes = R.resolve_batch(events, window_seconds=window)
        recovered = sum(1 for o in outcomes if o.recovered)
        if previous is not None:
            assert recovered >= previous, (
                "recovery fell from %d to %d when the window grew to %ds"
                % (previous, recovered, window)
            )
        previous = recovered


def test_recovery_always_lands_inside_the_window(events):
    for window in (24 * 3600, R.OBSERVATION_WINDOW_SECONDS):
        for outcome in R.resolve_batch(events[:300], window_seconds=window):
            if not outcome.recovered:
                continue
            event = next(e for e in events if e.event_id == outcome.event_id)
            elapsed = (
                canonical.parse_iso(outcome.recovered_at)
                - canonical.parse_iso(event.detected_at)
            ).total_seconds()
            assert 0 <= elapsed <= window, outcome.event_id


def test_the_window_must_exceed_the_scheduled_retry_delay():
    """Asserted at import in resolve.py; restated here as a test.

    If the window were shorter than the delay, arm B's main action would fire
    after the window closed and the result would read as "scheduled retries do
    not work" when what happened is that nobody waited for one. The two constants
    live in different modules and could drift apart.
    """
    from pramaan.envelope.reason_map import MIN_SCHEDULED_RETRY_DELAY_SECONDS

    assert R.OBSERVATION_WINDOW_SECONDS > MIN_SCHEDULED_RETRY_DELAY_SECONDS


# --------------------------------------------------------------------------
# 2. Inert actions are genuinely inert
# --------------------------------------------------------------------------


def test_inert_actions_leave_the_outcome_exactly_unchanged(events):
    """Per event, not per arm. The per-arm rates differ for a different reason.

    The demo's per-class table shows arm A and arm B rates differing by several
    points on TECH_TRANSIENT, whose action is ACT_WAIT. That looks alarming and is
    not: the two rates are computed over *different events*. This resolves the
    same event both ways, which is the claim actually being made.
    """
    for event in events:
        if R.is_actionable(event):
            continue
        a = R.resolve_one(event, "A")
        b = R.resolve_one(event, "B")
        assert (a.recovered, a.recovered_at, a.cause) == (
            b.recovered,
            b.recovered_at,
            b.cause,
        ), "%s (%s) is not inert after all" % (event.event_id, event.reason_class)


def test_actionable_is_pre_treatment_and_arm_independent(events):
    """The subgroup flag must be identical in every arm, or it leaks.

    ``actionable`` defines the pre-specified subgroup the demo reports alongside
    the headline. Conditioning on it is only legitimate because it is a function
    of ``cause_signal`` alone -- known at detection, unaffected by treatment. If it
    differed between arms, the subgroup analysis would be conditioning on
    post-treatment information.
    """
    for event in events[:400]:
        flags = {R.resolve_one(event, arm).actionable for arm in ("A", "B", "C")}
        assert len(flags) == 1, event.event_id
        assert flags.pop() == R.is_actionable(event)


def test_merchant_alerts_are_not_counted_as_recoveries(events):
    """Alerting a merchant fixes the next payment, not this one.

    Deliberate under-claiming. Folding the value of a merchant alert into a
    recovery figure would be inventing revenue, and the demo reports that value
    separately as a labelled externality instead.
    """
    for event in events:
        if event.reason_class not in ("MERCHANT_CONFIG", "INTEGRATION_BUG"):
            continue
        outcome = R.resolve_one(event, "B")
        assert outcome.action in ("ACT_ALERT_MERCHANT", "ACT_PAGE_ENGINEER")
        assert outcome.externality
        assert not outcome.recovered


# --------------------------------------------------------------------------
# 3. Arm B cannot be worse than arm A
# --------------------------------------------------------------------------


def test_no_event_does_worse_under_treatment(events):
    """Structural: the resolver takes the earlier of the two recovery paths."""
    for control, treatment in R.potential_outcomes(events):
        assert not (control.recovered and not treatment.recovered), control.event_id


def test_treatment_never_recovers_later_than_the_control_would(events):
    for control, treatment in R.potential_outcomes(events):
        if not control.recovered:
            continue
        assert canonical.parse_iso(treatment.recovered_at) <= canonical.parse_iso(
            control.recovered_at
        ), control.event_id


def test_attribution_goes_to_whichever_came_first(events):
    """An intervention firing after the customer already paid is 'organic'.

    Keeps the false-intervention rate meaningful: a message sent to someone who
    had already paid must not be credited with their payment.
    """
    for outcome in R.resolve_batch(events):
        if outcome.cause in ("retry", "route", "message", "voice"):
            # An action-caused recovery must have fired at or before any unaided
            # one, which for a recovery attributed to the action means the action
            # time is the recovery time.
            assert outcome.recovered
        if outcome.cause == "organic":
            assert outcome.would_recover_unaided


# --------------------------------------------------------------------------
# 4. The ledger never sees the answer key
# --------------------------------------------------------------------------


def test_no_ledger_row_contains_a_latent_field(events):
    """ADR-010's quarantine, extended to the ledger.

    ``would_recover_unaided`` is the counterfactual. Writing it to the ledger
    would put ground truth into the artifact a reviewer is invited to audit, and
    into a table the Day 4 investigator's read-only SQL tool can reach.
    """
    conn = connect(None)
    store, ledger = EventStore(conn), Ledger(conn)
    ingest(store, ledger, events[:300])
    R.resolve_batch(events[:300], ledger=ledger)

    forbidden = (
        "would_recover_unaided",
        "self_recovers_at",
        "capability_clears_at",
        "has_intent",
        "route_would_succeed",
        "message_response_lag",
    )
    rows = list(conn.execute("SELECT payload FROM ledger"))
    assert rows
    for (payload,) in rows:
        for name in forbidden:
            assert name not in payload, "%s leaked into a ledger payload" % name
    conn.close()


def test_resolution_writes_one_outcome_row_per_event(events):
    conn = connect(None)
    store, ledger = EventStore(conn), Ledger(conn)
    subset = events[:300]
    ingest(store, ledger, subset)
    outcomes = R.resolve_batch(subset, ledger=ledger)

    counts = dict(ledger.kind_counts())
    assert counts["OUTCOME"] == len(subset)
    expected_exceptions = sum(1 for o in outcomes if o.exception is not None)
    assert counts.get("EXCEPTION", 0) == expected_exceptions
    assert ledger.verify_chain(
        expected_rows=2 * len(subset) + expected_exceptions
    ).ok
    conn.close()


def test_an_exception_row_is_written_only_when_an_arm_could_not_act(events):
    """EXCEPTION has a real writer, and it is quiet when there is nothing to say.

    ADR-011 adds a ledger kind on the day something writes it. A run where every
    proposed action was available and allowed writes no EXCEPTION rows, and that
    is correct behaviour rather than a missing writer.
    """
    outcomes = R.resolve_batch(events)
    stuck = [o for o in outcomes if o.exception is not None]
    assert stuck, "the batch should contain at least one unavailable-channel case"
    for outcome in stuck:
        assert outcome.action == "none"
        assert outcome.proposed_action != "none"
        assert outcome.cost_paise == 0


# --------------------------------------------------------------------------
# Determinism, and the oracle boundary
# --------------------------------------------------------------------------


def test_resolution_is_deterministic(events):
    first = R.resolve_batch(events[:300])
    second = R.resolve_batch(events[:300])
    assert first == second


def test_the_oracle_is_a_pure_function(events):
    """No RNG at resolution time, or both potential outcomes are not well defined.

    Every random draw was made at generation and sits in ``LatentTruth``. If the
    oracle rolled dice here, ``potential_outcomes`` would give the unbiasedness
    test a Monte-Carlo error on the *truth* side and turn a proof into a smell
    test.
    """
    for event in events[:200]:
        calls = [
            O.resolve(event, "ACT_RETRY", 86_400, R.OBSERVATION_WINDOW_SECONDS)
            for _ in range(3)
        ]
        assert calls[0] == calls[1] == calls[2]


def test_the_oracle_can_be_replaced(events):
    """The seam that makes this productionisable. Swap the oracle, keep the rest.

    In production the outcome comes from a ``payment.captured`` webhook rather
    than from latent truth. That the resolver takes an injectable oracle is the
    whole of what productionising the measurement layer would require, and this
    test is what demonstrates the boundary is real rather than described.
    """

    def never_recovers(event, action, delay, window):
        return O.Resolution(
            recovered=False,
            recovered_at=None,
            cause="none",
            would_recover_unaided=False,
            contacted=False,
            externality=False,
        )

    outcomes = R.resolve_batch(events[:200], oracle=never_recovers)
    assert all(not o.recovered for o in outcomes)


def test_a_retry_needing_the_customer_never_succeeds_on_its_own(events):
    """AUTH_DROPOFF and INSTRUMENT_DEAD cannot be fixed by a merchant charge.

    A merchant-initiated debit cannot supply a PIN or a new card. If a retry ever
    succeeded on these classes, arm B would harvest a fabricated effect from a
    quarter of the volume.
    """
    from sim.latent import WORLD

    for event in events:
        if not WORLD[event.reason_class].retry_needs_customer:
            continue
        resolution = O.resolve(
            event, "ACT_RETRY", 3_600, R.OBSERVATION_WINDOW_SECONDS
        )
        assert resolution.cause != "retry", event.event_id


def test_a_contact_is_spent_whether_or_not_it_works(events):
    """The contact is the scarce resource, so it is counted on send.

    PRD 10.3: compute and messaging cost is not the binding constraint,
    permission to contact is. A message that was sent and ignored still used up a
    finite amount of goodwill, so ``contacted`` is set before any success test.
    """
    contacted_and_failed = 0
    for event in events:
        resolution = O.resolve(
            event, "ACT_MESSAGE", 0, R.OBSERVATION_WINDOW_SECONDS
        )
        if resolution.contacted and not resolution.recovered:
            contacted_and_failed += 1
    assert contacted_and_failed > 0


def test_every_reason_class_has_a_settle_delay():
    from pramaan.taxonomy import REASON_CLASSES

    assert set(R.SETTLE_DELAY_SECONDS) == set(REASON_CLASSES)


def test_settle_delay_does_not_stack_with_the_scheduled_retry(events):
    """max, not sum. Otherwise a FUNDS retry lands at 24h in one class and 24h
    plus a settle period in another, for no reason anybody could defend."""
    from pramaan.envelope.reason_map import MIN_SCHEDULED_RETRY_DELAY_SECONDS

    for event in events:
        if event.reason_class != "FUNDS":
            continue
        outcome = R.resolve_one(event, "B")
        assert outcome.delay_seconds == MIN_SCHEDULED_RETRY_DELAY_SECONDS


# --------------------------------------------------------------------------
# The measurement layer contains no model
# --------------------------------------------------------------------------


def test_the_eval_layer_has_no_import_edge_into_the_llm_package():
    """Day 3's whole premise: the headline number cannot be blocked by a rate limit.

    The same check Day 2 applied to the envelope, for the same reason and by the
    same method -- **parsing imports rather than grepping for a string**, because a
    grep is defeated by a line break and by any indirection, and a component
    claiming "no LLM in here" should be checkable by reading its imports.
    """
    import ast
    import pathlib

    targets = sorted(pathlib.Path("pramaan/eval").glob("*.py")) + [
        pathlib.Path("sim/latent.py"),
        pathlib.Path("sim/outcomes.py"),
    ]
    assert targets, "no eval modules found; has the layout moved?"

    offending = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if "llm" in module.split("."):
                    offending.append("%s -> %s" % (path, module))
    assert not offending, (
        "the measurement layer imports the LLM package: %r. Day 3 exists so that "
        "the headline number is produced with zero LLM calls; an import edge here "
        "is how that stops being true." % offending
    )


def test_importing_the_eval_layer_does_not_load_the_llm_package():
    """The transitive version, in a subprocess.

    An import-parse check sees direct edges only. This catches the case where some
    module in the chain pulls the LLM package in indirectly -- which would mean a
    missing API key could break the measurement layer at import time even though
    nothing in it calls a model.
    """
    import subprocess
    import sys

    code = (
        "import pramaan.eval, sim.latent, sim.outcomes, sys; "
        "loaded = [m for m in sys.modules if m.startswith('pramaan.llm')]; "
        "print(loaded)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", (
        "importing the measurement layer transitively loaded %s"
        % result.stdout.strip()
    )
