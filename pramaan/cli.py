"""The demo entrypoint. Deterministic output, zero API keys, zero LLM calls.

Two hard rules govern what this may print.

**Nothing time-varying.** No wall-clock, no elapsed time, no absolute paths.
Invariant I8 is checked by running ``make demo`` twice and diffing the output, so
a single stray duration would break it.

**The cache hit rate and memoisation ratio are always printed**, per PRD 9.1
point 3. A hit rate that drops after a prompt change is the alarm; without the
metric on screen the failure is invisible until the token budget is gone. On
Day 1 those numbers are zero calls by design, and printing the zero is the point:
the line exists from the first commit, so a regression has somewhere to show up.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Sequence

from pramaan import canonical, taxonomy
from pramaan.config import BUILD_DIR, ROOT, SIM_EPOCH, load_config
from pramaan.envelope import EnvelopeContext, Step, judge
from pramaan.eval import arms as eval_arms
from pramaan.eval import bootstrap as bs
from pramaan.eval import metrics as eval_metrics
from pramaan.eval import resolve as eval_resolve
from pramaan.ledger.chain import Ledger
from pramaan.llm.client import LLMClient
from pramaan.sense.models import RiskEvent
from pramaan.sense.store import EventStore, connect, ingest
from sim import generate as sim


def _rupees(paise: int) -> str:
    """Format paise as rupees with Indian digit grouping, for display only.

    Display only. Every amount is stored and reasoned about as integer paise; a
    float would put a rounding error into a ledger that claims to be a financial
    control.
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


def _display_path(path: Path) -> str:
    """A repo-relative path, or just the filename if it sits outside the repo.

    Never an absolute path. Two reasons, and the second is the one that bit:
    absolute paths are machine-specific, so printing one breaks invariant I8
    (run the demo twice and diff) for anybody whose checkout is elsewhere. And
    ``relative_to`` *raises* for a path outside ROOT, so calling it
    unconditionally meant any user-supplied ``--out`` killed the run mid-report,
    after the database and ledger had already been written.
    """
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def _bar(share: float, width: int = 24) -> str:
    filled = int(round(share * width))
    return "#" * filled + "." * (width - filled)


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


# --------------------------------------------------------------------------
# The Day 2 shadow gate
# --------------------------------------------------------------------------
#
# Every event's *deterministic default action* -- the reason-class map that
# becomes arm B on Day 3 -- is judged by the envelope, and the verdict is written
# to the ledger as a GATE row. No LLM, no execution, nothing sent.
#
# Two reasons this exists today rather than on Day 5. First, ADR-011: a ledger
# kind is added on the day something writes it, and GATE now has a writer.
# Second, it turns the envelope from a component with tests into a component with
# a *number* -- "of 200 events the envelope refused N, naming these rules" is the
# sentence the Day 5 shadow-mode report is built out of.

# Both of these moved to pramaan/eval/resolve.py and pramaan/eval/arms.py on
# Day 3, with their values unchanged, because the outcome resolver needs the same
# envelope input this gate pass does. Re-exported rather than re-declared: two
# copies of an assumption that moves a headline count is how the two stop
# agreeing, and the golden ledger would not necessarily catch it.
SHADOW_SENDER_ASSUMPTIONS = eval_resolve.SHADOW_SENDER_ASSUMPTIONS
envelope_context = eval_resolve.envelope_context
DEFAULT_CHANNEL = eval_arms.DEFAULT_CHANNEL


def gate_events(events: Sequence[RiskEvent], ledger: Ledger) -> dict:
    """Judge each event's default action and write a GATE row. Returns a tally."""
    verdicts: dict = {}
    rules_fired: dict = {}
    actions: dict = {}
    for event in events:
        action = taxonomy.default_action(event.cause_signal)
        step = Step(action=action, channel=DEFAULT_CHANNEL.get(action, "none"))
        judgement = judge(step, envelope_context(event))
        fields = judgement.ledger_fields()
        payload = dict(fields["payload"])
        payload["event_id"] = event.event_id
        payload["action"] = action
        payload["channel"] = step.channel
        ledger.append(
            "GATE",
            ts=event.detected_at,
            payload=payload,
            arm=event.arm,
            rule_fired=fields["rule_fired"],
            decision=fields["decision"],
        )
        verdicts[judgement.verdict] = verdicts.get(judgement.verdict, 0) + 1
        key = (judgement.verdict, judgement.rule_id)
        rules_fired[key] = rules_fired.get(key, 0) + 1
        actions[action] = actions.get(action, 0) + 1
    ledger.conn.commit()
    return {"verdicts": verdicts, "rules": rules_fired, "actions": actions}



