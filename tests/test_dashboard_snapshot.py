"""The dashboard snapshot reads; it does not derive, mutate, or invent.

``pramaan/report/snapshot.py`` is presentation, and presentation is where a
second, informal implementation of the pipeline would grow if one were going to.
These tests pin the four properties that stop that happening, each of which is a
claim DASHBOARD-PLAN.md §2 makes in prose:

1. **No LLM edge.** Importing the module must not pull in ``pramaan.llm``.
   Checked in a subprocess, because by the time the rest of the suite has run,
   the parent interpreter has imported half the project and ``sys.modules`` no
   longer says anything about what *this* module needs.
2. **Read-only, enforced by SQLite** rather than by convention.
3. **Deterministic bytes**, the same discipline as I8.
4. **Every number traces to a row.** The aggregates are cross-checked against a
   direct SQL count over the same file -- a different query shape reaching the
   same total -- and every why-panel is matched back to a real hashed row in the
   committed golden.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from pramaan.cli import gate_events
from pramaan.config import ROOT
from pramaan.eval.metrics import compute_metrics
from pramaan.eval.resolve import resolve_batch
from pramaan.ledger.chain import DECISIONS, Ledger
from pramaan.report import snapshot as S
from pramaan.sense.store import EventStore, connect, ingest
from sim.generate import dev_batch

GOLDEN = ROOT / "tests" / "golden" / "all_kinds.jsonl"


@pytest.fixture(scope="module")
def ledger_db(tmp_path_factory) -> Path:
    """A real on-disk ledger with the four kinds the snapshot reads.

    Built by the same writers production uses -- ``ingest`` for DETECT,
    ``gate_events`` for GATE, ``resolve_batch`` for OUTCOME/EXCEPTION -- so a
    change to a payload shape breaks these tests rather than silently changing
    what the dashboard renders. ``run_shadow`` is deliberately not called: it
    would add PLAN rows the snapshot does not read and a 10,000-resample
    bootstrap this fixture does not need.
    """
    path = tmp_path_factory.mktemp("dashboard") / "ledger.db"
    conn = connect(path)
    store = EventStore(conn)
    ledger = Ledger(conn)
    events = dev_batch(42)
    ingest(store, ledger, events)
    gate_events(events, ledger)
    resolve_batch(events, ledger=ledger)
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def snap(ledger_db: Path) -> dict:
    return S.build_snapshot(ledger_db, metrics_path=None, golden_path=GOLDEN)


# --------------------------------------------------------------------------
# 1. No import edge into pramaan.llm
# --------------------------------------------------------------------------


def test_importing_the_snapshot_does_not_import_the_llm_package():
    """The dashboard must not be able to reach a model, even accidentally.

    A subprocess with a clean interpreter is the only honest way to ask this:
    inside the test session ``pramaan.llm`` is already imported by other tests,
    so an in-process ``sys.modules`` check would pass no matter what this module
    imports.
    """
    probe = (
        "import sys; import pramaan.report.snapshot; "
        "print(sorted(m for m in sys.modules if m.startswith('pramaan.llm')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", (
        "pramaan.report.snapshot pulled in %s. The dashboard is presentation and "
        "must have no path to a model." % result.stdout.strip()
    )


# --------------------------------------------------------------------------
# 2. Read-only, enforced by the driver
# --------------------------------------------------------------------------


def test_the_connection_refuses_writes(ledger_db: Path):
    """Not "we never call a writer" -- SQLite itself rejects the statement."""
    conn = S.connect_readonly(ledger_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM ledger WHERE seq = 1")
    finally:
        conn.close()


def test_a_missing_ledger_says_which_command_makes_one(tmp_path: Path):
    with pytest.raises(FileNotFoundError) as excinfo:
        S.connect_readonly(tmp_path / "absent.db")
    assert "make demo-full" in str(excinfo.value)


def test_a_ledger_with_no_outcomes_is_refused(tmp_path: Path):
    """The execute batch writes only PLAN rows. Rendering it would draw zeroes.

    DASHBOARD-PLAN.md rev 1 named that file as the dashboard's source. Failing
    loudly here is what stops that mistake being made a second time.
    """
    path = tmp_path / "plans-only.db"
    conn = connect(path)
    Ledger(conn).append(
        "PLAN", "2026-08-03T10:00:00+05:30",
        {"signature": "reason_class=TECH_TRANSIENT", "steps": [], "llm_call_ids": []},
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="no OUTCOME rows"):
        S.build_snapshot(path, metrics_path=None, golden_path=GOLDEN)


# --------------------------------------------------------------------------
# 3. Deterministic bytes (I8's discipline)
# --------------------------------------------------------------------------


def test_two_runs_over_one_ledger_are_byte_identical(ledger_db: Path, tmp_path: Path):
    first = S.write_snapshot(
        S.build_snapshot(ledger_db, metrics_path=None, golden_path=GOLDEN),
        tmp_path / "one.json",
    )
    second = S.write_snapshot(
        S.build_snapshot(ledger_db, metrics_path=None, golden_path=GOLDEN),
        tmp_path / "two.json",
    )
    assert first.read_bytes() == second.read_bytes()


def test_the_output_carries_no_absolute_path(snap: dict, tmp_path: Path):
    """An absolute path would put the developer's home directory in the artifact.

    Same reason ``cli`` prints ``_display_path``: I8 diffs the output, and a
    machine-specific string makes the diff fail for a reason that has nothing to
    do with the numbers.
    """
    text = S.write_snapshot(snap, tmp_path / "paths.json").read_text(encoding="utf-8")
    assert str(ROOT) not in text
    assert str(ROOT).replace("\\", "\\\\") not in text
    assert snap["provenance"]["ledger_file"] == "ledger.db"


def test_the_snapshot_ends_in_exactly_one_newline(snap: dict, tmp_path: Path):
    raw = S.write_snapshot(snap, tmp_path / "nl.json").read_bytes()
    assert raw.endswith(b"}\n")
    assert b"\r\n" not in raw, "CRLF would break a byte comparison on Windows"


# --------------------------------------------------------------------------
# 4. Every number traces to a row
# --------------------------------------------------------------------------


def test_arm_totals_match_a_direct_sql_aggregate(ledger_db: Path, snap: dict):
    """The same totals, reached by a different query shape.

    ``read_ledger`` accumulates in one ordered Python pass; this recomputes the
    same figures in SQL. Agreement is evidence the pass is not dropping or
    double-counting rows -- the class of bug that would otherwise show up as a
    plausible-looking number.
    """
    conn = sqlite3.connect(ledger_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT arm, payload FROM ledger WHERE kind = 'OUTCOME'"
    ).fetchall()
    conn.close()

    expected: dict = {}
    for row in rows:
        payload = json.loads(row["payload"])
        bucket = expected.setdefault(
            row["arm"], {"n": 0, "recovered": 0, "contacted": 0,
                         "at_risk_paise": 0, "recovered_paise": 0}
        )
        bucket["n"] += 1
        bucket["recovered"] += 1 if payload["recovered"] else 0
        bucket["contacted"] += 1 if payload["contacted"] else 0
        bucket["at_risk_paise"] += payload["amount_at_risk_paise"]
        bucket["recovered_paise"] += payload["amount_recovered_paise"]

    assert set(snap["ledger"]["arms"]) == set(expected)
    for arm, want in expected.items():
        got = snap["ledger"]["arms"][arm]
        for field, value in want.items():
            assert got[field] == value, "arm %s field %s" % (arm, field)


def test_every_arm_total_is_integer_paise(snap: dict):
    """No floats in a money field. A rounded rupee is a wrong rupee."""
    for arm in snap["ledger"]["arms"].values():
        assert isinstance(arm["at_risk_paise"], int)
        assert isinstance(arm["recovered_paise"], int)
        assert not isinstance(arm["at_risk_paise"], bool)


def test_gate_decisions_report_every_decision_including_the_zero(snap: dict):
    """README states "0 REJECT" as a finding. An absent key would render as blank.

    A blank cell reads as "not measured". The zero is the measurement.
    """
    assert set(snap["ledger"]["gate_decisions"]) == set(DECISIONS)
    assert snap["ledger"]["gate_decisions"]["REJECT"] == 0


def test_gate_decision_total_equals_the_gate_row_count(snap: dict):
    total = sum(snap["ledger"]["gate_decisions"].values())
    assert total == snap["provenance"]["kind_counts"]["GATE"]


def test_category_totals_sum_to_the_arm_totals(snap: dict):
    """Grouping by source_type must not lose or duplicate an outcome.

    Every OUTCOME joins to exactly one DETECT and therefore to exactly one
    category, so the per-category arm counts must add back up to the ungrouped
    ones. On the committed batch there is one category, which makes this a weak
    check today and the right check the day a multi-type batch lands.
    """
    per_arm: dict = {}
    for category in snap["by_category"]:
        for arm, bucket in category["arms"].items():
            per_arm[arm] = per_arm.get(arm, 0) + bucket["n"]
    for arm, bucket in snap["ledger"]["arms"].items():
        assert per_arm[arm] == bucket["n"]


def test_provenance_head_hash_is_the_chains_last_row_hash(ledger_db: Path, snap: dict):
    """The dashboard names the chain it rendered, in the form the demo prints."""
    conn = sqlite3.connect(ledger_db)
    head = conn.execute(
        "SELECT row_hash FROM ledger ORDER BY seq DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()
    assert snap["provenance"]["ledger_head_hash"] == head
    assert len(head) == 64


# --------------------------------------------------------------------------
# The why panels
# --------------------------------------------------------------------------


def test_every_why_panel_traces_to_a_real_golden_row(snap: dict):
    """A "why" whose contents cannot be found in a hashed row is a story."""
    by_seq = {}
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            by_seq[int(row["seq"])] = row

    assert snap["why"], "no panels selected from the golden chain"
    for panel in snap["why"]:
        row = by_seq.get(panel["seq"])
        assert row is not None, "panel cites seq %s, absent from the golden" % panel["seq"]
        assert row["kind"] == panel["kind"]
        assert row["row_hash"] == panel["row_hash"]


def test_the_gate_panel_shows_an_amend_not_a_rubber_stamp(snap: dict):
    """An ALLOW panel would be evidence of nothing. The envelope earning its
    place means changing a proposed action, so that is the row selected."""
    gate = [p for p in snap["why"] if p["kind"] == "GATE"]
    assert gate and gate[0]["fields"]["decision"] == "AMEND"
    assert gate[0]["fields"]["rule_fired"]


def test_the_plan_panel_shows_a_step_that_does_something(snap: dict):
    plan = [p for p in snap["why"] if p["kind"] == "PLAN"]
    assert plan and plan[0]["fields"]["action"] not in (None, "ACT_WAIT")
    assert plan[0]["fields"]["stop_conditions"], "a step with no stop conditions"


def test_the_plan_panel_shows_its_signature_not_a_customer(snap: dict):
    """A plan is memoised per signature and belongs to no single customer.

    DASHBOARD-PLAN.md §1: attaching one to a counterparty would be inventing an
    edge the ledger does not have.
    """
    plan = [p for p in snap["why"] if p["kind"] == "PLAN"][0]
    assert "signature" in plan["fields"]
    assert "counterparty_id" not in plan["fields"]


def test_the_receipt_audit_panel_reports_what_was_stripped(snap: dict):
    """The pairing that is the whole thesis: the claim, then the check of it."""
    kinds = [p["kind"] for p in snap["why"]]
    assert kinds.index("DIAGNOSIS") < kinds.index("RECEIPT_AUDIT"), (
        "the verdict must render after the claim it is a verdict on"
    )
    audit = [p for p in snap["why"] if p["kind"] == "RECEIPT_AUDIT"][0]
    assert audit["fields"]["claims_in"] >= audit["fields"]["claims_surviving"]
    assert "may_plan_action" in audit["fields"]


def test_a_missing_golden_says_which_command_makes_one(ledger_db: Path, tmp_path: Path):
    with pytest.raises(FileNotFoundError) as excinfo:
        S.build_snapshot(ledger_db, metrics_path=None, golden_path=tmp_path / "gone.jsonl")
    assert "make golden-kinds" in str(excinfo.value)


# --------------------------------------------------------------------------
# Serialising BatchMetrics -- the headline is copied, never recomputed
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def metrics():
    """A real BatchMetrics. Few resamples: the interval's *value* is not under
    test here, only that serialising it changes nothing."""
    return compute_metrics(resolve_batch(dev_batch(42)), seed=42, resamples=200)


def test_serialising_metrics_preserves_the_point_estimate_exactly(metrics):
    """Not "close to" -- equal. The dashboard shows the number the report printed.

    Any transformation here would be a second derivation of the headline, which
    is the failure ``eval/metrics.py`` opens by warning about.
    """
    payload = S.metrics_to_dict(metrics)
    for name, contrast in metrics.contrasts.items():
        for statistic, interval in contrast.intervals.items():
            got = payload["contrasts"][name]["intervals"][statistic]
            assert got["point"] == interval.point
            assert got["low"] == interval.low
            assert got["high"] == interval.high
            assert got["method"] == interval.method
            assert got["resamples"] == interval.resamples


def test_serialised_contrasts_carry_the_actionable_only_flag(metrics):
    """``Contrast`` carries this flag so a caller "cannot print a subgroup figure
    as though it were the headline". A dashboard tile is such a caller."""
    payload = S.metrics_to_dict(metrics)
    assert payload["contrasts"]["B-A actioned"]["actionable_only"] is True
    assert payload["contrasts"]["B-A"]["actionable_only"] is False
    assert payload["contrasts"]["C-A"]["actionable_only"] is False


