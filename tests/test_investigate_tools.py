"""The tool belt: isolation, read-only enforcement, canonicality, arithmetic.

Four properties, and each is load-bearing for a different reason.

**The agent cannot reach ground truth.** Not "does not"; cannot. The projected
database has no ``latent`` table in it, so the strongest test available is that a
query naming it fails, and that no projected column is a function of it.

**The belt is read-only.** Tested through the authorizer rather than only through
the keyword screen, because the keyword screen is the layer that could be
bypassed by creative spelling and the authorizer is the layer that cannot.

**Every rendered result is safe in a prompt.** The unmodified
``assert_no_identifiers`` is run over every tool's output. This is the test that
lets the canonicality chokepoint stay untouched while a multi-turn agent feeds
tool results back into prompts -- and it is the test that would fail loudly if
someone later added a raw amount or a timestamp to the projection.

**The decomposition is arithmetically correct.** Checked against a hand-rolled
shift-share computed from raw SQL counts inside the test, never against the
tool's own helpers. ADR-023: a test may not compare a function to its own
delegate.
"""
from __future__ import annotations

import sqlite3

import pytest

from pramaan.investigate import tools as T
from pramaan.llm.prompts import PromptCanonicalityError, assert_no_identifiers
from sim.generate import dev_batch_degraded
from sim.incident import DEV_INCIDENT, build_downtime, build_traffic


@pytest.fixture(scope="module")
def world():
    events, truth = dev_batch_degraded()
    return events, build_traffic(events, 42, DEV_INCIDENT), build_downtime(DEV_INCIDENT), truth


@pytest.fixture()
def belt(world):
    events, traffic, downtime, _t = world
    return T.ToolBelt(T.build_agent_db(events, traffic, downtime))


def _projected_columns(world):
    """Every column name in the projection, via an unauthorized connection."""
    events, traffic, downtime, _t = world
    raw = T.build_agent_db(events, traffic, downtime, authorize=False)
    return {
        row[1].lower()
        for table in T.AGENT_TABLES
        for row in raw.execute("PRAGMA table_info(%s)" % table).fetchall()
    }


# --------------------------------------------------------------------------
# Isolation from ground truth
# --------------------------------------------------------------------------


def test_the_projection_has_only_the_three_agent_tables(world, belt):
    """No ``events``, no ``latent``, no ``runs``, no ``ledger``.

    STATE.md's Day 4 carry-in note 3: the latent table now holds ``has_intent``,
    and an investigator that could read it would know which customers will
    respond before contacting any of them. A denylist would be one regex away
    from failing open; absence is not.
    """
    # Introspection needs an unauthorized connection: the authorizer refuses
    # reads of sqlite_master, which is itself the correct behaviour and is
    # asserted separately below.
    events, traffic, downtime, _t = world
    raw = T.build_agent_db(events, traffic, downtime, authorize=False)
    names = {
        r[0]
        for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert names == set(T.AGENT_TABLES)
    # And the agent itself cannot even enumerate them.
    assert belt.call("query_sql", {"sql": "SELECT name FROM sqlite_master"}).ok is False


def test_querying_the_latent_table_fails(belt):
    for sql in (
        "SELECT * FROM latent",
        "SELECT has_intent FROM latent LIMIT 1",
        "SELECT e.arm FROM agent_events e JOIN latent l ON 1=1",
        "SELECT * FROM events",
    ):
        result = belt.call("query_sql", {"sql": sql})
        assert result.ok is False, "reached %r" % sql


def test_no_projected_column_mentions_intent_or_recovery(world):
    """A column named after ground truth would be a leak even if empty today.

    Cheap, and it catches the plausible future mistake: someone adds
    ``self_recovers`` to the projection for a Day 6 metric and the investigator
    silently gains the answer key.
    """
    columns = _projected_columns(world)
    for forbidden in ("intent", "recover", "latent", "capability", "would_succeed", "lag"):
        assert not any(forbidden in c for c in columns), forbidden


def test_the_projection_carries_no_identifier_column(world):
    columns = _projected_columns(world)
    for forbidden in ("event_id", "counterparty_id", "external_ref", "detected_at", "paise"):
        assert forbidden not in columns, forbidden


# --------------------------------------------------------------------------
# Read-only
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO agent_events VALUES (0,0,'business',0,'payment','x','x','metro','payer','service','minutes',1,'x',1,'full',1,'allowed','A')",
        "UPDATE agent_traffic SET attempts = 0",
        "DELETE FROM agent_events",
        "DROP TABLE agent_events",
        "CREATE TABLE sneaky (x INT)",
        "ALTER TABLE agent_events ADD COLUMN x INT",
        "ATTACH DATABASE 'build/pramaan-dev.db' AS other",
        "PRAGMA table_info(agent_events)",
        "SELECT 1; DROP TABLE agent_events",
    ],
)
def test_writes_and_schema_operations_are_refused(belt, sql):
    result = belt.call("query_sql", {"sql": sql})
    assert result.ok is False, "permitted %r" % sql
    assert belt.conn.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0] > 0