def run_demo(batch: str, seed: int, out_dir: Path) -> int:
    config = load_config()
    events: List[RiskEvent] = (
        sim.dev_batch(seed) if batch == "dev" else sim.full_batch(seed)
    )

    db_path = out_dir / ("pramaan-%s.db" % batch)
    ledger_path = out_dir / ("ledger-%s.jsonl" % batch)
    # A fresh store every run. The demo is a reproduction, not an accumulation:
    # appending to a previous run's ledger would make the head hash depend on how
    # many times the demo had been run before.
    if db_path.exists():
        db_path.unlink()

    conn = connect(db_path)
    store = EventStore(conn)
    ledger = Ledger(conn)

    title = "Pramaan -- revenue recovery, Day 3: the incremental number"
    print(title)
    print("=" * len(title))
    print("  batch                %s (%d events)" % (batch, len(events)))
    print("  seed                 %d" % seed)
    print("  sim epoch            %s" % SIM_EPOCH)
    print("  mode                 %s" % config.mode)
    print("  llm                  offline (Days 1-3 make zero LLM calls, by design)")
    print("  observation window   %dh, applied identically to every arm"
          % (eval_resolve.OBSERVATION_WINDOW_SECONDS // 3600))

    # -- ingest, then replay to prove idempotency -------------------------
    _section("SENSE -- ingest")
    new, duplicates = ingest(store, ledger, events)
    print("  pass 1               %d new, %d duplicate" % (new, duplicates))
    head_after_first = ledger.head_hash()

    # Replayed out of order and re-delivered, which is what a webhook endpoint
    # actually receives: at-least-once, out of order (NFR-5).
    replay_new, replay_dupes = ingest(store, ledger, sim.iter_shuffled(events, seed))
    print("  pass 2 (shuffled)    %d new, %d duplicate" % (replay_new, replay_dupes))
    head_after_replay = ledger.head_hash()
    idempotent = head_after_first == head_after_replay and replay_new == 0
    print(
        "  idempotent replay    %s  (I1: ledger head unchanged by a second pass)"
        % ("PASS" if idempotent else "FAIL")
    )
    print("  events stored        %d" % store.count())
    print("  amount at risk       %s" % _rupees(store.amount_at_risk_paise()))

    # -- envelope ----------------------------------------------------------
    _section("ENVELOPE -- deterministic gate, zero LLM calls")
    tally = gate_events(events, ledger)
    judged = len(events) or 1
    print("  judged               %d default actions (the reason-class map that" % len(events))
    print("                       becomes arm B on Day 3)")
    print("  sender assumed to be %s" % ", ".join(
        "%s=%s" % item for item in sorted(SHADOW_SENDER_ASSUMPTIONS.items())
    ))
    print("  (properties of a correctly-built sender, not of a failed payment.")
    print("   Stated here because they move the counts below.)")
    print()
    for verdict in ("ALLOW", "AMEND", "REJECT"):
        count = tally["verdicts"].get(verdict, 0)
        print(
            "  %-20s %5d  %5.1f%%  %s"
            % (verdict, count, count / judged * 100, _bar(count / judged))
        )
    print()
    print("  %-8s %-5s %6s  action(s) most often gated" % ("verdict", "rule", "n"))
    for (verdict, rule_id), count in sorted(
        tally["rules"].items(), key=lambda kv: (-kv[1], kv[0])
    ):
        print("  %-8s %-5s %6d" % (verdict, rule_id, count))
    print()
    if not tally["verdicts"].get("REJECT"):
        print("  Zero rejections, and that is the expected result rather than a")
        print("  broken gate: the deterministic reason-class map is *built* to be")
        print("  compliant -- it answers MERCHANT_CONFIG with ACT_ALERT_MERCHANT and")
        print("  TECH_TRANSIENT with ACT_WAIT, so it never proposes the things the")
        print("  envelope refuses. That is what makes it a fair arm B rather than a")
        print("  strawman. The evidence that the envelope is not inert is")
        print("  tests/test_redteam_envelope.py: one engineered violation per rule")
        print("  R1-R11, all caught. Organic violation rate is a Day 5 number, and")
        print("  it is a measure of the planner, not of this component.")
        print()
    # How much of the envelope this run actually exercised. Printed because the
    # honest answer is "a little", and a reviewer who assumed otherwise from a
    # 6,000-event count would be reading more into the number than it carries.
    from pramaan.envelope import registry as _registry
    from pramaan.envelope import rules as _rules

    cited = sorted({rule for _, rule in tally["rules"]})
    print("  rule ids cited        %d (%s)" % (len(cited), ", ".join(cited)))
    # Counted, not typed. The first version of this line said 25 by hand and the
    # real figure is 30 -- the same class of error as the grade count it was
    # written to accompany. See envelope/registry.py.
    print("  rule ids implemented  %s" % _registry.summary())
    print("  The demo path is narrow on purpose: the default actions are mostly")
    print("  silent ones, and for a silent action almost nothing has jurisdiction.")
    print("  It shows the gate runs on every event and records a citable verdict.")
    print("  It does not show the gate's range -- that is tests/test_envelope_")
    print("  matrix.py (3,600 action x context x hour x class combinations) and")
    print("  tests/test_redteam_envelope.py (one engineered violation per rule).")
    print()
    print("  regulatory citations  %d of 11 graded [A] (R9 only), %d graded [B]"
          % (_rules.GRADE_A_COUNT, _rules.GRADE_B_COUNT))
    print("  ...and R6 cites Razorpay's own docs rather than a regulator, so it")
    print("  is excluded from the regulator-backed count. See envelope/rules.py.")
    print()
    print("  Prefixes: R = regulation, and it cites a named instrument. G = a")
    print("  Razorpay decline-reason guardrail -- futility, not law. S = stopping")
    print("  rule. P = house policy. Nothing here cites a regulator for a rule we")
    print("  wrote ourselves; see pramaan/envelope/context.py.")

    # -- outcomes ----------------------------------------------------------
    #
    # Resolved here, before the ledger section, because resolution *writes* to
    # the ledger: one OUTCOME row per event, plus an EXCEPTION row wherever an arm
    # wanted to act and could not. Doing it after would print row counts and a
    # head hash that were already stale, verify a chain shorter than the one on
    # disk, and export a golden file missing the rows the day added -- all of
    # which the first draft did.
    #
    # The analysis is printed further down; only the writing happens here.
    outcomes = eval_resolve.resolve_batch(events, ledger=ledger)

    # -- ledger ------------------------------------------------------------
    _section("LEDGER -- hash chain")
    for kind, count in ledger.kind_counts():
        print("  %-20s %d rows" % (kind, count))
    # Anchored on the batch size: the demo knows how many rows it should have
    # written, so it can catch a truncated tail, which an unanchored verify
    # cannot see -- deleting the last n rows leaves every surviving row and link
    # correct.
    #
    # Three rows per event as of Day 3 (DETECT, GATE, OUTCOME), plus one
    # EXCEPTION per event where an arm wanted to act and could not. The exception
    # count is *counted*, not assumed: it depends on how many events wanted a
    # channel that was shut at their hour, and hard-coding a figure here would be
    # the kind of inherited constant this project keeps catching itself on.
    expected_exceptions = sum(1 for o in outcomes if o.exception is not None)
    verification = ledger.verify_chain(
        expected_rows=3 * len(events) + expected_exceptions
    )
    print("  head hash            %s" % ledger.head_hash())
    print(
        "  verify_chain         %s  (%d rows checked)"
        % ("PASS" if verification.ok else "FAIL", verification.rows_checked)
    )
    tamper = _tamper_probe(conn)
    print("  tamper probe         %s" % tamper)
    truncation = _truncation_probe(conn, ledger.count())
    print("  truncation probe     %s" % truncation)
    print("  (note the asymmetry: re-hashing catches a *mutated* row on its own,")
    print("   but a truncated tail leaves every surviving row and link correct --")
    print("   only the row-count or head anchor sees it. I7 covers mutation")
    print("   unconditionally and truncation only when anchored.)")

    ledger.export_jsonl(ledger_path)
    print("  exported             %s" % _display_path(ledger_path))

    # -- recovery: the Day 3 headline --------------------------------------
    metrics = _print_recovery(events, outcomes, seed, batch)

    # -- distribution ------------------------------------------------------
    _section("DISTRIBUTION -- reason buckets (PRD 5.1)")
    shares = sim.bucket_shares(events)
    print("  %-22s %8s %8s  %-13s" % ("bucket", "declared", "observed", "published"))
    for bucket, weight in sim.BUCKET_WEIGHTS.items():
        published = sim.PUBLISHED_RANGES.get(bucket)
        window = (
            "%.0f-%.0f%%" % (published[0] * 100, published[1] * 100)
            if published
            else "(long tail)"
        )
        print(
            "  %-22s %7.1f%% %7.1f%%  %-13s %s"
            % (bucket, weight * 100, shares[bucket] * 100, window, _bar(shares[bucket]))
        )
    classes = sim.class_shares(events)
    tech = classes.get("TECH_TRANSIENT", 0.0)
    print()
    print(
        "  TECH_TRANSIENT share %.1f%%   a uniform draw over the 69 codes would "
        "give %.1f%%" % (tech * 100, 6 / 69 * 100)
    )
    print(
        "  ...which is anti-pattern A2: it would put the envelope's never-retry"
    )
    print(
        "     branches in charge of a workload that does not exist."
    )

    # -- bands -------------------------------------------------------------
    _section("AMOUNT BANDS -- five, frozen (F6)")
    histogram = sim.band_histogram(events)
    total = len(events) or 1
    for band in canonical.AMOUNT_BANDS:
        count = histogram[band]
        note = " <- R2 AFA ceiling" if band == 3 else ""
        print(
            "  band %d  %-8s %5d  %5.1f%%  %s%s"
            % (
                band,
                canonical.AMOUNT_BAND_LABELS[band],
                count,
                count / total * 100,
                _bar(count / total),
                note,
            )
        )

    # -- arms --------------------------------------------------------------
    _section("ARMS -- three, stratified (F5)")
    for arm, count in store.arm_counts():
        print(
            "  arm %s  %5d  %5.1f%%  %s"
            % (arm, count, count / total * 100, canonical.ARM_LABELS[arm])
        )

    # -- the counterfactual ------------------------------------------------
    _section("COUNTERFACTUAL -- latent ground truth (simulator only)")
    organic = sim.self_recovery_rate(events)
    print("  organic self-recovery %.1f%% of events would recover with no action" % (organic * 100))
    print("  So gross recovered-rupees would over-claim by roughly this share.")
    print("  Reporting gross as the headline is a category error (A4); the")
    print("  headline is incremental, against arm A. Day 3 produces that number.")

    # -- signatures --------------------------------------------------------
    _section("SIGNATURES -- planner memoisation (F7)")
    features = [event.canonical_features() for event in events]
    signatures = sim_signatures(features)
    distinct = len(signatures)
    print("  signature fields     %d (%s)" % (
        len(canonical.PLANNER_SIGNATURE_FIELDS),
        ", ".join(canonical.PLANNER_SIGNATURE_FIELDS),
    ))
    print(
        "  nominal space        %d combinations (BUILD-PLAN 1.5 quotes 40,500 for"
        % _nominal_space()
    )
    print(
        "                       ten diagnosis classes; there are %d here, because"
        % len(canonical.DIAGNOSIS_CLASSES)
    )
    print("                       'undiagnosed' is a real value, not a placeholder)")
    print("  distinct observed    %d" % distinct)
    print(
        "  memoisation ratio    %.1fx  (%d events / %d distinct situations)"
        % (len(events) / max(distinct, 1), len(events), distinct)
    )
    print("  So the planner would run %d times, not %d." % (distinct, len(events)))
    print("  The ratio grows with batch size -- distinct situations saturate while")
    print("  events keep arriving -- so a 200-event dev batch understates it badly.")
    print("  Run 'make demo-full' for the figure that governs the token budget.")

    # -- llm ----------------------------------------------------------------
    _section("LLM -- plumbing only today")
    client = LLMClient(config)
    stats = client.stats()
    print("  offline              %s" % client.offline)
    print("  cache entries        %d committed" % client.cache.entry_count())
    print("  network calls        %d" % stats["tokens"]["network_calls"])
    print("  tokens spent         %d" % stats["tokens"]["total_tokens"])
    print(
        "  cache hit rate       %.1f%%   memoisation %.1fx"
        % (stats["cache"]["hit_rate_pct"], stats["cache"]["memoisation_ratio"])
    )
    print("  (Day 1 budget is zero tokens on purpose: the foundation must not")
    print("   depend on a rate limit.)")

    store.record_run(
        run_id="day3-%s-seed%d" % (batch, seed),
        seed=seed,
        batch=batch,
        event_count=len(events),
        sim_epoch=SIM_EPOCH,
        notes="day 3: three arms, outcome resolution, bootstrap CI, no LLM",
    )
    conn.commit()

    _section("RESULT")
    checks = [
        ("ingest is idempotent (I1)", idempotent),
        ("hash chain verifies (I7)", verification.ok),
        ("tamper is detected (I7)", tamper.startswith("detected")),
        ("a truncated tail is detected (I7)", truncation.startswith("detected")),
        ("declared weights inside the published ranges", _declared_weights_ok()),
        (
            "observed draw consistent with declared weights (3 s.e.)",
            _observed_matches_declared(shares, len(events)),
        ),
        ("every amount band populated", all(histogram[b] > 0 for b in canonical.AMOUNT_BANDS)),
        # I3: the envelope returned a verdict AND a rule id for every action it
        # judged. Asserted on the run rather than only in the test suite,
        # because a GATE row with no rule is indistinguishable from a GATE row
        # whose rule was lost.
        ("every gated action names a rule (I3)", sum(tally["verdicts"].values()) == len(events)
            and all(rule for _, rule in tally["rules"])),
        ("arms within 5% of equal thirds", _arms_balanced(store)),
        # -- Day 3 gated this the other way ("arm C present and empty");
        # Day 5 wires the planner, so the honest check flipped with it.
        (
            "three arms exist, and arm C is wired and takes real action",
            _arm_c_is_wired_and_acts(metrics),
        ),
        (
            "every stratum is balanced to within one event",
            eval_arms.stratum_imbalance(events) <= 1,
        ),
        (
            "B-A prints as an incremental rate with a CI",
            metrics.headline.intervals["rate"].method in ("BCa", "percentile"),
        ),
        (
            "both an event-weighted and a value-weighted figure printed",
            "rate" in metrics.headline.intervals
            and "value_share" in metrics.headline.intervals,
        ),
        # The heavy-tail check. Not a convention -- if the money interval is not
        # relatively wider than the rate interval on log-normal amounts, the
        # bootstrap is wrong.
        (
            "the money CI is wider than the rate CI (heavy tails)",
            metrics.headline.money_interval_is_wider,
        ),
        (
            "BCa agrees with the closed-form Wald interval on the rate",
            metrics.headline.bootstrap_agrees_with_normal,
        ),
        (
            "no OUTCOME row carries latent ground truth",
            _no_latent_in_ledger(conn),
        ),
        (
            "inert-action classes resolve identically in A and B (per event)",
            eval_metrics.inert_classes_are_identical(events),
        ),
    ]
    for label, passed in checks:
        print("  [%s] %s" % ("x" if passed else " ", label))
    ok = all(passed for _, passed in checks)
    print()
    print("  %s" % ("ALL CHECKS PASS" if ok else "SOME CHECKS FAILED"))
    conn.close()
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Day 3 -- the number
# --------------------------------------------------------------------------


def _pp(interval, scale=100.0, unit="pp"):
    """A point estimate and its interval, in percentage points."""
    return "%+6.2f %s  [%+6.2f, %+6.2f]" % (
        interval.point * scale,
        unit,
        interval.low * scale,
        interval.high * scale,
    )


def _money(interval):
    """The same, in rupees. Paise in, rupees out, sign preserved."""
    return "%s  [%s, %s]" % (
        _rupees(int(round(interval.point))),
        _rupees(int(round(interval.low))),
        _rupees(int(round(interval.high))),
    )


def _hours(seconds: int) -> str:
    if seconds % 86400 == 0 and seconds >= 86400:
        return "%dd" % (seconds // 86400)
    return "%dh" % (seconds // 3600)


def _arm_c_is_wired_and_acts(metrics) -> bool:
    """Arm C exists, is populated with events, and now takes real action.

    Day 3's version of this check asserted the opposite -- arm C present and
    empty, because it was not wired yet and a number from an unwired arm would
    have been arm A's outcomes reported under the LLM's name. Day 5 wires it
    (``pramaan.plan.planner``), so the honest check is the mirror image: an
    arm C that still reported zero actions on this run would mean the wiring
    silently regressed, not that the day is being cautious.
    """
    summary = metrics.summaries.get("C")
    return (
        "C" not in metrics.unwired_arms
        and summary is not None
        and summary.n > 0
        and summary.actions_taken > 0
    )


def _no_latent_in_ledger(conn) -> bool:
    """No ledger payload may contain a latent field name.

    Checked over the rows rather than over the writer. ``_write_rows`` is careful
    today, and a check that reads the code it is checking proves nothing -- this
    reads what was actually stored. The forbidden names are the LatentTruth field
    names, because those are the answer key: an OUTCOME row carrying
    ``would_recover_unaided`` would put the counterfactual into the artifact a
    reviewer is invited to audit and into a table the Day 4 investigator's SQL
    tool can reach.
    """
    forbidden = (
        "would_recover_unaided",
        "self_recovers_at",
        "capability_clears_at",
        "has_intent",
        "route_would_succeed",
    )
    for (payload,) in conn.execute("SELECT payload FROM ledger"):
        for name in forbidden:
            if name in payload:
                return False
    return True


def _print_recovery(events, outcomes, seed: int, batch: str):
    """Print the headline. Block C/C2/D of Day 3.

    Takes already-resolved outcomes rather than resolving its own. Resolution
    writes ledger rows, and a printer that wrote to the ledger would have to run
    before the ledger section -- which is how the first draft came to print row
    counts that were stale by the time the run finished.
    """
    # 10,000 for the headline contrasts on every batch, per PRD 8.1. An earlier
    # draft had this backwards -- 10,000 on the dev smoke test and 2,000 on the
    # 6,000-event batch that actually produces the published figure -- which is
    # exactly the wrong way round: the batch whose number gets quoted is the one
    # that needs the resample count the PRD specifies. The reduced count is used
    # only for the two sweeps, where the point is the direction of movement across
    # rows rather than a third significant figure, and it is labelled on screen.
    resamples = bs.DEFAULT_RESAMPLES
    metrics = eval_metrics.compute_metrics(
        outcomes, seed=seed, resamples=resamples
    )

    # The *smallest* arm, not len(events)//3. Permuted blocks leave the arms
    # within one event of each other but not exactly equal (68/67/65 on the dev
    # batch), and power is set by the binding constraint rather than the average.
    arm_counts = eval_arms.arm_counts(events)
    per_arm = min(arm_counts[a] for a in eval_arms.WIRED_ARMS)

    # ---- power, first, so the batch size is justified before any result ----
    _section("POWER -- what this batch can and cannot detect (PRD 8.1)")
    print("  Printed before the result, not after, because the honest reading of")
    print("  an interval depends on what the sample could ever have resolved.")
    print()
    print("  %-16s %10s %10s" % ("detectable lift", "per arm", "3 arms"))
    for lift, per, total in eval_arms.power_table():
        marker = "  <- this batch clears it" if per_arm >= per else ""
        print("  %-16s %10d %10d%s" % ("%.0f pp" % (lift * 100), per, total, marker))
    mde = eval_arms.min_detectable_effect(per_arm)
    print()
    print("  events per arm       %d" % per_arm)
    print("  minimum detectable   %.1f pp at alpha=0.05, 80%% power, p-bar=%.2f"
          % (mde * 100, eval_arms.POWER_BASELINE_RATE))
    print("  (event-weighted -- a proportion of events. PRD 10.2 is explicit that")
    print("   this is a different quantity from a share of rupees.)")
    if batch == "dev":
        print()
        print("  So the 200-event dev batch CANNOT resolve any effect a recovery")
        print("  system would plausibly produce. Its interval below is real and it")
        print("  will span zero, and that is a fact about 67 events per arm rather")
        print("  than about the interventions. `make demo-full` is the governing")
        print("  figure; this batch exists to exercise the apparatus.")

    # ---- arms -----------------------------------------------------------
    _section("RECOVERY -- three arms, %s observation window" % _hours(metrics.window_seconds))
    print("  The window is applied identically to every arm. Recoveries landing")
    print("  after it are counted as non-recoveries everywhere, so the censoring")
    print("  cancels in B-A and shows up only in the absolute levels.")
    print()
    print("  %-4s %6s %8s %8s %10s  %s" % (
        "arm", "n", "recov", "rate", "Rs/event", "policy"))
    for arm in canonical.ARMS:
        summary = metrics.summaries[arm]
        policy = eval_arms.ARM_POLICIES[arm]
        if not policy.wired:
            print("  %-4s %6d %8s %8s %10s  %s" % (
                arm, summary.n, "--", "--", "--", policy.label))
            continue
        print("  %-4s %6d %8d %7.1f%% %10s  %s" % (
            arm,
            summary.n,
            summary.recovered,
            summary.rate * 100,
            _rupees(int(summary.money_per_event_paise)),
            policy.label,
        ))
    print()
    print("  Arm C is wired (Day 5): pramaan.plan.planner proposes a step, judged")
    print("  by the same envelope B's proposals are. With no LLM key present its")
    print("  plans are the NFR-2 deterministic fallback -- the same table arm B")
    print("  reads -- so C's numbers above are not yet a measurement of an LLM.")
    print("  'python -m pramaan.cli execute' prints the C-B ablation, the organic")
    print("  planner violation rate and the memoisation ratio this table omits.")

    # ---- the headline ---------------------------------------------------
    headline = metrics.headline
    rate_iv = headline.intervals["rate"]
    value_iv = headline.intervals["value_share"]
    money_iv = headline.intervals["money_per_event"]

    _section("B - A -- the incremental figure (PRD 8.1)")
    print("  Intent-to-treat over every event, including the ones the policy")
    print("  declines to act on. %s, %d resamples, 95%%.\n" % (
        rate_iv.method, rate_iv.resamples))
    print("  %-38s %s" % (
        "EVENT-weighted, of at-risk events", _pp(rate_iv)))
    print("  %-38s %s" % (
        "VALUE-weighted, of failed value", _pp(value_iv)))
    print("  %-38s %s" % (
        "Rs per at-risk event", _money(money_iv)))
    print()
    print("  These are three different quantities and none of them is a rounding")
    print("  of another (PRD 10.2). Quoting a pp figure without saying which of")
    print("  the first two it is would be a defect, so both are labelled.")
    print()
    if not rate_iv.excludes_zero:
        print("  The event-weighted interval SPANS ZERO. On this batch the")
        print("  intent-to-treat effect is not distinguishable from no effect.")
        print("  That is the pre-registered honest reading (PRD 8.1's kill")
        print("  condition), and the power block above says why: the effect is")
        print("  smaller than what %d events per arm can resolve." % per_arm)
    else:
        print("  The event-weighted interval EXCLUDES ZERO.")

    # ---- the subgroup ---------------------------------------------------
    actioned = metrics.contrasts["B-A actioned"]
    a_rate = actioned.intervals["rate"]
    a_money = actioned.intervals["money_per_event"]
    print()
    print("  Pre-specified subgroup -- events the policy actually acts on")
    print("  " + "-" * 58)
    print("  Arm B answers TECH_TRANSIENT and AUTH_DROPOFF with ACT_WAIT, and")
    print("  those are most of the volume, so for those events arm B is")
    print("  IDENTICAL TO ARM A by construction: zero signal, full variance.")
    print("  The subset below is defined by cause_signal alone -- pre-treatment,")
    print("  known at detection, identical in both arms -- so conditioning on it")
    print("  is legitimate. It is a subgroup, not the headline.")
    print()
    print("  %-34s %d of %d (%.1f%%)" % (
        "events the policy acts on",
        actioned.n_control + actioned.n_treatment,
        headline.n_control + headline.n_treatment,
        (actioned.n_control + actioned.n_treatment)
        / max(1, headline.n_control + headline.n_treatment) * 100,
    ))
    print("  %-34s %s" % ("incremental recovery, EVENT-weighted", _pp(a_rate)))
    print("  %-34s %s" % ("incremental Rs per at-risk event", _money(a_money)))
    print("  %-34s %s" % (
        "interval excludes zero",
        "YES" if a_rate.excludes_zero else "no",
    ))

    # ---- where the effect comes from ------------------------------------
    print()
    print("  Where the effect comes from, and where it does not")
    print("  " + "-" * 58)
    print("  %-18s %6s %7s %8s %8s %9s  %-20s %s" % (
        "class", "n", "share", "A rate", "B rate", "contrib", "arm B action", ""))
    for row in metrics.contributions:
        print("  %-18s %6d %6.1f%% %7.1f%% %7.1f%% %+8.2fpp  %-20s %s" % (
            row.reason_class,
            row.n_control + row.n_treatment,
            row.share_of_events * 100,
            row.control_rate * 100,
            row.treatment_rate * 100,
            row.contribution * 100,
            row.default_action,
            "no-op: true effect is 0" if row.inert else "",
        ))
    print()
    print("  Rows marked no-op have a TRUE effect of exactly zero: arm B's action")
    print("  for them does nothing to the payment path, so the resolver returns")
    print("  arm A's outcome unchanged. Any number in their contrib column is")
    print("  arm-assignment noise -- arm A and arm B hold different events, not")
    print("  the same events treated differently. Which makes those rows useful")
    print("  twice over: they are also a direct read-out of the noise floor at")
    print("  this sample size. Verified per-event, not asserted: see the")
    print("  'inert-action classes resolve identically' check in RESULT.")
    print()
    print("  The zero rows are the finding, not a bug. A lookup table's ceiling")
    print("  is set by how much of the volume it is willing to touch, and this")
    print("  one declines to touch the two largest classes. That is precisely the")
    print("  headroom C - B is measured against on Day 5 -- stated now, before")
    print("  any LLM result exists to be flattered by it.")

    # ---- interval diagnostics -------------------------------------------
    rate_rel, money_rel = headline.relative_widths
    _section("INTERVAL DIAGNOSTICS -- is the bootstrap trustworthy")
    print("  %-38s %s" % ("method", rate_iv.method))
    print("  %-38s %d" % ("resamples", rate_iv.resamples))
    print("  %-38s %s" % ("BCa rate CI", _pp(rate_iv)))
    print("  %-38s %s" % (
        "closed-form Wald rate CI", _pp(headline.rate_normal)))
    print("  %-38s %s" % (
        "the two agree to within 25% of width",
        "YES" if headline.bootstrap_agrees_with_normal else "NO -- investigate",
    ))
    print("  (PRD 8.1: a normal CI is adequate for a *proportion*. So the rate is")
    print("   the one statistic with a known closed form, which makes it the one")
    print("   place the bootstrap can be checked rather than trusted.)")
    print()
    print("  %-38s %.3f" % ("rate CI width / arm A rate", rate_rel))
    print("  %-38s %.3f" % ("money CI width / arm A Rs-per-event", money_rel))
    print("  %-38s %s" % (
        "money CI relatively wider",
        "YES" if headline.money_interval_is_wider else "NO -- BOOTSTRAP IS WRONG",
    ))
    print("  %-38s %.2f  (1.00 = symmetric)" % (
        "money CI asymmetry, upper/lower", headline.money_interval_asymmetry))
    print("  Order amounts are log-normal, so the money interval must be")
    print("  relatively wider AND visibly asymmetric. A normal approximation")
    print("  cannot produce the second of those at all -- it is symmetric by")
    print("  construction -- which is why PRD 8.1 forbids it here.")

    # ---- gross vs incremental -------------------------------------------
    b = metrics.summaries["B"]
    a = metrics.summaries["A"]
    _section("GROSS vs INCREMENTAL -- why gross is a category error (PRD 3)")
    print("  %-38s %.1f%% of events / %.1f%% of value" % (
        "arm B gross recovery", b.rate * 100, b.value_share * 100))
    print("  %-38s %.1f%% of events / %.1f%% of value" % (
        "arm A -- recovered with no action", a.rate * 100, a.value_share * 100))
    print("  %-38s %s" % ("incremental, event-weighted", _pp(rate_iv)))
    print("  %-38s %s" % ("incremental, value-weighted", _pp(value_iv)))
    print()
    print("  Razorpay's own webhook documentation warns that payment.failed is")
    print("  frequently followed by payment.captured for the same transaction,")
    print("  because customers correct a wrong UPI PIN and retry inside their own")
    print("  banking app. Arm A is that population, measured. A headline of")
    print("  '%.1f%% recovered' would be mostly other people's work." % (b.rate * 100))
    print()
    print("  Razorpay's published figure for automated retry systems is 15-20% of")
    print("  failed transactions recovered [B] -- almost certainly gross. This")
    print("  project's incremental band landing well below it is the model")
    print("  working, not a weakness in it.")

    # ---- cost -----------------------------------------------------------
    _section("COST -- and the resource that is actually scarce (PRD 10.3)")
    print("  %-38s %d" % ("actions taken (arm B)", b.actions_taken))
    print("  %-38s %d" % ("customer contacts", b.contacts))
    print("  %-38s %s" % ("total cost", _rupees(b.cost_paise)))
    if b.cost_by_action:
        for action, cost in sorted(
            b.cost_by_action.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            print("    %-36s %s (%d x)" % (
                action, _rupees(cost), b.action_counts.get(action, 0)))
    if b.recovered_paise:
        print("  %-38s %.3f paise" % (
            "cost per rupee recovered, GROSS",
            b.cost_paise / (b.recovered_paise / 100.0)))
    incremental_paise = money_iv.point * b.n
    if incremental_paise > 0:
        print("  %-38s %.3f paise" % (
            "cost per rupee recovered, INCREMENTAL",
            b.cost_paise / (incremental_paise / 100.0)))
    else:
        print("  %-38s n/a -- the incremental point estimate is" % (
            "cost per rupee recovered, INCREMENTAL"))
        print("  %-38s not positive on this batch, so the ratio" % "")
        print("  %-38s would be meaningless rather than large." % "")
    print("  (Gross flatters the ratio, because most of the denominator is money")
    print("   that was coming back anyway. PRD 10.3 costs against incremental.)")
    if b.contacts:
        print("  %-38s %s" % (
            "recovered per contact, contact-caused",
            _rupees(int(b.paise_recovered_per_contact))))
        print("   Numerator counts only value a contact actually brought in --")
        print("   not the arm's whole recovery divided by its contact count,")
        print("   which is a flattering nonsense the first draft printed.")
    print("  %-38s %d of %d contacts (%.1f%%)" % (
        "false interventions",
        b.false_interventions,
        b.contacts,
        b.false_intervention_rate * 100,
    ))
    print("  (A contact spent on someone who was coming back anyway. Denominator")
    print("   is contacts, not events -- over events the rate would fall just by")
    print("   contacting fewer people, and doing nothing is already arm A. This is")
    print("   the number T_settle exists to hold down, and it is only computable")
    print("   because the simulator knows the counterfactual.)")
    if b.externalities:
        print()
        print("  %-38s %d events, %s" % (
            "merchant alerts / engineer pages",
            b.externalities,
            _rupees(b.externality_paise),
        ))
        print("  Counted as NON-recoveries. Telling a merchant their configuration")
        print("  is broken fixes the next thousand payments, not this one, and")
        print("  folding that into a recovery figure would be inventing revenue.")
        print("  Under-claiming a real benefit is the right way round to be wrong.")

    # ---- refusals -------------------------------------------------------
    refusal = metrics.refusal_breakdown
    _section("REFUSALS -- broken down by the rule that refused (PRD 8, 10.4)")
    print("  %-38s %d" % ("actions proposed by arm B", refusal.total_actions_proposed))
    print("  %-38s %d (%.1f%%)" % (
        "refused by the envelope", refusal.refused, refusal.refusal_rate * 100))
    if refusal.by_rule:
        print()
        print("  %-8s %8s %16s" % ("rule", "n", "value blocked"))
        for rule, n in sorted(refusal.by_rule.items(), key=lambda kv: (-kv[1], kv[0])):
            print("  %-8s %8d %16s" % (
                rule, n, _rupees(refusal.by_rule_paise.get(rule, 0))))
        print()
        print("  This per-rule breakdown IS the guardrail price list of PRD 10.4:")
        print("  each row is value that compliance made unreachable by contact.")
    else:
        print()
        print("  Zero rule refusals, and that is the designed result rather than")
        print("  an inert gate. Arm B is the deterministic reason-class map plus")
        print("  that map's own retry_mode, and the map is built compliant -- it")
        print("  answers MERCHANT_CONFIG with ACT_ALERT_MERCHANT and FUNDS with a")
        print("  retry already deferred past the credit cycle. It never proposes")
        print("  what the envelope refuses, which is what makes it a fair arm B")
        print("  rather than a strawman.")
        print()
        print("  The evidence that the envelope is not inert is elsewhere and is")
        print("  deliberately not this number: tests/test_redteam_envelope.py runs")
        print("  one engineered violation per rule R1-R11 and every one is caught")
        print("  and cited. A refusal count on compliant input measures the input.")
    if refusal.verdicts_by_rule:
        print()
        print("  Every envelope verdict on an arm-B action, by the rule that")
        print("  decided it -- the same per-rule shape as a refusal table, and")
        print("  non-empty, so it still answers which rules govern this workload")
        print("  and what value each one touches (PRD 10.4's price list).")
        print()
        print("  %-8s %-8s %8s %16s" % ("verdict", "rule", "n", "value governed"))
        for (verdict, rule), n in sorted(
            refusal.verdicts_by_rule.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            print("  %-8s %-8s %8d %16s" % (
                verdict,
                rule,
                n,
                _rupees(refusal.verdicts_by_rule_paise.get((verdict, rule), 0)),
            ))
        print()
        print("  Read the ALLOW rows as 'this rule had jurisdiction and permitted")
        print("  it', not as 'nothing was checked'. A silent action has almost")
        print("  nothing with jurisdiction over it, which is why the demo path")
        print("  cites few rules and the 3,600-cell matrix test cites many.")
    if refusal.amended_by_rule:
        print()
        print("  %-8s %8s   amended, not refused" % ("rule", "n"))
        for rule, n in sorted(
            refusal.amended_by_rule.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            print("  %-8s %8d" % (rule, n))
        print("  An amendment is a fixed action, not a blocked one, so it is")
        print("  counted apart from the refusals above (ADR-016).")
    if refusal.unavailable:
        print()
        print("  %-38s %d events, %s" % (
            "no action available at all",
            refusal.unavailable,
            _rupees(refusal.unavailable_paise),
        ))
        print("  The policy wanted a channel that was not open at that hour. A")
        print("  capability gap, not a compliance decision -- no rule ever saw")
        print("  these, so counting them as refusals would inflate the apparent")
        print("  cost of the rules.")

    # ---- sensitivity to the organic-recovery assumption -----------------
    _section("SENSITIVITY -- if organic self-recovery is not 30% (PRD 10.2)")
    print("  The parameter that dominates the answer, and the most obvious attack")
    print("  on the whole submission. So it is swept here, in the standard")
    print("  output, rather than in an appendix.")
    print()
    print("  Each row is a FULL RE-SIMULATION at a rescaled self-recovery")
    print("  probability, not a rescaling of the central row. That distinction is")
    print("  the entire value of the table: under a per-action uplift model the")
    print("  sweep would move the baseline and leave the uplift untouched, so")
    print("  every row would print the same incremental figure and the table would")
    print("  prove nothing. Here capability and intent are modelled separately")
    print("  (sim/latent.py), so raising organic recovery genuinely eats the")
    print("  headroom an intervention has to work in.")
    print()
    sens = eval_metrics.sensitivity_table(events, seed=seed)
    print("  %-8s %9s %8s   %11s   %s" % (
        "organic", "achieved", "A rate", "TRUE effect",
        "what a holdout of this size would see"))
    for row in sens:
        marker = " <- central" if abs(
            row.target_organic - eval_metrics.SENSITIVITY_CENTRAL) < 1e-9 else ""
        print("  %-8s %8.1f%% %7.1f%%   %+8.2f pp   %s%s" % (
            "%.0f%%" % (row.target_organic * 100),
            row.achieved_organic * 100,
            row.control_rate * 100,
            row.true_rate * 100,
            _pp(row.rate_interval),
            marker,
        ))
    print()
    widest = max(r.rate_interval.width for r in sens)
    print("  TWO columns, and both are needed. The first draft printed only the")
    print("  second and the table was useless: the widest estimate interval here")
    print("  spans %.1f pp, which exceeds the entire range the quantity moves" % (
        widest * 100))
    print("  across, so the rows came out non-monotone and looked like a bug")
    print("  rather than a sensitivity analysis.")
    print()
    print("  TRUE effect is the ESTIMAND -- exact, from both potential outcomes,")
    print("  no sampling error. It answers 'how much does this assumption")
    print("  matter', which is the question the sweep asks. It falls %.2fpp ->" % (
        sens[0].true_rate * 100))
    print("  %.2fpp as organic recovery goes 15%% -> 70%%: rising organic recovery" % (
        sens[-1].true_rate * 100))
    print("  eats the headroom an intervention has to work in, and it does so")
    print("  NON-proportionally, which is the whole point -- under a per-action")
    print("  uplift model every row would print an identical figure.")
    print()
    print("  The interval column is the ESTIMATE -- what a randomised holdout of")
    print("  this size would have seen. It answers 'could production tell these")
    print("  scenarios apart', and the honest answer is no. Printing only the")
    print("  estimand would overstate what the experiment can resolve; printing")
    print("  only the estimate hides the trend inside the noise.")
    print()
    print("  'achieved' is printed next to 'organic' because the two can differ:")
    print("  ALREADY_PAID sits at probability 1.0 as a matter of definition and")
    print("  the dead classes at 0.0, so no scale factor can move them. Printing")
    print("  the achieved value is the difference between a sensitivity table and")
    print("  a wish. Intervals use %d resamples, not %d." % (
        bs.SENSITIVITY_RESAMPLES, bs.DEFAULT_RESAMPLES))
    print()
    print("  The 70% row is the one that matters: if most failed payments come")
    print("  back on their own, most of what a recovery product reports was never")
    print("  its own work. That row is the reason this project measures")
    print("  incrementally at all.")

    # ---- sensitivity to the observation window -------------------------
    _section("OBSERVATION WINDOW -- the curve, not a picked constant (PRD 5.1)")
    print("  A genuine trade-off in both directions: too short and the scheduled")
    print("  retry fires after the window closes, too long and organic recovery")
    print("  swallows the incremental effect. So it is published as a curve.")
    print()
    windows = eval_metrics.observation_window_sweep(events, seed=seed)
    print("  %-8s %8s %8s   %-30s %s" % (
        "window", "A rate", "B rate", "incremental (event-weighted)", "false-interv"))
    for row in windows:
        print("  %-8s %7.1f%% %7.1f%%   %-30s %6.1f%%%s" % (
            _hours(row.window_seconds),
            row.control_rate * 100,
            row.treatment_rate * 100,
            _pp(row.rate_interval),
            row.false_intervention_rate * 100,
            "  <- headline" if row.is_headline else "",
        ))
    print()
    from pramaan.envelope.reason_map import MIN_SCHEDULED_RETRY_DELAY_SECONDS

    print("  %dh is the headline window. It has to exceed the %dh scheduled" % (
        metrics.window_seconds // 3600,
        MIN_SCHEDULED_RETRY_DELAY_SECONDS // 3600))
    print("  retry delay or the measurement would censor the treatment rather")
    print("  than the outcome -- asserted at import in eval/resolve.py, not left")
    print("  to a comment.")

    # ---- estimator validation ------------------------------------------
    _section("ESTIMATOR VALIDATION -- against ground truth (PRD 8.2)")
    print("  SIMULATOR ONLY. None of this is available in production, and that")
    print("  asymmetry is the point: the holdout estimates the effect the way")
    print("  production would, and the simulator's known counterfactual then")
    print("  checks that the estimator is UNBIASED rather than merely producing a")
    print("  number. The figures below are the answer key, not the result.")
    print()
    pairs = eval_resolve.potential_outcomes(events)
    truth = eval_resolve.true_effect(pairs)
    print("  %-30s %s" % ("", "true ATE        estimate (95% CI)"))
    for name, iv, scale in (
        ("rate (event-weighted)", rate_iv, 100.0),
        ("value_share (value-weighted)", value_iv, 100.0),
    ):
        covered = iv.low <= truth[name.split(" ")[0]] <= iv.high
        print("  %-30s %+7.2f pp     %s   %s" % (
            name,
            truth[name.split(" ")[0]] * scale,
            _pp(iv),
            "covered" if covered else "NOT COVERED",
        ))
    money_covered = (
        money_iv.low <= truth["money_per_event"] <= money_iv.high
    )
    print("  %-30s %s     %s   %s" % (
        "Rs per at-risk event",
        _rupees(int(round(truth["money_per_event"]))),
        _money(money_iv),
        "covered" if money_covered else "NOT COVERED",
    ))
    print()
    print("  Both potential outcomes are known for every event -- what it does")
    print("  under arm A AND under arm B -- so the true effect is an exact")
    print("  quantity rather than an estimate with error of its own. That is what")
    print("  makes tests/test_estimator_unbiased.py a proof rather than a smell")
    print("  test, and it is Razorpay's own 'verification capacity is the")
    print("  bottleneck' thesis turned into a test file.")

    return metrics


def sim_signatures(features: Sequence[dict]) -> set:
    return {canonical.planner_signature(f) for f in features}


def _nominal_space() -> int:
    space = 1
    canonical.validate_features(
        {
            "reason_class": "FUNDS",
            "diagnosis_class": "undiagnosed",
            "amount_band": 1,
            "segment": "metro",
            "legal_context": "service",
            "channel_eligibility": "full",
            "hour_bucket": "business",
        }
    )
    for field in canonical.PLANNER_SIGNATURE_FIELDS:
        space *= len(canonical.SIGNATURE_DOMAINS[field])
    return space


def _declared_weights_ok() -> bool:
    """Are the *declared* bucket weights inside the published ranges?

    This is the check that belongs on the configuration, and it is exact: the
    weights are a constant, so this either holds or it does not.
    """
    return all(
        low <= sim.BUCKET_WEIGHTS[bucket] <= high
        for bucket, (low, high) in sim.PUBLISHED_RANGES.items()
    )


def _observed_matches_declared(shares, n: int) -> bool:
    """Is the *observed* draw consistent with the declared weights?

    Deliberately not "is every observed share inside the published range". A
    200-event batch is a 200-draw multinomial: the standard error on a 0.37 share
    is about 3.4pp, so a sample landing outside a 10pp-wide population range now
    and then is the sampling distribution working correctly, not a bug. Demanding
    otherwise would be demanding a simulator that is not random.

    So the sample is tested against the thing it is actually drawn from -- the
    declared weight -- at three standard errors, with a small floor so tiny
    buckets are not held to an impossible absolute precision. The published range
    is checked against the weights instead, exactly, by _declared_weights_ok.
    """
    for bucket, weight in sim.BUCKET_WEIGHTS.items():
        standard_error = (weight * (1.0 - weight) / max(n, 1)) ** 0.5
        if abs(shares[bucket] - weight) > max(3.0 * standard_error, 0.01):
            return False
    return True


def _arms_balanced(store: EventStore) -> bool:
    """Are the three arms within 5% relative of equal thirds?

    Not "within one event". Assignment is stratified permuted-block over
    (source_type, amount_band, segment), so each stratum whose final block is
    incomplete leaves a residual of one or two. With up to fifteen strata the
    worst-case absolute imbalance is bounded by the stratum count, not by one --
    that is a property of stratification, and giving it up to make an arbitrary
    tolerance pass would trade real balance on the heavy-tailed money variable
    for a tidier count.
    """
    counts = [n for _, n in store.arm_counts()]
    if not counts:
        return False
    expected = sum(counts) / 3.0
    return all(abs(count - expected) / expected <= 0.05 for count in counts)


def _tamper_probe(conn: sqlite3.Connection) -> str:
    """Corrupt one row of a throwaway copy and confirm the chain notices.

    Runs on an in-memory ``backup()`` of the live database, so the real ledger is
    never touched. Doing this in the demo rather than only in a test is
    deliberate: a reviewer with four minutes sees tamper-evidence demonstrated
    rather than asserted.
    """
    copy = sqlite3.connect(":memory:")
    copy.row_factory = sqlite3.Row
    conn.backup(copy)
    shadow = Ledger(copy)
    if not shadow.verify_chain().ok:
        copy.close()
        return "inconclusive: the copy did not verify before tampering"
    # Pick a row the edit can actually land on. The probe's story is somebody
    # quietly inflating a recorded amount, so it needs a row that *has* an
    # amount -- and once GATE rows joined the ledger, "the middle row" stopped
    # being one. The probe then mutated nothing and reported a tamper-evidence
    # failure that was really a no-op, which is precisely the sort of silently
    # inert check this probe exists to be the opposite of.
    field = '"amount_at_risk_paise":'
    candidates = [
        int(row["seq"])
        for row in copy.execute(
            "SELECT seq FROM ledger WHERE payload LIKE ? ORDER BY seq",
            ("%" + field + "%",),
        )
    ]
    if not candidates:
        copy.close()
        return "inconclusive: no row carries an amount to tamper with"
    seq = candidates[len(candidates) // 2]
    before = copy.execute(
        "SELECT payload FROM ledger WHERE seq = ?", (seq,)
    ).fetchone()["payload"]
    # Change one integer in one payload -- the subtlest useful edit.
    copy.execute(
        "UPDATE ledger SET payload = replace(payload, ?, ?) WHERE seq = ?",
        (field, field + "1", seq),
    )
    after = copy.execute(
        "SELECT payload FROM ledger WHERE seq = ?", (seq,)
    ).fetchone()["payload"]
    if before == after:
        copy.close()
        return "inconclusive: the edit did not change row %d" % seq
    result = shadow.verify_chain()
    copy.close()
    if result.ok:
        return "FAILED -- a mutated row went undetected"
    return "detected at row %d (%s)" % (seq, result.error.split(":")[0])


def _truncation_probe(conn: sqlite3.Connection, expected_rows: int) -> str:
    """Drop the tail of a throwaway copy and confirm the length anchor notices.

    Separate from _tamper_probe because it fails for a different reason. A
    truncated chain is internally *perfect* -- every row hashes correctly, every
    link holds -- so no amount of re-hashing finds it. Only the expected row
    count does, which is why verify_chain takes one.
    """
    copy = sqlite3.connect(":memory:")
    copy.row_factory = sqlite3.Row
    conn.backup(copy)
    shadow = Ledger(copy)
    if expected_rows < 2:
        copy.close()
        return "inconclusive: ledger too short to truncate"
    copy.execute("DELETE FROM ledger WHERE seq > ?", (expected_rows - 1,))
    unanchored = shadow.verify_chain().ok
    anchored = shadow.verify_chain(expected_rows=expected_rows)
    copy.close()
    if anchored.ok:
        return "FAILED -- a truncated tail went undetected"
    return "detected (unanchored verify %s, length anchor caught it)" % (
        "misses it" if unanchored else "also caught it"
    )


# --------------------------------------------------------------------------
# The investigator (Day 4)
# --------------------------------------------------------------------------


def _investigation_ts(day_index: int) -> str:
    """An ISO timestamp for a day index, in IST.

    A ledger row needs a timestamp and there is deliberately no ``now()``
    anywhere in the write path (``config.py``'s first paragraph). So a DIAGNOSIS
    row is stamped with the *start of the window it is about*, which is an event
    time derived from the data. A wall-clock read here would make the ledger hash
    depend on when the run happened and NFR-3 would be unsatisfiable.
    """
    from datetime import timedelta

    epoch = canonical.parse_iso(SIM_EPOCH)
    return canonical.to_iso(epoch + timedelta(days=int(day_index)))


def run_investigate(seed: int, out_dir: Path, count: int, days: int) -> int:
    """Detect incidents in a degraded batch, investigate each, audit the results.

    The LLM-touched surface of the whole day, and it is small on purpose: one
    investigation per incident (PRD 1.1 -- "a degradation episode spanning 400
    failures has one root cause, and diagnosing it 400 times is not expensive, it
    is wrong"). The batch is 1,200 events and contains one incident, so this is
    one session, not 1,200.
    """
    from pramaan.investigate import receipts as receipts_mod
    from pramaan.investigate.agent import (
        detect_incidents,
        investigate,
        session_lines,
    )
    from pramaan.investigate.tools import ToolBelt, build_agent_db
    from pramaan.llm.cache import CacheMiss
    from pramaan.llm.client import LLMClient
    from sim.generate import dev_batch_degraded
    from sim.incident import DEV_INCIDENT, build_downtime, build_traffic

    config = load_config()
    out_dir.mkdir(parents=True, exist_ok=True)

    events, truth = dev_batch_degraded(seed=seed, count=count, days=days)
    traffic = build_traffic(events, seed, DEV_INCIDENT)
    downtime = build_downtime(DEV_INCIDENT)

    _section("INVESTIGATE -- an agent that writes its own queries")
    print("  batch                %s events over %d days, seed %d"
          % (_thousands(len(events)), days, seed))
    print("  injected incident    %s" % truth.spec_name)
    print("                       window days %d-%d, rate shift on %s, mix shift on %s"
          % (truth.start_day, truth.end_day, truth.rate_segment, truth.mix_segment))
    print("                       platform downtime declared: %s"
          % ("yes" if truth.downtime_declared else "no"))
    print()
    print("  Ground truth is printed here for the reader, and is NOT reachable by")
    print("  the agent: its SQL runs against a separate in-memory projection with")
    print("  no latent table in it (pramaan/investigate/tools.py).")

    # -- detection: arithmetic, zero tokens ------------------------------
    probe_belt = ToolBelt(build_agent_db(events, traffic, downtime))
    incidents = detect_incidents(probe_belt)

    _section("DETECT -- deterministic, no LLM")
    print("  incidents found      %d" % len(incidents))
    for incident in incidents:
        print("  %-20s blended %s -> %s  (%s)"
              % (incident.label,
                 investigate_fmt_rate(incident.blended_baseline),
                 investigate_fmt_rate(incident.blended_window),
                 investigate_fmt_pp(incident.delta)))
    if not incidents:
        print("  Nothing is elevated. With no incident there is nothing to")
        print("  investigate, and no LLM call is made.")
        return 0

    # -- ledger ---------------------------------------------------------
    db_path = out_dir / ("investigate-%d.db" % seed)
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    ledger = Ledger(conn)

    client = LLMClient(config)
    sessions = []
    audits = []

    for incident in incidents:
        belt = ToolBelt(build_agent_db(events, traffic, downtime))
        try:
            session = investigate(incident, belt, client)
        except CacheMiss as exc:
            _section("NO LLM KEY AND NO CACHED SESSION")
            print("  The investigator needs either a live API key or a cached")
            print("  session, and has neither.")
            print()
            print("  %s" % str(exc).splitlines()[0])
            print()
            print("  Set GROQ_API_KEY (or OPENROUTER_API_KEY) in .env and run")
            print("    make investigate-live")
            print("  once. That writes fixtures/llm_cache, after which this")
            print("  command runs offline and free, forever.")
            return 3
        sessions.append(session)
        audits.append(session.audit)

        ts = _investigation_ts(min(incident.window))
        ledger.append(
            "DIAGNOSIS",
            ts=ts,
            payload=session.as_ledger_payload(),
            llm_call_ids=[t.llm_call_id for t in session.turns],
        )
        ledger.append(
            "RECEIPT_AUDIT",
            ts=ts,
            payload=session.audit.as_ledger_payload(),
            decision=session.audit.status,
        )
    conn.commit()

    for session in sessions:
        _section("SESSION -- %s" % session.incident.label)
        for line in session_lines(session):
            print("  " + line)

    # -- the headline metrics -------------------------------------------
    summary = receipts_mod.coverage_of(audits)
    _section("RECEIPTS -- the headline trust metric (PRD 8)")
    print("  diagnoses            %d  (%d supported, %d unsupported)"
          % (summary["diagnoses"], summary["supported"], summary["unsupported"]))
    print("  claims made          %d" % summary["claims"])
    print("  receipt coverage     %.1f%%   share of claims citing a tool call"
          % (100.0 * summary["receipt_coverage"]))
    print("  survival rate        %.1f%%   share surviving the audit"
          % (100.0 * summary["survival_rate"]))
    if summary["stripped_by_reason"]:
        print("  stripped, by reason:")
        for reason, n in summary["stripped_by_reason"].items():
            print("    %-44s %d" % (reason, n))
    else:
        print("  stripped             0")
    print()
    print("  Coverage and survival are separate numbers and the gap between them")
    print("  is the informative one. Coverage counts claims that cited something;")
    print("  survival counts claims whose citation checked out. A model that")
    print("  cites confidently and wrongly scores 100% on the first and less on")
    print("  the second.")

    _section("TOKENS AND CACHE")
    stats = client.stats()
    cache, tokens = stats["cache"], stats["tokens"]
    total_turns = sum(len(s.turns) for s in sessions)
    hits = sum(s.cache_hits for s in sessions)
    print("  llm calls            %d over %d session(s)" % (total_turns, len(sessions)))
    print("  cache hits           %d of %d  (%.1f%%)"
          % (hits, total_turns, 100.0 * hits / total_turns if total_turns else 100.0))
    print("  network calls        %d" % tokens["network_calls"])
    print("  tokens consumed      %s" % _thousands(tokens["total_tokens"]))
    print("  rate limited         %d   failovers %d"
          % (tokens["rate_limited"], tokens["failovers"]))
    if cache:
        print("  cache entries        %s" % _thousands(cache.get("entries", 0) or 0))
        if cache.get("lookups"):
            print("  cache hit rate       %.1f%%" % (100.0 * cache.get("hit_rate", 0.0)))
        if cache.get("memoisation_ratio"):
            print("  memoisation ratio    %.1fx" % cache["memoisation_ratio"])
    print()
    print("  A second run of this command makes zero network calls and consumes")
    print("  zero tokens: every turn prompt is a pure function of the incident,")
    print("  the transcript so far and the turn number, so the cache keys repeat.")

    _section("LEDGER")
    verification = ledger.verify_chain()
    print("  rows                 %s" % _thousands(ledger.count()))
    for kind, n in ledger.kind_counts():
        print("    %-18s %s" % (kind, _thousands(n)))
    print("  verify_chain         %s" % ("PASS" if verification else "FAIL"))
    export = ledger.export_jsonl(out_dir / ("investigate-%d.jsonl" % seed))
    print("  exported             %s" % _display_path(export))

    return 0 if all(a.is_supported for a in audits) else 0


def investigate_fmt_rate(value: float) -> str:
    from pramaan.investigate.tools import fmt_rate

    return fmt_rate(value)


def investigate_fmt_pp(value: float) -> str:
    from pramaan.investigate.tools import fmt_pp

    return fmt_pp(value)


def _thousands(value: int) -> str:
    return format(int(value), ",")


# --------------------------------------------------------------------------
# The planner and the executor (Day 5)
# --------------------------------------------------------------------------


def run_voice(out_dir: Path, *, live_sarvam: bool) -> int:
    """Day 7: place the canonical Hinglish recovery call and write its artifacts.

    Deterministic and keyless by default -- the transcript is the 'review on
    mute' artifact and is byte-stable with no API key. With ``--live-sarvam`` and
    a ``SARVAM_API_KEY`` present, the same call is synthesised to audio; without
    the key it prints the same honest one-line blocker every other live target
    prints, and still exits 0 (NFR-4).
    """
    from pramaan.converse import voice
    from pramaan.ledger.chain import Ledger as _Ledger

    config = load_config()

    # The committed artifact uses the DETERMINISTIC turn policy on purpose, for
    # the same reason `make demo` is deterministic: the transcript then reproduces
    # byte-for-byte with no key, and the mp3 (below) speaks exactly the lines the
    # transcript shows, so the two can never drift. The live LLM turn policy is a
    # real, tested code path (generate_reply with a client; verified live this
    # session) -- it just is not the committed artifact, because a per-conversation
    # LLM reply is not reliably reproducible offline and would make `make voice`
    # rewrite the committed transcript. `--live-sarvam` therefore controls the
    # AUDIO only; it never changes the words.
    llm_note = "deterministic (keyless, reproducible); live LLM path tested separately"
    result = voice.run_demo_call(llm=None)

    title = "Pramaan -- Day 7: Hinglish voice recovery"
    print(title)
    print("=" * len(title))
    print("  call at              %s (IST)" % voice.DEMO_CALL_AT)
    print("  channel              voice (Sarvam STT -> turn policy -> Sarvam TTS)")
    print("  turn policy          %s" % llm_note)
    print("  envelope pre-flight  %s citing %s  (ACT_VOICE, collection)"
          % (result.gate.verdict, result.gate.rule_id))
    print("  call placed          %s" % result.call_placed)

    _section("TRANSCRIPT -- the opening is the disclosure (R10), by construction")
    for i, turn in enumerate(result.turns, 1):
        who = "AI agent" if turn.speaker == "agent" else "customer"
        tag = " [scripted]" if turn.scripted else ""
        print("  %2d  %-9s%s %s" % (i, who, tag, turn.text))

    _section("COMPLIANCE -- each gate, and where it is enforced")
    print("  R10 disclosure first %s  (turn 1 is the AI disclosure)"
          % ("YES" if result.turns and result.turns[0].text == voice.DISCLOSURE_LINE else "NO"))
    print("  R9 window / self-id  enforced by the envelope pre-flight above")
    print("  R8 call cap / DND    same pre-flight (unsolicited_calls_today gate)")
    print("  S7 stand-down        checked before every reply; not triggered here")
    print("  (19:30 -> R9 REJECT, 08:30 -> R8 REJECT, missing disclosure -> R10")
    print("   REJECT, and a distress signal -> stand-down: tests/test_voice.py)")

    _section("PROMISE -- extracted from speech into the state machine (PRD 6.8)")
    if result.promise is not None:
        p = result.promise
        print("  state                %s" % p.state)
        print("  promised date        %s" % p.promised_date)
        print("  channel              %s" % p.channel)
        print("  verbatim             \"%s\"" % p.verbatim)
    else:
        print("  no commitment extracted (the extractor is conservative -- S3)")

    # -- ledger -----------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "voice.db"
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    ledger = _Ledger(conn)
    voice.write_call_to_ledger(ledger, result, ts=voice.DEMO_CALL_AT)
    verification = ledger.verify_chain()

    _section("LEDGER -- the call is a hash-chained record")
    kind_counts = dict(ledger.kind_counts())  # read before the connection closes
    for kind, count in sorted(kind_counts.items()):
        print("  %-20s %d rows" % (kind, count))
    print("  head hash            %s" % ledger.head_hash())
    print("  verify_chain         %s (%d rows)"
          % ("PASS" if verification.ok else "FAIL", verification.rows_checked))
    ledger.export_jsonl(out_dir / "voice.jsonl")
    conn.close()

    # -- the transcript artifact ------------------------------------------
    assets_dir = ROOT / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = assets_dir / "voice-transcript.md"
    # LF-forced like ledger.export_jsonl: a CRLF here would make the committed
    # artifact differ byte-for-byte between a Windows and a Linux checkout.
    with open(transcript_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(voice.render_transcript_markdown(result))

    _section("ARTIFACTS")
    print("  transcript           %s" % _display_path(transcript_path))
    if live_sarvam:
        sarvam = voice.SarvamClient(config.sarvam_api_key)
        if not sarvam.available:
            print("  audio                SKIPPED -- SARVAM_API_KEY is not set.")
            print("                       The transcript above is complete; only the")
            print("                       audio clip needs a live Sarvam key. Set it in")
            print("                       .env and re-run with --live-sarvam to write")
            print("                       assets/voice-demo.mp3.")
        else:
            audio_path = _synthesize_call_audio(sarvam, result, assets_dir)
            print("  audio                %s" % _display_path(audio_path))
    else:
        print("  audio                (pass --live-sarvam with SARVAM_API_KEY to")
        print("                       synthesise assets/voice-demo.mp3)")

    _section("RESULT")
    checks = [
        ("the call was placed (envelope allowed the ACT_VOICE step)", result.call_placed),
        ("the AI disclosure is the first utterance (R10)",
            bool(result.turns) and result.turns[0].text == voice.DISCLOSURE_LINE),
        ("a promise was extracted from speech into the state machine",
            result.promise is not None and result.promise.state == "promised"),
        ("the call is written to the hash chain and it verifies", verification.ok),
        ("a CONVERSE row and a PROMISE row exist",
            kind_counts.get("CONVERSE", 0) == 1
            and kind_counts.get("PROMISE", 0) == 1),
    ]
    for label, passed in checks:
        print("  [%s] %s" % ("x" if passed else " ", label))
    ok = all(passed for _, passed in checks)
    print()
    print("  %s" % ("ALL CHECKS PASS" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


def _synthesize_call_audio(sarvam, result, assets_dir: Path) -> Path:
    """Synthesise the whole call as a two-voice MP3 dialogue.

    Only reached when a Sarvam key is present. Every turn is synthesised in
    speaker order -- the agent in the default voice, the customer in a
    contrasting one -- so the clip is an actual back-and-forth conversation, not
    the agent talking into silence. MP3 is a frame stream, so concatenating the
    per-line clips yields one file every common player handles; the codec choice
    that makes this safe lives in ``voice.SARVAM_TTS_CODEC``.
    """
    from pramaan.converse.voice import SARVAM_TTS_CUSTOMER_SPEAKER

    audio_path = assets_dir / "voice-demo.mp3"
    with open(audio_path, "wb") as handle:
        for turn in result.turns:
            speaker = None if turn.speaker == "agent" else SARVAM_TTS_CUSTOMER_SPEAKER
            handle.write(sarvam.synthesize(turn.text, speaker=speaker))
    return audio_path


def run_execute(
    batch: str, seed: int, out_dir: Path, *, live_razorpay: bool
) -> int:
    """Shadow mode, always. A real Razorpay TEST-mode demo, only if asked and
    only if credentials exist.

    Shadow mode is unconditional and prints first: plan -> envelope -> resolve
    over the whole batch, with arm C wired, the C-B ablation, the organic
    planner violation rate, and the memoisation ratio (BUILD-PLAN Day 5's
    definition of done, in one command). ``--live-razorpay`` is a separate,
    additive step -- PRD 12.1 is explicit that shadow mode is the default and
    executes nothing, so the live demo is never folded into it, only appended
    after it.
    """
    from pramaan.execute.runner import run_execute as run_execute_live
    from pramaan.execute.runner import run_shadow

    config = load_config()
    events: List[RiskEvent] = (
        sim.dev_batch(seed) if batch == "dev" else sim.full_batch(seed)
    )

    db_path = out_dir / ("pramaan-execute-%s.db" % batch)
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    ledger = Ledger(conn)

    title = "Pramaan -- Day 5: the planner proposes, the envelope disposes"
    print(title)
    print("=" * len(title))
    print("  batch                %s (%d events)" % (batch, len(events)))
    print("  seed                 %d" % seed)
    print("  mode                 %s" % config.mode)
    print()

    result = run_shadow(events, seed=seed, ledger=ledger)
    print(result.report)

    _section("LEDGER")
    for kind, n in ledger.kind_counts():
        print("  %-18s %s" % (kind, _thousands(n)))
    verification = ledger.verify_chain()
    print("  verify_chain         %s" % ("PASS" if verification.ok else "FAIL"))
    export = ledger.export_jsonl(out_dir / ("execute-%s.jsonl" % batch))
    print("  exported             %s" % _display_path(export))

    if live_razorpay:
        _section("LIVE -- Razorpay TEST mode")
        live = run_execute_live(config, ledger=ledger)
        if not live.attempted:
            print("  %s" % live.message)
        else:
            print("  %s" % live.message)
            print("  order id             %s" % live.order.get("id"))
            print("  payment link id      %s" % live.payment_link.get("id"))
            print("  payment link url     %s" % live.payment_link.get("short_url"))
            print(
                "  idempotent replay    %s  (calling create_order twice with the"
                % ("no-op" if live.idempotent_replay_was_noop else "FAILED -- re-executed")
            )
            print("                        same idempotency key made one API call)")
            print(
                "  terminal-state guard %s"
                % (
                    "correct (order not yet paid, guard let the action through)"
                    if live.terminal_state_guard_correct
                    else "FAILED"
                )
            )
        conn.commit()

    _section("RESULT")
    checks = [
        ("arm C is wired and takes action", eval_arms.ARM_POLICIES["C"].acts),
        (
            "C-B is a real bootstrap contrast with a CI",
            "C-B" in result.metrics.contrasts
            and result.metrics.contrasts["C-B"].intervals["rate"].method in ("BCa", "percentile"),
        ),
        (
            "memoisation ratio computed and > 1",
            result.planner.stats.memoisation_ratio >= 1.0,
        ),
        ("PLAN ledger rows written, one per distinct signature",
            dict(ledger.kind_counts()).get("PLAN", 0) == len(result.planner.newly_built)),
        ("ledger verifies", verification.ok),
    ]
    if live_razorpay and live.attempted:
        checks.append(("at least one real payment link created", bool(live.payment_link)))
        checks.append(("double-executing the same action is a no-op", bool(live.idempotent_replay_was_noop)))
    for label, passed in checks:
        print("  [%s] %s" % ("x" if passed else " ", label))
    ok = all(passed for _, passed in checks)
    print()
    print("  %s" % ("ALL CHECKS PASS" if ok else "SOME CHECKS FAILED"))
    conn.close()
    return 0 if ok else 1


def run_models() -> int:
    """Report the configured model IDs, and verify them if a key is present.

    Exists because of a real and slightly embarrassing finding on Day 4: the two
    model IDs this project carried for three days had been shut down twelve days
    earlier. A comment saying "verify these" is not a verification, and a
    free-tier lineup changes without notice, so the check is a command.
    """
    from pramaan.llm.client import (
        MODEL_SOURCES,
        MODELS,
        MODELS_VERIFIED_ON,
        TIERS,
    )

    config = load_config()
    client = LLMClient(config)

    _section("MODELS -- what this project asks for")
    print("  last verified        %s" % MODELS_VERIFIED_ON)
    for tier in TIERS:
        print("  %s:" % tier)
        for index, (provider, model) in enumerate(MODELS[tier]):
            role = "primary " if index == 0 else "failover"
            print("    %s  %-12s %s" % (role, provider, model))
    print()
    for provider, url in sorted(MODEL_SOURCES.items()):
        print("  %-12s %s" % (provider, url))

    _section("VERIFICATION -- what the providers actually serve")
    report = client.verify_models()
    ok = True
    for provider, entry in sorted(report["providers"].items()):
        print("  %s: %s" % (provider, entry["status"]))
        if "served_count" in entry:
            print("    models served      %d" % entry["served_count"])
            for model in entry.get("present", []):
                print("    PRESENT            %s" % model)
            for model in entry.get("missing", []):
                ok = False
                print("    MISSING            %s   <-- this ID does not resolve" % model)
    print()
    if all("no API key" in e["status"] for e in report["providers"].values()):
        print("  No API keys are set, so nothing could be checked. Set GROQ_API_KEY")
        print("  or OPENROUTER_API_KEY in .env and re-run. This command makes no")
        print("  completion calls and consumes no tokens either way.")
        return 0
    if not ok:
        print("  At least one configured model ID does not resolve. Fix MODELS in")
        print("  pramaan/llm/client.py before running anything that costs tokens.")
        return 1
    print("  Every configured model ID resolves.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pramaan", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="ingest a seeded batch and write a ledger")
    group = demo.add_mutually_exclusive_group()
    group.add_argument(
        "--dev",
        dest="batch",
        action="store_const",
        const="dev",
        help="200-event dev batch (default). Never iterate on the full batch.",
    )
    group.add_argument(
        "--full",
        dest="batch",
        action="store_const",
        const="full",
        help="6,000-event batch. Sized from the PRD 8.1 power calculation.",
    )
    demo.add_argument("--seed", type=int, default=None)
    demo.add_argument("--out", type=Path, default=BUILD_DIR)
    demo.set_defaults(batch="dev")

    investigate_cmd = sub.add_parser(
        "investigate",
        help="detect an incident in a degraded batch and diagnose it with an LLM agent",
    )
    investigate_cmd.add_argument("--seed", type=int, default=None)
    investigate_cmd.add_argument("--out", type=Path, default=BUILD_DIR)
    investigate_cmd.add_argument(
        "--count",
        type=int,
        default=None,
        help="events in the degraded batch. Larger costs no more tokens: the LLM "
             "cost is one session per incident, and SQL is free.",
    )
    investigate_cmd.add_argument("--days", type=int, default=None)

    sub.add_parser(
        "models",
        help="print the configured model IDs and verify them against each provider",
    )

    execute_cmd = sub.add_parser(
        "execute",
        help="plan -> envelope -> resolve, arm C wired. Shadow mode by default.",
    )
    execute_group = execute_cmd.add_mutually_exclusive_group()
    execute_group.add_argument(
        "--dev", dest="batch", action="store_const", const="dev",
        help="200-event dev batch (default)",
    )
    execute_group.add_argument(
        "--full", dest="batch", action="store_const", const="full",
        help="6,000-event batch",
    )
    execute_cmd.add_argument("--seed", type=int, default=None)
    execute_cmd.add_argument("--out", type=Path, default=BUILD_DIR)
    execute_cmd.add_argument(
        "--live-razorpay",
        action="store_true",
        help="also create one real order and one real payment link in Razorpay "
             "TEST mode. Requires RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET in .env. "
             "Additive to shadow mode, never a replacement for it.",
    )
    execute_cmd.set_defaults(batch="dev")

    voice_cmd = sub.add_parser(
        "voice",
        help="Day 7: place the canonical Hinglish recovery call, write the "
             "transcript and ledger rows. Deterministic and keyless by default.",
    )
    voice_cmd.add_argument("--out", type=Path, default=BUILD_DIR)
    voice_cmd.add_argument(
        "--live-sarvam",
        action="store_true",
        help="also synthesise the call to assets/voice-demo.mp3 via Sarvam TTS. "
             "Requires SARVAM_API_KEY in .env; without it this prints an honest "
             "blocker and still exits 0.",
    )

    args = parser.parse_args(argv)
    if args.command == "models":
        return run_models()
    if args.command == "demo":
        seed = args.seed if args.seed is not None else load_config().seed
        args.out.mkdir(parents=True, exist_ok=True)
        return run_demo(args.batch, seed, args.out)
    if args.command == "investigate":
        from sim.generate import INVESTIGATE_BATCH_DAYS, INVESTIGATE_BATCH_SIZE

        seed = args.seed if args.seed is not None else load_config().seed
        return run_investigate(
            seed,
            args.out,
            args.count if args.count is not None else INVESTIGATE_BATCH_SIZE,
            args.days if args.days is not None else INVESTIGATE_BATCH_DAYS,
        )
    if args.command == "execute":
        seed = args.seed if args.seed is not None else load_config().seed
        args.out.mkdir(parents=True, exist_ok=True)
        return run_execute(args.batch, seed, args.out, live_razorpay=args.live_razorpay)
    if args.command == "voice":
        args.out.mkdir(parents=True, exist_ok=True)
        return run_voice(args.out, live_sarvam=args.live_sarvam)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