def test_serialised_intervals_keep_the_method_and_any_fallback_reason(metrics):
    payload = S.metrics_to_dict(metrics)
    for contrast in payload["contrasts"].values():
        for interval in contrast["intervals"].values():
            assert interval["method"] in ("BCa", "percentile", "normal")
            assert "fallback_reason" in interval


def test_latent_truth_is_not_serialised(metrics):
    """``would_recover_unaided`` is the answer key. ADR-010 keeps it out of the
    ledger because the ledger is audited; it stays out of the dashboard because
    the dashboard is watched."""
    payload = S.metrics_to_dict(metrics)
    for summary in payload["summaries"].values():
        assert "would_recover_unaided" not in summary


def test_metrics_written_to_disk_round_trip_unchanged(metrics, tmp_path: Path):
    path = S.write_metrics(metrics, tmp_path / "m.json")
    assert json.loads(path.read_text(encoding="utf-8")) == S.metrics_to_dict(metrics)
    assert path.read_bytes().endswith(b"\n")


# --------------------------------------------------------------------------
# The row-level CSV export
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def csv_path(ledger_db: Path, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("csv") / "actions.csv"
    S.build_snapshot(
        ledger_db, metrics_path=None, golden_path=GOLDEN, actions_csv=out
    )
    return out


def test_the_csv_has_one_row_per_outcome(ledger_db: Path, csv_path: Path):
    """The export is the OUTCOME rows, joined -- not a subset and not a product.

    An off-by-one here would be a join that dropped or duplicated events, which
    is the failure mode that produces a plausible-looking table.
    """
    conn = sqlite3.connect(ledger_db)
    outcomes = conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE kind = 'OUTCOME'"
    ).fetchone()[0]
    conn.close()
    with open(csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == outcomes


def test_the_csv_header_is_the_declared_column_order(csv_path: Path):
    """Position-stable: someone's saved spreadsheet or awk script indexes by it."""
    with open(csv_path, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert tuple(header) == S.ACTION_CSV_COLUMNS


def test_the_csv_is_byte_identical_across_runs(ledger_db: Path, tmp_path: Path):
    first = tmp_path / "one.csv"
    second = tmp_path / "two.csv"
    for out in (first, second):
        S.build_snapshot(
            ledger_db, metrics_path=None, golden_path=GOLDEN, actions_csv=out
        )
    assert first.read_bytes() == second.read_bytes()
    assert b"\r\n" not in first.read_bytes(), "CRLF would break a byte comparison"


def test_the_csv_carries_no_latent_truth(csv_path: Path):
    """ADR-010's rule, applied to the one artifact a reviewer can download.

    ``would_recover_unaided`` is the answer key. A CSV containing it would let
    anyone compute the true per-event effect and turn an audit artifact into an
    answer sheet, so the column set is asserted rather than trusted.
    """
    with open(csv_path, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    for banned in ("would_recover_unaided", "self_recovers_at", "has_intent",
                   "route_would_succeed", "latent"):
        assert not any(banned in column for column in header), banned


def test_every_real_row_keeps_its_column_count(csv_path: Path):
    """Read back with a real parser: bad quoting shows up as column drift."""
    with open(csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        assert len(row) == len(S.ACTION_CSV_COLUMNS)


def test_punctuation_in_the_refusal_prose_survives_a_round_trip(tmp_path: Path):
    """``exception_reason`` carries the envelope's own prose, commas and all.

    Tested against the writer directly rather than against the fixture, and the
    first version of this test is the reason. It asserted that some row in the
    generated CSV contained a quoted cell -- which passed on the five-type full
    batch (760 rows of R1 citations) and failed on the payment-only dev batch
    the fixture builds, where the single EXCEPTION happens to have no comma. A
    test whose subject depends on which batch it runs against is testing the
    batch. This one states the property: whatever punctuation a payload brings,
    the file round-trips.
    """
    nasty = 'envelope refused: R1 (no pre-debit notification, "ever", was sent)'
    path = S.write_actions_csv(
        [{"seq": 1, "exception_reason": nasty, "recovered": False}], tmp_path / "q.csv"
    )
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert len(rows[0]) == len(S.ACTION_CSV_COLUMNS)
    assert rows[0]["exception_reason"] == nasty
    assert rows[0]["recovered"] == "false"
    # And the cell really was quoted on disk, not silently stripped.
    assert '"' in path.read_text(encoding="utf-8")


def test_booleans_render_as_lowercase_words(csv_path: Path):
    """``true``/``false``, not Python's ``True``/``False``.

    The dashboard's parser and a spreadsheet both read the file; Python's repr
    would be the only reader-specific spelling in it.
    """
    with open(csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        assert row["recovered"] in ("true", "false")
        assert row["contacted"] in ("true", "false")


def test_a_carriage_return_in_a_cell_is_refused():
    with pytest.raises(ValueError, match="carriage return"):
        S._csv_cell("two\rlines")


def test_provenance_names_the_csv_and_its_row_count(ledger_db: Path, tmp_path: Path):
    out = tmp_path / "actions.csv"
    snapshot = S.build_snapshot(
        ledger_db, metrics_path=None, golden_path=GOLDEN, actions_csv=out
    )
    assert snapshot["provenance"]["rows_file"] == "actions.csv"
    assert snapshot["provenance"]["rows_exported"] > 0
    assert snapshot["provenance"]["rows_columns"] == list(S.ACTION_CSV_COLUMNS)


def test_the_snapshot_json_does_not_embed_the_rows(snap: dict):
    """The rows live in the CSV precisely so the JSON stays small enough to
    fetch on every page load. A ``rows`` key here would undo that."""
    assert "rows" not in snap


def test_non_finite_values_become_null_rather_than_crashing():
    """``canonical_json`` sets ``allow_nan=False``, and this system produces
    infinities: ``gross_over_claim`` returns one on an underpowered batch whose
    incremental point estimate lands at or below zero."""
    assert S._finite(float("inf")) is None
    assert S._finite(float("nan")) is None
    assert S._finite(0.25) == 0.25
    assert math.isfinite(S._finite(-3.0))