def test_the_authorizer_refuses_writes_even_past_the_keyword_screen(world):
    """The second line of defence, tested on its own.

    The keyword screen is a string check and string checks are evadable. This
    goes straight to the connection, bypassing ``query_sql`` entirely, and shows
    that a write fails at the SQLite layer regardless of how it was spelled.
    """
    events, traffic, downtime, _t = world
    conn = T.build_agent_db(events, traffic, downtime)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM agent_events")


def test_a_select_that_looks_like_a_keyword_still_works(belt):
    """``created_day`` must not be refused for containing ``create``.

    A tool that rejects valid queries teaches the agent to stop using it, and the
    agent then reasons from the transcript instead of from the data -- which is
    the failure this whole design exists to prevent.
    """
    result = belt.call(
        "query_sql",
        {"sql": "SELECT day_index AS created_day, COUNT(*) n FROM agent_events GROUP BY 1 LIMIT 3"},
    )
    assert result.ok is True
    assert result.row_count == 3


def test_there_is_no_write_tool_of_any_kind(belt):
    """``run_canary`` is the only tool with side effects and it is not here.

    BUILD-PLAN Day 4: "Do not give the agent any write tool." Asserted rather
    than remembered, because the tool belt is exactly the place a helpful future
    edit would add one.
    """
    assert "run_canary" not in T.ToolBelt.TOOLS
    for name in T.ToolBelt.TOOLS:
        assert hasattr(belt, "_t_" + name)
    assert belt.call("run_canary", {"hypothesis": "x"}).ok is False


# --------------------------------------------------------------------------
# Canonicality -- the property that lets the chokepoint stay strict
# --------------------------------------------------------------------------


def test_every_tool_output_passes_the_unmodified_screen(belt):
    """The Day 4 collision, resolved and asserted.

    ``llm.call`` screens every prompt with this exact function. A multi-turn
    investigator feeds tool results into prompts, so if any tool rendered a raw
    timestamp, a paise amount, a ``cp_`` identifier or a hex digest, the build
    would break on the first turn -- and the wrong fix would be to loosen the
    screen, which PRD 9.1 says silently takes the token budget from ~800K to ~15M.

    So the data is canonical instead, and this is the test that keeps it so.
    """
    calls = [
        ("decompose", {"window": [3, 4], "dimension": "segment"}),
        ("compare_baseline", {"window": [3, 4]}),
        ("compare_baseline", {"window": [3, 4], "dimension": "reason_class"}),
        ("get_downtime", {"window": [3, 4]}),
        ("get_downtime", {}),
        ("get_merchant_config", {}),
        ("get_reason_taxonomy", {}),
        ("query_sql", {"sql": "SELECT * FROM agent_events LIMIT 5"}),
        ("query_sql", {"sql": "SELECT SUM(attempts) a FROM agent_traffic"}),
        ("query_sql", {"sql": "SELECT amount_band_label, COUNT(*) n FROM agent_events GROUP BY 1"}),
        ("query_sql", {"sql": "SELECT * FROM agent_downtime"}),
        ("query_sql", {"sql": "SELECT * FROM latent"}),          # a refusal renders too
        ("nonexistent_tool", {}),
    ]
    for tool, args in calls:
        result = belt.call(tool, args)
        # Raises rather than returns False, which is the right shape: this is a
        # build break, not a runtime condition.
        assert_no_identifiers(result.rendered, context="%s output" % tool)


def test_a_large_count_renders_with_separators(belt):
    """Four consecutive digits are refused, so counts must be formatted.

    ``1362`` breaks the screen and ``1,362`` does not. This is the one place a
    formatting convention is load-bearing, so it gets a test rather than a
    comment.
    """
    result = belt.call("query_sql", {"sql": "SELECT COUNT(*) n FROM agent_events"})
    total = result.result["rows"][0]["n"]
    assert total >= 1000, "this test needs a batch big enough to have the problem"
    assert T.fmt_count(total) in result.rendered
    with pytest.raises(PromptCanonicalityError):
        assert_no_identifiers(str(total))


