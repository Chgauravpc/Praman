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

from pramaan import canonical
from pramaan.config import BUILD_DIR, ROOT, SIM_EPOCH, load_config
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


def _bar(share: float, width: int = 24) -> str:
    filled = int(round(share * width))
    return "#" * filled + "." * (width - filled)


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


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

    print("Pramaan -- revenue recovery, Day 1 spine")
    print("=" * 39)
    print("  batch                %s (%d events)" % (batch, len(events)))
    print("  seed                 %d" % seed)
    print("  sim epoch            %s" % SIM_EPOCH)
    print("  mode                 %s" % config.mode)
    print("  llm                  offline (Day 1 makes zero LLM calls, by design)")

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

    # -- ledger ------------------------------------------------------------
    _section("LEDGER -- hash chain")
    for kind, count in ledger.kind_counts():
        print("  %-20s %d rows" % (kind, count))
    verification = ledger.verify_chain()
    print("  head hash            %s" % ledger.head_hash())
    print(
        "  verify_chain         %s  (%d rows checked)"
        % ("PASS" if verification.ok else "FAIL", verification.rows_checked)
    )
    tamper = _tamper_probe(conn)
    print("  tamper probe         %s" % tamper)

    ledger.export_jsonl(ledger_path)
    print("  exported             %s" % ledger_path.relative_to(ROOT).as_posix())

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
        run_id="day1-%s-seed%d" % (batch, seed),
        seed=seed,
        batch=batch,
        event_count=len(events),
        sim_epoch=SIM_EPOCH,
        notes="day 1 spine: sense + ledger, no LLM",
    )
    conn.commit()

    _section("RESULT")
    checks = [
        ("ingest is idempotent (I1)", idempotent),
        ("hash chain verifies (I7)", verification.ok),
        ("tamper is detected (I7)", tamper.startswith("detected")),
        ("declared weights inside the published ranges", _declared_weights_ok()),
        (
            "observed draw consistent with declared weights (3 s.e.)",
            _observed_matches_declared(shares, len(events)),
        ),
        ("every amount band populated", all(histogram[b] > 0 for b in canonical.AMOUNT_BANDS)),
        ("arms within 5% of equal thirds", _arms_balanced(store)),
    ]
    for label, passed in checks:
        print("  [%s] %s" % ("x" if passed else " ", label))
    ok = all(passed for _, passed in checks)
    print()
    print("  %s" % ("ALL CHECKS PASS" if ok else "SOME CHECKS FAILED"))
    conn.close()
    return 0 if ok else 1


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
    target = copy.execute(
        "SELECT seq FROM ledger ORDER BY seq LIMIT 1 OFFSET ?",
        (max(0, shadow.count() // 2),),
    ).fetchone()
    if target is None:
        copy.close()
        return "inconclusive: empty ledger"
    seq = int(target["seq"])
    # Change one integer in one payload -- the subtlest useful edit: someone
    # quietly inflating a recovered amount.
    copy.execute(
        "UPDATE ledger SET payload = replace(payload, '\"amount_at_risk_paise\":', "
        "'\"amount_at_risk_paise\":1') WHERE seq = ?",
        (seq,),
    )
    result = shadow.verify_chain()
    copy.close()
    if result.ok:
        return "FAILED -- a mutated row went undetected"
    return "detected at row %d (%s)" % (seq, result.error.split(":")[0])


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

    args = parser.parse_args(argv)
    if args.command == "demo":
        seed = args.seed if args.seed is not None else load_config().seed
        args.out.mkdir(parents=True, exist_ok=True)
        return run_demo(args.batch, seed, args.out)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
