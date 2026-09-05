"""The dashboard snapshot. One JSON file, aggregated from artifacts that exist.

``build/dashboard.json`` is the only thing the static dashboard reads. This
module writes it, offline, from a read-only pass over a committed ledger plus
the ``BatchMetrics`` the execute run already serialised. It is presentation, and
presentation is the layer most likely to quietly become a second, informal
implementation of the pipeline -- so four things are enforced here rather than
left to reviewer discipline:

**One producer per number.** ``eval/metrics.py`` opens by warning that if two
layers can each derive the headline, they can disagree, "and then there are two
truths and no audit trail". So this module never recomputes a contrast. It
serialises the ``BatchMetrics`` object ``run_shadow`` already built (§
``metrics_to_dict``), and everything else it reports is a *count of rows the
ledger recorded*. Counting what was written is reading. Re-deriving it is not,
and is not done here.

**Read-only, structurally.** The SQLite connection is opened through a
``file:...?mode=ro`` URI, and this module deliberately does **not** construct a
``Ledger``: ``Ledger.__init__`` runs ``executescript(SCHEMA)`` and commits,
which is a write. A read-only guarantee that holds only because nobody happened
to call a writer is not a guarantee.

**Only joins the data supports.** ``event_id`` is the one key shared across
``DETECT``/``GATE``/``OUTCOME``/``EXCEPTION``, and it is the only join performed.
``PLAN`` rows carry a memoised ``signature`` and no event or counterparty, and
``ACTION``/``DIAGNOSIS``/``RECEIPT_AUDIT`` rows carry neither -- so no
per-customer thread is assembled, because assembling one would mean inventing
the edges. DASHBOARD-PLAN.md §1 is the long version.

**Deterministic bytes.** Output goes through ``canonical_json``: sorted keys,
no wall-clock, no absolute paths, LF endings. Same discipline as I8, and
``tests/test_dashboard_snapshot.py`` runs it twice and diffs.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pramaan.canonical import canonical_json
from pramaan.config import BUILD_DIR, ROOT
from pramaan.eval.metrics import ArmSummary, BatchMetrics, Contrast
from pramaan.ledger.chain import DECISIONS

#: Bumped when the shape below changes incompatibly. The frontend checks it and
#: refuses to render on a mismatch: a dashboard that silently draws blanks
#: against a stale file is worse than one that says it is stale.
SCHEMA_VERSION = 1

#: The all-kinds coverage chain (``make golden-kinds``). It is the only
#: committed artifact containing every ``LEDGER_KINDS`` value, which makes it the
#: only honest source for the "why did the agent do this?" panels.
DEFAULT_GOLDEN = ROOT / "tests" / "golden" / "all_kinds.jsonl"


# --------------------------------------------------------------------------
# Serialising BatchMetrics -- the headline, never recomputed
# --------------------------------------------------------------------------


def _finite(value: float) -> Optional[float]:
    """``None`` for inf/NaN, which are not JSON and which this system can produce.

    ``canonical_json`` sets ``allow_nan=False``, so an infinity reaching it is a
    crash rather than a bad number. It is reachable: ``gross_over_claim`` returns
    ``inf`` on an underpowered batch where the incremental point estimate lands
    at or below zero, and ``relative_widths`` does the same on a zero baseline.
    Emitting ``null`` keeps the field present and unmistakably absent, rather
    than dropping the key and letting the frontend read a missing value as zero.
    """
    number = float(value)
    return number if math.isfinite(number) else None


def _interval_to_dict(interval: Any) -> Dict[str, Any]:
    """One ``bootstrap.Interval``, with the method it was actually produced by.

    ``fallback_reason`` is carried through deliberately. ``bootstrap.Interval``
    records it because "a percentile interval labelled BCa would be a lie about
    the method, and the method is the part being trusted" -- dropping it here
    would launder exactly that lie into the UI.
    """
    return {
        "point": _finite(interval.point),
        "low": _finite(interval.low),
        "high": _finite(interval.high),
        "method": interval.method,
        "resamples": int(interval.resamples),
        "confidence": _finite(interval.confidence),
        "excludes_zero": bool(interval.excludes_zero),
        "fallback_reason": interval.fallback_reason,
    }


def _contrast_to_dict(contrast: Contrast) -> Dict[str, Any]:
    """One arm minus another.

    ``actionable_only`` is not optional here. ``Contrast`` carries that flag
    "so that a caller cannot print a subgroup figure as though it were the
    headline", and a dashboard tile is a caller. The full-batch C-A lift and the
    actioned-subset lift are different quantities over different populations;
    the frontend renders the flag as visible text on the tile.
    """
    return {
        "treatment": contrast.treatment,
        "control": contrast.control,
        "n_treatment": int(contrast.n_treatment),
        "n_control": int(contrast.n_control),
        "actionable_only": bool(contrast.actionable_only),
        "baseline_rate": _finite(contrast.baseline_rate),
        "baseline_money_paise": _finite(contrast.baseline_money_paise),
        "rate_normal": _interval_to_dict(contrast.rate_normal),
        "intervals": {
            name: _interval_to_dict(interval)
            for name, interval in contrast.intervals.items()
        },
    }


def _arm_summary_to_dict(summary: ArmSummary) -> Dict[str, Any]:
    """One arm's observed behaviour. Descriptive; every field is a count or paise."""
    return {
        "arm": summary.arm,
        "n": int(summary.n),
        "at_risk_paise": int(summary.at_risk_paise),
        "recovered": int(summary.recovered),
        "recovered_paise": int(summary.recovered_paise),
        "actions_taken": int(summary.actions_taken),
        "contacts": int(summary.contacts),
        "false_interventions": int(summary.false_interventions),
        "cost_paise": int(summary.cost_paise),
        "externalities": int(summary.externalities),
        "externality_paise": int(summary.externality_paise),
        "exceptions": int(summary.exceptions),
        "contact_attributed_paise": int(summary.contact_attributed_paise),
        "action_counts": dict(summary.action_counts),
        "cost_by_action": dict(summary.cost_by_action),
        "rate": _finite(summary.rate),
        "value_share": _finite(summary.value_share),
        # The aggregate counterfactual: how many of this arm's recoveries the
        # latent world says would have happened with no action at all.
        #
        # An earlier version of this function withheld it, reasoning that
        # ADR-010 keeps latent truth out of the ledger so the dashboard should
        # match. That conflated two different things. What ADR-010 protects
        # against is an *agent* reading the answer key, and per-event truth
        # leaking into an artifact a reviewer can download -- which is why
        # neither CSV carries it and a test asserts so. One aggregate per arm is
        # neither: it is the same figure EVALUATION.md publishes, and it is the
        # most honest number this project has. Arm C recovered 710 and 515 of
        # those would have recovered anyway; hiding that would flatter the
        # result by exactly the amount the holdout exists to measure.
        "would_recover_unaided": int(summary.would_recover_unaided),
    }