def test_rendering_helpers_keep_digit_runs_short():
    for value in (0, 7, 999, 1000, 12_345, 1_234_567):
        assert_no_identifiers(T.fmt_count(value))
    for value in (0.0, 0.0001, 0.4371, 0.999, 1.0):
        assert_no_identifiers(T.fmt_rate(value))
        assert_no_identifiers(T.fmt_pp(value))
        assert_no_identifiers(T.fmt_ratio(value))


def test_a_truncated_table_says_so(belt):
    """The model must not mistake the first thirty rows for the population.

    Silent truncation is how an agent ends up asserting something about a
    population it never saw, with a perfectly valid receipt.
    """
    result = belt.call(
        "query_sql", {"sql": "SELECT day_index, segment, cause_signal FROM agent_events"}
    )
    assert result.row_count > T.RENDER_ROW_CAP
    assert "rows shown" in result.rendered


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def test_hash_is_sha256_over_canonical_json():
    """Key order must not change the digest.

    Without canonical JSON the hash would be a function of Python's dict
    iteration order, and a receipt would stop verifying in a later process for no
    reason at all -- which would look exactly like tampering.
    """
    import hashlib

    from pramaan import canonical

    a = {"b": 1, "a": [2, 3]}
    b = {"a": [2, 3], "b": 1}
    assert T.hash_of(a) == T.hash_of(b)
    expected = hashlib.sha256(canonical.canonical_json(a).encode()).hexdigest()
    assert T.hash_of(a) == "sha256:" + expected
    assert T.hash_of({"b": 1, "a": [3, 2]}) != T.hash_of(a)


def test_identical_calls_hash_identically_and_get_distinct_call_ids(belt):
    first = belt.call("get_downtime", {"window": [3, 4]})
    second = belt.call("get_downtime", {"window": [3, 4]})
    assert first.result_hash == second.result_hash
    assert first.args_hash == second.args_hash
    assert first.call_id != second.call_id


def test_call_ids_are_sequential_and_prompt_safe(belt):
    """A hash-derived id would break the canonicality screen intermittently.

    An 8-character hex id contains four consecutive digits often enough to matter,
    and ``assert_no_identifiers`` would then refuse the transcript on a schedule
    set by the hash. Sequential ids are deterministic and always safe.
    """
    for _ in range(12):
        belt.call("get_merchant_config", {})
    assert belt.call_ids[:3] == ["tc_01", "tc_02", "tc_03"]
    for call_id in belt.call_ids:
        assert_no_identifiers(call_id)


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------


def test_row_cap_is_enforced_and_flagged(world):
    events, traffic, downtime, _t = world
    belt = T.ToolBelt(T.build_agent_db(events, traffic, downtime))
    original = T.ROW_CAP
    try:
        T.ROW_CAP = 5
        result = belt.call("query_sql", {"sql": "SELECT day_index FROM agent_events"})
        assert result.row_count == 5
        assert result.result["truncated"] is True
        assert "TRUNCATED" in result.rendered
    finally:
        T.ROW_CAP = original


def test_a_slow_query_is_aborted_rather_than_hanging(world):
    """The 5s ceiling, tested by shrinking it rather than by writing a slow query.

    A genuinely slow query on 1,362 rows would have to be a large cross join, and
    a test that takes five seconds to prove a five-second timeout is a test people
    delete. The clock is injected, so the deadline can be made immediate.
    """
    events, traffic, downtime, _t = world

    calls = {"n": 0}

    def clock():
        # Strictly increasing, in thousand-second jumps, so the deadline is
        # already in the past by the time the progress handler first fires.
        #
        # Two details matter here and both cost a debugging round. ToolBelt.call
        # reads the clock once for its own elapsed-time accounting *before*
        # query_sql reads it to set the deadline, so a clock that returns a large
        # value on its second read puts the deadline ahead of every later read
        # and nothing ever aborts. And it must be an unbounded callable rather
        # than a finite iterator: the progress handler fires an unpredictable
        # number of times, and a StopIteration raised inside it surfaces as an
        # unrelated RuntimeError.
        calls["n"] += 1
        return calls["n"] * 1_000.0

    belt = T.ToolBelt(T.build_agent_db(events, traffic, downtime), clock=clock)
    result = belt.call(
        "query_sql",
        {"sql": "SELECT COUNT(*) FROM agent_events a, agent_events b, agent_events c"},
    )
    assert result.ok is False
    assert "limit" in result.result["error"].lower()