def metrics_to_dict(metrics: BatchMetrics) -> Dict[str, Any]:
    """Serialise the metrics object the run already built. Computes nothing.

    Called by ``cli.run_execute`` immediately after it prints ``result.report``,
    so the printed number and the rendered number are the same object serialised
    twice. ``ShadowResult`` promises that "a caller inspecting ``metrics``
    directly sees exactly the numbers the report quotes"; this is that caller.
    """
    return {
        "n_events": int(metrics.n_events),
        "window_seconds": int(metrics.window_seconds),
        "resamples": int(metrics.resamples),
        "unwired_arms": list(metrics.unwired_arms),
        "gross_over_claim": _finite(metrics.gross_over_claim),
        "summaries": {
            arm: _arm_summary_to_dict(summary)
            for arm, summary in metrics.summaries.items()
        },
        "contrasts": {
            name: _contrast_to_dict(contrast)
            for name, contrast in metrics.contrasts.items()
        },
    }


def write_metrics(metrics: BatchMetrics, path: Path) -> Path:
    """Write ``metrics_to_dict`` to ``path`` as canonical JSON.

    Byte-stable and LF-terminated for the same reason ``Ledger.export_jsonl`` is:
    on Windows the platform default would be CRLF, and a golden comparison would
    fail for a reason that has nothing to do with the metrics.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(metrics_to_dict(metrics)))
        handle.write("\n")
    return path


# --------------------------------------------------------------------------
# Reading the ledger -- read-only, one pass, one join
# --------------------------------------------------------------------------


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a ledger DB read-only, and prove it by asking SQLite rather than us.

    ``mode=ro`` makes a stray write an ``OperationalError`` at the driver rather
    than a silent mutation of an artifact whose whole value is being unmutated.
    ``uri=True`` is required for the mode parameter to be honoured at all -- a
    plain path with a query string appended would be read as a filename.
    """
    if not path.exists():
        raise FileNotFoundError(
            "no ledger at %s -- run `make demo-full` first" % path.name
        )
    conn = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _payload(row: sqlite3.Row) -> Dict[str, Any]:
    """The stored payload string, parsed. Rows are read, never re-hashed."""
    return json.loads(row["payload"])


def _bump(counter: Dict[str, int], key: Optional[str]) -> None:
    """Count one, tolerating a NULL column as the literal string ``"none"``.

    ``OUTCOME.action`` already uses ``"none"`` for "the arm did nothing", so a
    missing value folds into the bucket that already means that rather than
    inventing a second name for the same fact.
    """
    name = key if key is not None else "none"
    counter[name] = counter.get(name, 0) + 1


def read_ledger(conn: sqlite3.Connection) -> Dict[str, Any]:
    """One ordered pass over the chain, grouped every way the dashboard needs.

    The single join is ``OUTCOME.event_id -> DETECT.event_id``, used only to
    attach ``source_type``; ``OUTCOME`` already carries its own money fields, so
    nothing else needs looking up. Rows of kinds this dashboard does not render
    (``PLAN``, ``CANARY``, ...) are counted in ``kind_counts`` and otherwise left
    alone -- their presence is a fact worth showing, their contents are not
    joinable to anything here.
    """
    source_type_of: Dict[str, str] = {}
    kind_counts: Dict[str, int] = {}
    # Seeded with every decision the enum allows, so a decision that never fired
    # renders as an explicit `0` instead of an absent key. README states "0
    # REJECT" as a result -- a missing key would let the UI draw nothing there,
    # which reads as "not measured" rather than "measured, and it was zero".
    gate_decisions: Dict[str, int] = {decision: 0 for decision in DECISIONS}
    gate_rules: Dict[str, int] = {}
    exception_reasons: Dict[str, int] = {}
    exception_actions: Dict[str, int] = {}
    arms: Dict[str, Dict[str, Any]] = {}
    categories: Dict[str, Dict[str, Any]] = {}
    head_hash = None
    rows_seen = 0

    for row in conn.execute("SELECT * FROM ledger ORDER BY seq"):
        rows_seen += 1
        head_hash = row["row_hash"]
        kind = row["kind"]
        _bump(kind_counts, kind)

        if kind == "DETECT":
            payload = _payload(row)
            source_type_of[payload["event_id"]] = payload["source_type"]

        elif kind == "GATE":
            _bump(gate_decisions, row["decision"])
            _bump(gate_rules, row["rule_fired"])

        elif kind == "EXCEPTION":
            payload = _payload(row)
            _bump(exception_reasons, payload.get("reason"))
            _bump(exception_actions, payload.get("proposed_action"))

        elif kind == "OUTCOME":
            payload = _payload(row)
            arm = row["arm"]
            if arm is None:
                raise ValueError(
                    "OUTCOME row seq=%s has no arm. Every outcome belongs to "
                    "exactly one arm; an unlabelled one cannot be aggregated "
                    "without silently pooling arms, which is the one thing this "
                    "dashboard must not do." % row["seq"]
                )
            bucket = arms.setdefault(arm, _empty_arm(arm))
            _accumulate(bucket, payload)

            source_type = source_type_of.get(payload["event_id"])
            if source_type is not None:
                category = categories.setdefault(
                    source_type, {"source_type": source_type, "arms": {}}
                )
                per_arm = category["arms"].setdefault(arm, _empty_arm(arm))
                _accumulate(per_arm, payload)

    # Rates are finalised here rather than in the browser. The frontend formats
    # and lays out; it does not divide. Keeping every derived number on this
    # side means there is exactly one place a rate can be wrong, and it is a
    # place with tests.
    for bucket in arms.values():
        _finalise(bucket)
    for category in categories.values():
        for bucket in category["arms"].values():
            _finalise(bucket)

    return {
        "head_hash": head_hash,
        "rows": rows_seen,
        "kind_counts": kind_counts,
        "gate_decisions": gate_decisions,
        "gate_rules": gate_rules,
        "exception_reasons": exception_reasons,
        "exception_actions": exception_actions,
        "arms": arms,
        "by_category": [categories[name] for name in sorted(categories)],
    }


# --------------------------------------------------------------------------
# Row-level export -- one line per resolved event
# --------------------------------------------------------------------------
#
# The aggregates above answer "what happened"; this answers "show me". It is a
# separate artifact rather than another key in ``dashboard.json`` for a measured
# reason: 6,000 joined rows serialise to ~2.6 MB of JSON, which would be 240x
# the rest of the snapshot and would be fetched on every page load to render a
# table most viewers never open. As CSV the same rows are ~700 KB, open in a
# spreadsheet with no tooling at all, and the dashboard fetches them only when
# the data section is expanded. One file, two consumers, no duplicated truth.

#: Fixed column order. Append-only: a reader with a saved spreadsheet or a
#: script that indexes by position should not break because a column moved.
ACTION_CSV_COLUMNS: Tuple[str, ...] = (
    "seq",
    "ts",
    "arm",
    "event_id",
    "counterparty_id",
    "source_type",
    "reason_class",
    "segment",
    "amount_band",
    "gate_decision",
    "gate_rule",
    "action",
    "channel",
    "delay_seconds",
    "amount_at_risk_paise",
    "amount_recovered_paise",
    "recovered",
    "contacted",
    "cause",
    "exception_reason",
)


def read_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """One row per ``OUTCOME``, joined to its ``DETECT``, ``GATE`` and ``EXCEPTION``.

    ``event_id`` is the only key the ledger shares across kinds, and this is the
    join it supports. ``OUTCOME`` is the spine because it is the row that says
    what was actually done; a ``DETECT`` with no outcome would be an event the
    run never resolved, and there are none (asserted in the tests).

    **No latent field is read.** ``would_recover_unaided`` and the rest of the
    answer key live in their own table (ADR-010) and this function never opens
    it. A CSV a reviewer can download is exactly the wrong place for the
    counterfactual: it would let anyone compute the "true" effect per event and
    quietly turn an audit artifact into an answer sheet.
    """
    detect: Dict[str, Dict[str, Any]] = {}
    gate: Dict[str, Any] = {}
    exception: Dict[str, Dict[str, Any]] = {}
    outcomes: List[Tuple[sqlite3.Row, Dict[str, Any]]] = []

    for row in conn.execute("SELECT * FROM ledger ORDER BY seq"):
        kind = row["kind"]
        if kind == "DETECT":
            payload = _payload(row)
            detect[payload["event_id"]] = payload
        elif kind == "GATE":
            payload = _payload(row)
            gate[payload["event_id"]] = row
        elif kind == "EXCEPTION":
            payload = _payload(row)
            exception[payload["event_id"]] = payload
        elif kind == "OUTCOME":
            outcomes.append((row, _payload(row)))

    rows: List[Dict[str, Any]] = []
    for row, payload in outcomes:
        event_id = payload["event_id"]
        d = detect.get(event_id, {})
        g = gate.get(event_id)
        x = exception.get(event_id, {})
        rows.append(
            {
                "seq": int(row["seq"]),
                "ts": row["ts"],
                "arm": row["arm"],
                "event_id": event_id,
                "counterparty_id": d.get("counterparty_id"),
                "source_type": d.get("source_type"),
                "reason_class": d.get("reason_class"),
                "segment": d.get("segment"),
                "amount_band": d.get("amount_band"),
                "gate_decision": g["decision"] if g is not None else None,
                "gate_rule": g["rule_fired"] if g is not None else None,
                "action": payload.get("action"),
                "channel": payload.get("channel"),
                "delay_seconds": payload.get("delay_seconds"),
                "amount_at_risk_paise": int(payload.get("amount_at_risk_paise", 0)),
                "amount_recovered_paise": int(payload.get("amount_recovered_paise", 0)),
                "recovered": bool(payload.get("recovered")),
                "contacted": bool(payload.get("contacted")),
                "cause": payload.get("cause"),
                "exception_reason": x.get("reason"),
            }
        )
    return rows


#: The full ``DETECT`` payload, in a fixed order. Wider than
#: ``ACTION_CSV_COLUMNS`` on purpose: this is the event *as it arrived*, before
#: anything judged or acted on it, so it keeps the fields the outcome-spine
#: export drops -- the raw cause signal, the channel the hour allows, the decay
#: profile, and the action menu the event itself declares.
DETECTED_CSV_COLUMNS: Tuple[str, ...] = (
    "seq",
    "ts",
    "arm",
    "event_id",
    "counterparty_id",
    "source_type",
    "reason_class",
    "cause_signal",
    "segment",
    "amount_band",
    "amount_at_risk_paise",
    "legal_context",
    "channel_eligibility",
    "hour_bucket",
    "decay_profile",
    "available_actions",
    "external_ref",
    "signature",
)