def test_a_failed_call_is_still_logged_hashed_and_citable(belt):
    """An error must not vanish from the log.

    A claim resting on a query that errored would otherwise have no receipt to
    strip, and would look unbacked for the wrong reason -- which corrupts the
    receipt-coverage metric in the flattering direction.
    """
    result = belt.call("query_sql", {"sql": "SELECT nonexistent FROM agent_events"})
    assert result.ok is False
    assert belt.get(result.call_id) is result
    assert result.result_hash == T.hash_of(result.result)


# --------------------------------------------------------------------------
# compare_baseline: the segment's own history, never the fleet
# --------------------------------------------------------------------------


def test_baseline_is_the_slices_own_history(belt):
    """tier3 fails at roughly twice metro's rate and is not flagged for it.

    PRD 6.2's reason for the tool: compared to the fleet, a structurally weak but
    stable slice alarms every day, which is the same as never alarming. This is
    that property as a number -- tier3's own deviation is small even though its
    level is the highest on the platform.
    """
    result = belt.call("compare_baseline", {"window": [3, 4]})
    rows = {r["key"]: r for r in result.result["rows"]}
    assert rows["tier3"]["baseline_rate"] > 2 * rows["metro"]["baseline_rate"] * 0.9
    # Its own deviation is modest: nothing broke in tier3.
    assert abs(rows["tier3"]["delta"]) < 0.05
    # tier2 is the slice that actually moved.
    assert rows["tier2"]["delta"] > 0.08


def test_baseline_is_trailing_not_surrounding(belt):
    """Days after the window must never enter the baseline.

    If the elevation has not resolved, a surrounding baseline absorbs it and the
    tool reports no change -- a false negative on a live incident, which is the
    worst direction for this particular error.
    """
    result = belt.call("compare_baseline", {"window": [3, 4]})
    baseline = result.result["baseline_days"]
    assert max(baseline) < 3
    assert baseline == [0, 1, 2]


def test_compare_baseline_refuses_a_window_with_no_history(belt):
    result = belt.call("compare_baseline", {"window": [0]})
    assert result.ok is False
    assert "baseline" in result.result["error"].lower()


def test_share_dimensions_are_labelled_as_shares_not_rates(belt):
    """Only ``segment`` has a denominator, and the output must say so.

    A share-of-failures table and a failure-rate table look identical on screen
    and mean entirely different things. Labelling is the cheap defence; refusing
    to decompose a share is the expensive one, and both are in place.
    """
    result = belt.call("compare_baseline", {"window": [3, 4], "dimension": "reason_class"})
    assert result.ok is True
    assert "share of failures" in result.result["measure"]
    assert "no attempt denominator" in result.rendered


# --------------------------------------------------------------------------
# decompose: the arithmetic, checked independently
# --------------------------------------------------------------------------


def _independent_shift_share(conn, window, baseline):
    """Shift-share computed here, from raw SQL, with no help from the tool.

    ADR-023 forbids comparing a function to its own delegate, so this does the
    three-term arithmetic inline. It is eight lines; the alternative is a test
    that asserts the tool agrees with itself.
    """

    def slices(days):
        rows = conn.execute(
            "SELECT segment, SUM(attempts) a, SUM(failures) f FROM agent_traffic "
            "WHERE day_index IN (%s) GROUP BY segment" % ",".join("?" * len(days)),
            tuple(days),
        ).fetchall()
        total = sum(int(r[1]) for r in rows)
        return {r[0]: (int(r[1]) / total, int(r[2]) / int(r[1])) for r in rows}

    base, now = slices(baseline), slices(window)
    keys = sorted(set(base) | set(now))
    rate = sum(base[k][0] * (now[k][1] - base[k][1]) for k in keys)
    mix = sum((now[k][0] - base[k][0]) * base[k][1] for k in keys)
    inter = sum((now[k][0] - base[k][0]) * (now[k][1] - base[k][1]) for k in keys)
    p0 = sum(base[k][0] * base[k][1] for k in keys)
    pt = sum(now[k][0] * now[k][1] for k in keys)
    return rate, mix, inter, p0, pt


def test_decompose_matches_independently_computed_shift_share(belt):
    result = belt.call("decompose", {"window": [3, 4], "dimension": "segment"})
    assert result.ok is True
    rate, mix, inter, p0, pt = _independent_shift_share(belt.conn, [3, 4], [0, 1, 2])
    payload = result.result
    assert payload["rate_effect"] == pytest.approx(rate, abs=1e-12)
    assert payload["mix_effect"] == pytest.approx(mix, abs=1e-12)
    assert payload["interaction"] == pytest.approx(inter, abs=1e-12)
    assert payload["blended_rate_baseline"] == pytest.approx(p0, abs=1e-12)
    assert payload["blended_rate_window"] == pytest.approx(pt, abs=1e-12)