#: Fields whose distribution the dashboard draws. Each one is a dimension the
#: envelope or the planner actually keys on, so the shape of the input explains
#: the shape of everything downstream.
DETECTED_FACETS: Tuple[str, ...] = (
    "source_type",
    "reason_class",
    "segment",
    "legal_context",
    "channel_eligibility",
    "hour_bucket",
    "decay_profile",
)


def read_detected(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The batch as it arrived: one row per ``DETECT``, plus its distribution.

    Spined on ``DETECT`` rather than ``OUTCOME``, which is the whole point. The
    row-level export joins outcomes back to detections, so an event that was
    never resolved cannot appear in it -- on the committed batch every event
    resolves, but a view that *structurally* cannot show an unresolved event is
    the wrong place to answer "what went in". This one counts detections and
    reports the reconciliation against outcomes, so a gap would be visible
    rather than invisible.
    """
    rows: List[Dict[str, Any]] = []
    facets: Dict[str, Dict[str, int]] = {name: {} for name in DETECTED_FACETS}
    bands: Dict[str, int] = {}
    at_risk = 0
    outcome_ids = set()

    for row in conn.execute("SELECT * FROM ledger ORDER BY seq"):
        kind = row["kind"]
        if kind == "OUTCOME":
            outcome_ids.add(_payload(row)["event_id"])
            continue
        if kind != "DETECT":
            continue
        payload = _payload(row)
        rows.append(
            {
                "seq": int(row["seq"]),
                "ts": row["ts"],
                "arm": row["arm"],
                "event_id": payload.get("event_id"),
                "counterparty_id": payload.get("counterparty_id"),
                "source_type": payload.get("source_type"),
                "reason_class": payload.get("reason_class"),
                "cause_signal": payload.get("cause_signal"),
                "segment": payload.get("segment"),
                "amount_band": payload.get("amount_band"),
                "amount_at_risk_paise": int(payload.get("amount_at_risk_paise", 0)),
                "legal_context": payload.get("legal_context"),
                "channel_eligibility": payload.get("channel_eligibility"),
                "hour_bucket": payload.get("hour_bucket"),
                "decay_profile": payload.get("decay_profile"),
                # A list in the payload; joined so one CSV cell holds it.
                "available_actions": " ".join(payload.get("available_actions") or ()),
                "external_ref": payload.get("external_ref"),
                "signature": payload.get("signature"),
            }
        )
        at_risk += int(payload.get("amount_at_risk_paise", 0))
        _bump(bands, str(payload.get("amount_band")))
        for name in DETECTED_FACETS:
            _bump(facets[name], payload.get(name))

    detected_ids = {r["event_id"] for r in rows}
    return {
        "rows": rows,
        "count": len(rows),
        "at_risk_paise": at_risk,
        "facets": facets,
        "amount_bands": bands,
        # The reconciliation. Equal counts and an empty unresolved set is the
        # claim; printing it is what makes it checkable.
        "resolved": len(detected_ids & outcome_ids),
        "unresolved": sorted(detected_ids - outcome_ids)[:20],
        "unresolved_count": len(detected_ids - outcome_ids),
    }


def write_detected_csv(rows: Sequence[Dict[str, Any]], path: Path) -> Path:
    """The input export. Same quoting and determinism rules as the action CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(DETECTED_CSV_COLUMNS)
        for row in rows:
            writer.writerow([_csv_cell(row.get(c)) for c in DETECTED_CSV_COLUMNS])
    return path


def _csv_cell(value: Any) -> str:
    """Render one cell as text. Quoting is the writer's job, not this function's.

    Booleans become ``true``/``false`` rather than Python's ``True``/``False``
    so the file reads the same way in a spreadsheet, in ``jq`` and in the
    dashboard's parser. ``None`` becomes empty, which is what a spreadsheet
    shows for "this event had no exception" -- the honest rendering of absent,
    and distinguishable from the string "none" that ``OUTCOME.action`` uses to
    mean "the arm deliberately did nothing".
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if "\r" in text:
        # A bare CR would survive quoting but break a line-oriented reader, and
        # nothing in this ledger should contain one.
        raise ValueError("CSV cell %r contains a carriage return" % text)
    return text


def write_actions_csv(rows: Sequence[Dict[str, Any]], path: Path) -> Path:
    """Write the row-level export. Deterministic bytes, LF endings, RFC 4180.

    Real quoting rather than a no-punctuation rule, because one column earns it:
    ``exception_reason`` carries the envelope's own refusal prose ("envelope
    refused: R1 (no pre-debit notification has been sent. Every mandate debit --
    including a retry -- needs one at least 24h in advance, ...)"), which is the
    single most useful cell in the file and is full of commas. Mangling it to
    keep the parser trivial would be optimising the wrong side; the dashboard
    carries a quote-aware parser to match.

    Ordered by ``seq``, the chain's own order, so two runs over the same ledger
    produce identical files and any diff means the ledger changed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(ACTION_CSV_COLUMNS)
        for row in rows:
            writer.writerow([_csv_cell(row.get(c)) for c in ACTION_CSV_COLUMNS])
    return path


def _empty_arm(arm: str) -> Dict[str, Any]:
    return {
        "arm": arm,
        "n": 0,
        "recovered": 0,
        "contacted": 0,
        "at_risk_paise": 0,
        "recovered_paise": 0,
        "action_counts": {},
        "causes": {},
    }


def _finalise(bucket: Dict[str, Any]) -> None:
    """Attach the two ratios the UI displays. Both are counts over counts.

    This is not the headline and must never be read as it: a per-arm recovery
    rate is descriptive, and the difference between two of them is *not* the
    contrast, which is bootstrapped over the actioned subset with an interval.
    The frontend labels these as observed rates for that reason.
    """
    bucket["rate"] = bucket["recovered"] / bucket["n"] if bucket["n"] else 0.0
    bucket["value_share"] = (
        bucket["recovered_paise"] / bucket["at_risk_paise"]
        if bucket["at_risk_paise"]
        else 0.0
    )
    bucket["escalations"] = bucket["action_counts"].get("ACT_ESCALATE_HUMAN", 0)


def _accumulate(bucket: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Add one OUTCOME row into an arm bucket. Integer paise only, no floats."""
    bucket["n"] += 1
    bucket["recovered"] += 1 if payload.get("recovered") else 0
    bucket["contacted"] += 1 if payload.get("contacted") else 0
    bucket["at_risk_paise"] += int(payload.get("amount_at_risk_paise", 0))
    bucket["recovered_paise"] += int(payload.get("amount_recovered_paise", 0))
    _bump(bucket["action_counts"], payload.get("action"))
    _bump(bucket["causes"], payload.get("cause"))


# --------------------------------------------------------------------------
# The "why" exemplars -- real rows, deterministically selected
# --------------------------------------------------------------------------
#
# The brief's mockup wants a per-customer thread. The ledger is not keyed for
# one (DASHBOARD-PLAN.md §1), so these are presented as what they are: the audit
# record of one run, each panel labelled with its own `seq` and `kind`, with no
# implied edge between them.
#
# Selection is by rule rather than by hard-coded `seq`, so regenerating the
# golden with `make golden-kinds` cannot silently point a panel at a different
# row than the one its caption describes. Each rule prefers the most informative
# row of its kind and falls back to the first.


def _select(rows: Sequence[Dict[str, Any]], kind: str, prefer=None) -> Optional[Dict[str, Any]]:
    """First row of ``kind`` satisfying ``prefer``, else the first of ``kind``."""
    candidates = [row for row in rows if row["kind"] == kind]
    if not candidates:
        return None
    if prefer is not None:
        for row in candidates:
            if prefer(row):
                return row
    return candidates[0]


def read_why(golden_path: Path) -> List[Dict[str, Any]]:
    """The why panels, from the all-kinds golden chain.

    Returns a list rather than a dict keyed by ``seq`` so the frontend renders
    them in a fixed, meaningful order -- gate, plan, diagnosis, receipt audit,
    action, promise -- instead of in numeric order, which would put the receipt
    audit's verdict before the claim it is a verdict on.
    """
    if not golden_path.exists():
        raise FileNotFoundError(
            "no all-kinds golden at %s -- run `make golden-kinds`" % golden_path.name
        )
    rows: List[Dict[str, Any]] = []
    for line in golden_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # `export_jsonl` writes the payload as its stored *string*, so the row's
        # bytes and the hashed bytes stay identical. Parse it back for reading.
        row["payload"] = json.loads(row["payload"])
        rows.append(row)

    panels: List[Dict[str, Any]] = []

    # An AMEND is the interesting gate row: it is the envelope changing a
    # proposed action rather than waving it through, which is the only visible
    # evidence the envelope does anything.
    gate = _select(rows, "GATE", lambda r: r["decision"] == "AMEND")
    if gate is not None:
        panels.append(
            _panel(gate, "The envelope ruled on a proposed action", {
                "rule_fired": gate["rule_fired"],
                "decision": gate["decision"],
                "verdict": gate["payload"].get("verdict"),
                "reason": gate["payload"].get("reason"),
                "action": gate["payload"].get("action"),
                "channel": gate["payload"].get("channel"),
                "bound_rules": gate["payload"].get("bound_rules"),
                "terminates_thread": gate["payload"].get("terminates_thread"),
            })
        )

    # A plan whose first step actually does something. `ACT_WAIT` is the most
    # common first step and the least informative one to show.
    plan = _select(
        rows, "PLAN",
        lambda r: (r["payload"].get("steps") or [{}])[0].get("action") not in (None, "ACT_WAIT"),
    )
    if plan is not None:
        step = (plan["payload"].get("steps") or [{}])[0]
        panels.append(
            _panel(plan, "The planner proposed a first step", {
                "action": step.get("action"),
                "channel": step.get("channel"),
                "rationale": step.get("rationale"),
                "stop_conditions": step.get("stop_conditions"),
                "expected_value_paise": step.get("expected_value_paise"),
                "cost_paise": step.get("cost_paise"),
                "delay_seconds": step.get("delay_seconds"),
                # The memoisation key. Shown because it is the honest answer to
                # "which customer is this plan for?" -- it is for a signature,
                # not a customer, and the dashboard says so rather than picking
                # a customer to attach it to.
                "signature": plan["payload"].get("signature"),
                "llm_call_ids": plan["payload"].get("llm_call_ids"),
            })
        )

    diagnosis = _select(rows, "DIAGNOSIS")
    if diagnosis is not None:
        panels.append(
            # Caption is precise about provenance. ``all_kinds.jsonl`` builds its
            # DIAGNOSIS from "a real investigator session over a *scripted*
            # model" (tests/test_ledger_kind_coverage.py) -- the agent loop, the
            # tool belt and the receipt auditor are all real, but the model's
            # words are a test double. "What the model claimed" implied a live
            # reply and would not survive a reviewer opening the golden.
            _panel(diagnosis, "What the investigator claimed (scripted model)", {
                "diagnosis_class": diagnosis["payload"].get("diagnosis_class"),
                "confidence": diagnosis["payload"].get("confidence"),
                "claims": diagnosis["payload"].get("claims"),
                "falsifiable_by": diagnosis["payload"].get("falsifiable_by"),
                "falsifier_stated": diagnosis["payload"].get("falsifier_stated"),
                "llm_calls": diagnosis["payload"].get("llm_calls"),
                "stop_reason": diagnosis["payload"].get("stop_reason"),
            })
        )

    audit = _select(rows, "RECEIPT_AUDIT")
    if audit is not None:
        panels.append(
            _panel(audit, "What survived a deterministic check of it", {
                "status": audit["payload"].get("status"),
                "claims_in": audit["payload"].get("claims_in"),
                "claims_stripped": audit["payload"].get("claims_stripped"),
                "claims_surviving": audit["payload"].get("claims_surviving"),
                "receipt_coverage": audit["payload"].get("receipt_coverage"),
                "threshold": audit["payload"].get("threshold"),
                "may_plan_action": audit["payload"].get("may_plan_action"),
                "stripped": audit["payload"].get("stripped"),
            })
        )

    action = _select(rows, "ACTION")
    if action is not None:
        panels.append(
            # Caption chosen carefully. These rows come from the coverage chain,
            # where `run_execute` runs against `FakeTransport` -- the real
            # executor code path (URL, auth, idempotency key, replay guard) over
            # canned HTTP responses. Nothing left the machine. The earlier
            # wording, "the one action actually executed", read as a live
            # Razorpay call, which on a demo screen is a claim the data does not
            # support -- the ids say FAKE for exactly this reason, and the
            # caption should not fight them.
            _panel(action, "The executor's code path, against a fake transport", {
                "action_type": action["payload"].get("action_type"),
                "amount_paise": action["payload"].get("amount_paise"),
                "idempotency_key": action["payload"].get("idempotency_key"),
                "idempotent_replay_was_noop": action["payload"].get("idempotent_replay_was_noop"),
                "order_id": action["payload"].get("order_id"),
                "payment_link_id": action["payload"].get("payment_link_id"),
            })
        )

    promise = _select(rows, "PROMISE")
    if promise is not None:
        panels.append(
            _panel(promise, "What the customer said, in their own words", {
                "state": promise["payload"].get("state"),
                "channel": promise["payload"].get("channel"),
                "confidence": promise["payload"].get("confidence"),
                "promised_date": promise["payload"].get("promised_date"),
                "amount_paise": promise["payload"].get("amount_paise"),
                "verbatim": promise["payload"].get("verbatim"),
            })
        )

    return panels


def read_voice(db_path: Path, audio: Path, transcript: Path) -> Optional[Dict[str, Any]]:
    """The Hinglish recovery call: its gate ruling, its turns, its promise.

    Read from ``build/voice.db`` -- the chain ``make voice`` writes -- rather
    than from the transcript markdown, so what the page shows is what was
    hashed. The transcript file is a rendering of the same call and is linked,
    not parsed.

    Returns ``None`` when the call has not been run, which is not an error: the
    dashboard simply omits the section rather than showing an empty player.
    """
    if not db_path.exists():
        return None
    conn = connect_readonly(db_path)
    try:
        converse: Optional[Dict[str, Any]] = None
        promise: Optional[Dict[str, Any]] = None
        gate_rule = gate_decision = None
        for row in conn.execute("SELECT * FROM ledger ORDER BY seq"):
            if row["kind"] == "CONVERSE":
                converse = _payload(row)
                gate_rule, gate_decision = row["rule_fired"], row["decision"]
            elif row["kind"] == "PROMISE":
                promise = _payload(row)
    finally:
        conn.close()

    if converse is None:
        return None

    return {
        "gate_rule": gate_rule,
        "gate_decision": gate_decision,
        "call_placed": bool(converse.get("call_placed")),
        "disclosure_first": bool(converse.get("disclosure_first")),
        "stood_down": bool(converse.get("stood_down")),
        "standdown_signals": converse.get("standdown_signals") or [],
        "turns": [
            {"speaker": t.get("speaker"), "text": t.get("text"),
             "scripted": bool(t.get("scripted"))}
            for t in (converse.get("turns") or [])
        ],
        "promise": promise,
        # Relative to dashboard/, so the page can link and play them directly.
        "audio": "../assets/" + audio.name if audio.exists() else None,
        "audio_bytes": audio.stat().st_size if audio.exists() else 0,
        "transcript": "../assets/" + transcript.name if transcript.exists() else None,
    }


#: How many LLM-authored plans to carry in full. All 33 on the committed batch
#: fit comfortably; the cap exists so a future run with a warmer cache cannot
#: quietly turn the snapshot into a megabyte.
PLAN_SAMPLE_CAP = 40


def read_plans(jsonl_path: Path) -> Optional[Dict[str, Any]]:
    """The planner's own output: what it proposed, and who actually authored it.

    Read from the execute run's exported chain rather than the demo ledger,
    because ``PLAN`` is the one kind only ``run_shadow`` writes.

    **The three-way split is the point.** "LLM-authored" is not the same as "an
    LLM was called", and conflating them overstates the model's contribution by
    two and a half times on this batch:

      - no call at all -- no cached reply for the signature, so the deterministic
        reason-class table answered;
      - called, reply unreadable -- the model was asked and produced something
        the plan schema rejected, so the same table answered;
      - called, plan used -- the model's own words, in the ledger.

    A plan carries ``llm_call_ids`` in both of the last two cases, so counting
    those ids gives 85 where the honest figure is 33. The discriminator is the
    rationale: the fallback writes a fixed NFR-2 string, and a real reply does
    not.
    """
    if not jsonl_path.exists():
        return None

    counts = {"no_call": 0, "unreadable": 0, "llm_authored": 0}
    actions: Dict[str, Dict[str, int]] = {"llm": {}, "fallback": {}}
    authored: List[Dict[str, Any]] = []
    total = 0

    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") != "PLAN":
            continue
        payload = json.loads(row["payload"])
        total += 1

        called = bool(payload.get("llm_call_ids"))
        fell_back = "NFR-2" in (payload.get("rationale") or "")
        if not called:
            bucket = "no_call"
        elif fell_back:
            bucket = "unreadable"
        else:
            bucket = "llm_authored"
        counts[bucket] += 1

        steps = payload.get("steps") or []
        if steps:
            _bump(actions["llm" if bucket == "llm_authored" else "fallback"],
                  steps[0].get("action"))

        if bucket == "llm_authored" and len(authored) < PLAN_SAMPLE_CAP:
            step = steps[0] if steps else {}
            authored.append({
                "seq": int(row["seq"]),
                "signature": payload.get("signature"),
                "rationale": payload.get("rationale"),
                "action": step.get("action"),
                "channel": step.get("channel"),
                "delay_seconds": step.get("delay_seconds"),
                "cost_paise": step.get("cost_paise"),
                "expected_value_paise": step.get("expected_value_paise"),
                "stop_conditions": step.get("stop_conditions") or [],
                "step_rationale": step.get("rationale"),
            })

    if not total:
        return None

    called_total = counts["unreadable"] + counts["llm_authored"]
    return {
        "signatures": total,
        "counts": counts,
        "called": called_total,
        # Of the times the model was actually asked, how often was the answer
        # usable. Reported because a planner that degrades silently looks
        # identical to one that never degrades.
        "usable_reply_rate": (
            counts["llm_authored"] / called_total if called_total else None
        ),
        "first_step_actions": actions,
        "authored": authored,
        "source": jsonl_path.name,
    }


def read_canary(db_path: Path) -> Optional[Dict[str, Any]]:
    """The canary's verdict on the investigator, from the investigate run's chain.

    A third assertion, separate from the two beside it on purpose: ``DIAGNOSIS``
    is what the model said, ``RECEIPT_AUDIT`` is what survived a check of its
    citations, and ``CANARY`` is whether the conclusion survives the arithmetic.
    A ``RETRACTION`` appears only when a verdict is refuted -- the system
    withdrawing a claim it had already made -- so its absence is a result too,
    and is reported rather than left as a blank.

    Returns ``None`` when no investigate run has happened, which is not an
    error: the section is simply omitted.
    """
    if not db_path.exists():
        return None
    conn = connect_readonly(db_path)
    try:
        canary = retraction = None
        for row in conn.execute("SELECT * FROM ledger ORDER BY seq"):
            if row["kind"] == "CANARY":
                canary = (int(row["seq"]), _payload(row))
            elif row["kind"] == "RETRACTION":
                retraction = (int(row["seq"]), _payload(row))
    finally:
        conn.close()

    if canary is None:
        return None
    seq, payload = canary
    return {
        "seq": seq,
        "verdict": payload.get("verdict"),
        "observed_rate_segment": payload.get("observed_rate_segment"),
        "truth_rate_segment": payload.get("truth_rate_segment"),
        "observed_mix_segment": payload.get("observed_mix_segment"),
        "truth_mix_segment": payload.get("truth_mix_segment"),
        # Computed here rather than in the browser so the page keeps formatting
        # and never deciding whether a claim held.
        "rate_matches": payload.get("observed_rate_segment") == payload.get("truth_rate_segment"),
        "mix_matches": payload.get("observed_mix_segment") == payload.get("truth_mix_segment"),
        "retraction": None if retraction is None else {
            "seq": retraction[0],
            "retracted_verdict": retraction[1].get("retracted_verdict"),
        },
        "source": db_path.name,
    }


def _panel(row: Dict[str, Any], caption: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """One why-panel: the row's identity, a caption, and named fields.

    ``seq`` and ``row_hash`` travel with every panel so a reviewer can find the
    exact row in ``tests/golden/all_kinds.jsonl`` and check it. A "why" panel
    whose contents cannot be traced back to a hashed row is a story, not a
    receipt.
    """
    return {
        "seq": int(row["seq"]),
        "ts": row["ts"],
        "kind": row["kind"],
        "arm": row["arm"],
        "row_hash": row["row_hash"],
        "caption": caption,
        "fields": fields,
    }


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_snapshot(
    db_path: Path,
    *,
    metrics_path: Optional[Path] = None,
    golden_path: Path = DEFAULT_GOLDEN,
    actions_csv: Optional[Path] = None,
) -> Dict[str, Any]:
    """The whole ``build/dashboard.json`` object.

    ``actions_csv``, when given, also writes the row-level export from the same
    single pass over the ledger and records its name and row count in
    ``provenance``. Same pass deliberately: two passes could disagree, and then
    the table and the tiles would be two readings of one file.

    ``metrics_path`` is optional and its absence is reported rather than filled
    in: without it there is no headline, and the frontend says so. Inventing one
    from the ledger would be the second-truth failure this module exists to
    prevent -- the ledger's own C-A over all 6,000 outcomes is a different
    quantity from the actioned-subset headline, and showing it under the
    headline's label is the category error README warns about.
    """
    conn = connect_readonly(db_path)
    try:
        ledger = read_ledger(conn)
        rows = read_rows(conn) if actions_csv is not None else []
        detected = read_detected(conn) if actions_csv is not None else None
    finally:
        conn.close()

    if actions_csv is not None:
        write_actions_csv(rows, actions_csv)
    detected_csv = None
    if detected is not None and actions_csv is not None:
        detected_csv = actions_csv.with_name("detected.csv")
        write_detected_csv(detected["rows"], detected_csv)

    if not ledger["arms"]:
        raise ValueError(
            "%s has no OUTCOME rows, so there is nothing to render. The demo "
            "batches write them; the execute batch writes only PLAN rows. Run "
            "`make demo-full` and point --db at build/pramaan-full.db."
            % db_path.name
        )

    headline: Optional[Dict[str, Any]] = None
    if metrics_path is not None and metrics_path.exists():
        headline = json.loads(metrics_path.read_text(encoding="utf-8"))

    return {
        "schema_version": SCHEMA_VERSION,
        # Provenance, not decoration: the head hash is the same 64-hex string
        # `make demo-full` prints, so a reviewer can confirm the dashboard is
        # rendering the chain they were shown and not some other run. Basenames
        # only -- an absolute path in the output would carry the developer's
        # home directory into a committed artifact and break I8's diff.
        "provenance": {
            "ledger_file": db_path.name,
            "ledger_head_hash": ledger["head_hash"],
            "ledger_rows": ledger["rows"],
            "kind_counts": ledger["kind_counts"],
            "metrics_file": metrics_path.name if metrics_path is not None else None,
            "golden_file": golden_path.name,
            # The row-level export. The page fetches this lazily, only when the
            # data section is opened, so the initial load stays small.
            "rows_file": actions_csv.name if actions_csv is not None else None,
            "rows_exported": len(rows),
            "rows_columns": list(ACTION_CSV_COLUMNS),
            "detected_file": detected_csv.name if detected_csv is not None else None,
            "detected_columns": list(DETECTED_CSV_COLUMNS),
        },
        # The batch as it arrived, before anything judged it. Distributions
        # rather than 6,000 rows: the individual events are already browsable
        # in the row table, and what this answers is "what shape was the input".
        "detected": None if detected is None else {
            "count": detected["count"],
            "at_risk_paise": detected["at_risk_paise"],
            "facets": detected["facets"],
            "amount_bands": detected["amount_bands"],
            "resolved": detected["resolved"],
            "unresolved_count": detected["unresolved_count"],
            "unresolved": detected["unresolved"],
        },
        "headline": headline,
        "ledger": {
            "arms": ledger["arms"],
            "gate_decisions": ledger["gate_decisions"],
            "gate_rules": ledger["gate_rules"],
            "exception_reasons": ledger["exception_reasons"],
            "exception_actions": ledger["exception_actions"],
        },
        "by_category": ledger["by_category"],
        "why": read_why(golden_path),
        # The planner's own output. Optional: present once an execute run has
        # exported its chain.
        "plans": read_plans(BUILD_DIR / "execute-full.jsonl"),
        # The canary's verdict on the investigator. Optional: present once
        # `make investigate` has run.
        "canary": read_canary(BUILD_DIR / "investigate-42.db"),
        # Optional: present only once `make voice` has run.
        "voice": read_voice(
            BUILD_DIR / "voice.db",
            ROOT / "assets" / "voice-demo.mp3",
            ROOT / "assets" / "voice-transcript.md",
        ),
    }


def write_snapshot(snapshot: Dict[str, Any], path: Path) -> Path:
    """Canonical JSON, LF-terminated. Byte-identical for identical inputs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(snapshot))
        handle.write("\n")
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pramaan.report.snapshot",
        description="Aggregate a committed ledger into build/dashboard.json.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=BUILD_DIR / "pramaan-full.db",
        help="ledger to read. Must contain OUTCOME rows (a demo batch, not an "
             "execute batch). Opened read-only.",
    )
    parser.add_argument(
        "--metrics",
        type=Path,
        default=BUILD_DIR / "execute-full-metrics.json",
        help="BatchMetrics written by `make execute-full`. Absent is allowed: "
             "the dashboard then renders without a headline rather than "
             "deriving one.",
    )
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--out", type=Path, default=BUILD_DIR / "dashboard.json")
    parser.add_argument(
        "--actions-csv",
        type=Path,
        default=BUILD_DIR / "actions.csv",
        help="row-level export: one line per resolved event, joined across "
             "DETECT/GATE/OUTCOME/EXCEPTION. Opens in a spreadsheet; the "
             "dashboard's data table reads this same file.",
    )
    args = parser.parse_args(argv)

    snapshot = build_snapshot(
        args.db,
        metrics_path=args.metrics,
        golden_path=args.golden,
        actions_csv=args.actions_csv,
    )
    out = write_snapshot(snapshot, args.out)

    print("dashboard snapshot")
    print("  ledger        %s (%s rows, head %s)" % (
        snapshot["provenance"]["ledger_file"],
        snapshot["provenance"]["ledger_rows"],
        (snapshot["provenance"]["ledger_head_hash"] or "")[:12],
    ))
    print("  headline      %s" % (
        snapshot["provenance"]["metrics_file"]
        if snapshot["headline"] is not None
        else "absent -- run `make execute-full` for the contrast tiles"
    ))
    print("  why panels    %d" % len(snapshot["why"]))
    print("  wrote         %s" % out.name)
    print("  detected      %s (%s events, %d columns)" % (
        snapshot["provenance"]["detected_file"],
        snapshot["detected"]["count"] if snapshot["detected"] else 0,
        len(DETECTED_CSV_COLUMNS),
    ))
    print("  rows          %s (%s events, %d columns)" % (
        snapshot["provenance"]["rows_file"],
        snapshot["provenance"]["rows_exported"],
        len(ACTION_CSV_COLUMNS),
    ))
    print()
    # The demo server, not `http.server`: it serves these same files *and*
    # exposes the run endpoints the dashboard's buttons call, so the page can
    # start a pipeline instead of only reporting one. A plain static server
    # still works and the page degrades to a read-only report.
    print("  To drive the pipeline from the page:")
    print("      python -m pramaan.report.server")
    print("  then open  http://127.0.0.1:8000/dashboard/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