def test_the_three_terms_sum_to_the_observed_change_exactly(belt):
    """An algebraic identity, not an approximation. Asserted at 1e-12.

    A residual here would mean the decomposition is mis-attributing part of the
    change to nothing at all, and the mis-attribution would be invisible on a
    screen that prints one decimal place.
    """
    payload = belt.call("decompose", {"window": [3, 4]}).result
    total = payload["rate_effect"] + payload["mix_effect"] + payload["interaction"]
    assert total == pytest.approx(payload["observed_change"], abs=1e-12)


def test_decompose_refuses_dimensions_without_a_denominator(belt):
    for dimension in ("reason_class", "amount_band", "hour_bucket"):
        result = belt.call("decompose", {"window": [3, 4], "dimension": dimension})
        assert result.ok is False
        assert "denominator" in result.result["error"]


# --------------------------------------------------------------------------
# The remaining tools
# --------------------------------------------------------------------------


def test_no_declared_downtime_is_reported_as_a_finding(belt):
    """An empty result must not read as missing data.

    It is the claim that separates "the issuer broke" from "traffic moved", and a
    bare ``(no rows)`` invites the model to treat the tool as broken and ignore
    it.
    """
    result = belt.call("get_downtime", {"window": [3, 4]})
    assert result.ok is True
    assert result.result["count"] == 0
    assert "finding, not missing data" in result.rendered


def test_declared_downtime_is_returned_when_the_spec_declares_one(world):
    from dataclasses import replace as dc_replace

    events, traffic, _downtime, _t = world
    spec = dc_replace(DEV_INCIDENT, declares_downtime=True)
    belt = T.ToolBelt(T.build_agent_db(events, traffic, build_downtime(spec)))
    inside = belt.call("get_downtime", {"window": [3, 4]})
    outside = belt.call("get_downtime", {"window": [7]})
    assert inside.result["count"] == 1
    assert outside.result["count"] == 0, "a downtime must not match a window it misses"


def test_reason_taxonomy_defaults_to_the_observed_codes(belt):
    """The full 69-code table is most of a thousand tokens and mostly irrelevant."""
    observed = belt.call("get_reason_taxonomy", {})
    everything = belt.call("get_reason_taxonomy", {"observed_only": False})
    assert 0 < observed.row_count < everything.row_count
    assert everything.row_count == 69
    for row in observed.result["codes"]:
        assert row["observed_n"] > 0


def test_merchant_config_is_returned_and_is_healthy(belt):
    result = belt.call("get_merchant_config", {})
    assert result.ok is True
    assert "upi" in result.result["config"]["enabled_methods"]


def test_unknown_tool_and_bad_arguments_are_errors_not_exceptions(belt):
    assert belt.call("no_such_tool", {}).ok is False
    assert belt.call("decompose", {"nonsense": 1}).ok is False
    assert belt.call("query_sql", {}).ok is False
    assert belt.call("query_sql", {"sql": ""}).ok is False


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_two_belts_over_the_same_world_produce_identical_hashes(world):
    events, traffic, downtime, _t = world
    calls = [
        ("decompose", {"window": [3, 4]}),
        ("compare_baseline", {"window": [3, 4]}),
        ("query_sql", {"sql": "SELECT segment, COUNT(*) n FROM agent_events GROUP BY 1"}),
    ]
    hashes = []
    for _ in range(2):
        belt = T.ToolBelt(T.build_agent_db(events, traffic, downtime))
        hashes.append([belt.call(t, a).result_hash for t, a in calls])
    assert hashes[0] == hashes[1]


def test_there_is_no_unauthorized_caller_outside_tests():
    """``authorize=False`` is a test affordance and must stay one.

    It exists so a test can read ``sqlite_master``. If production code ever
    passes it, the agent's SQL runs against a connection with no authorizer and
    the read-only guarantee becomes a keyword denylist again.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    definition_site = root / "pramaan" / "investigate" / "tools.py"
    call_site = re.compile(r"build_agent_db\([^)]*authorize\s*=\s*False", re.S)
    offenders = []
    for path in (root / "pramaan").rglob("*.py"):
        if path == definition_site:
            # Where the parameter is declared and documented. Excluded by exact
            # path rather than by name, so a second module cannot hide behind the
            # same basename.
            continue
        if call_site.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], offenders
