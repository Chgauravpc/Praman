"""The read-only tool belt. Pure functions over a projection of the event store.

PRD 6.2, in the priority order BUILD-PLAN Day 4 sets: ``query_sql``,
``compare_baseline``, ``get_downtime`` carry the day; ``decompose``,
``get_merchant_config`` and ``get_reason_taxonomy`` follow. ``run_canary`` is
absent, and its absence is the point -- there is no write tool here, so no
sequence of model outputs can move money from inside a diagnosis.

Four design decisions in this module are load-bearing enough to state up front.

**1. The agent queries a separate database, not the event store.**

``build_agent_db`` copies a *projection* of the events into a fresh in-memory
SQLite connection: ``agent_events``, ``agent_traffic``, ``agent_downtime``. The
real store is never attached. So the ``latent`` table -- ``has_intent``,
``capability_clears_at``, ``self_recovers_at`` -- is not merely un-joined, it is
not present in the database the agent's SQL executes against. An investigator
that could read ``has_intent`` would know which customers will respond before
contacting any of them, which is not a diagnosis, it is the answer key with extra
steps (ADR-010, and STATE.md's Day 4 carry-in note 3). A denylist of table names
would have been the cheap version and it would have been one regex away from
failing open.

**2. The projection carries no high-cardinality column, so tool output is safe
in a prompt by construction.**

This is the resolution of a real collision. ``llm.call`` screens every prompt
with ``prompts.assert_no_identifiers``, which refuses raw timestamps, exact
amounts, ``cp_``/``evt_`` identifiers, hex digests and any run of four or more
digits. A multi-turn investigator has to feed tool results back into the prompt,
so the naive implementation puts raw rows in front of the screen and the build
breaks on the first turn.

The wrong fix is to relax the screen for evidence. PRD 9.1 is explicit that a
weakened canonicality guarantee is the failure mode no test catches: the system
keeps working, every call becomes a cache miss, and the token budget goes from
~800K to ~15M silently. So the screen is untouched -- byte for byte the same
function, at the same chokepoint -- and the *data* is made canonical instead:

- no ``event_id``, ``external_ref`` or ``counterparty_id`` column exists;
- time is ``day_index`` (an integer offset from ``SIM_EPOCH``) plus
  ``hour_of_day`` and ``hour_bucket``, never a date string;
- money is ``amount_band`` and its label, never paise;
- every rendered figure goes through ``fmt_*`` below, which keeps digit runs
  under four by using thousands separators and three-decimal rates.

``tests/test_investigate_tools.py`` asserts that every tool's rendered output
passes the unmodified ``assert_no_identifiers``. That test is the real guarantee;
this docstring is a description of it.

**3. Read-only is enforced by SQLite's authorizer, not by a keyword denylist.**

``set_authorizer`` refuses every action except SELECT, READ and FUNCTION, and
refuses READ on any table outside the projection. A regex looking for ``DROP``
is a thing to be evaded; an authorizer is a thing to be obeyed. The keyword
screen is still there in front of it, because a clear error message beats an
opaque ``not authorized``, but it is the second line of defence rather than the
only one.

**4. Every result is hashed with SHA-256, and the hash is over canonical JSON.**

``canonical.sha256_hex`` over ``canonical_json`` -- sorted keys, fixed
separators -- so the same result hashes identically on any machine, which is what
makes a receipt checkable in a later process. BUILD-PLAN Day 4 anticipated a
suggestion to use string comparison instead and declined it: the hash is one
line, it is not the expensive part, and downgrading it removes the
tamper-evidence property that makes a receipt a receipt.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pramaan import canonical
from pramaan.llm.prompts import assert_no_identifiers
from pramaan.taxonomy import (
    BY_CODE,
    REASON_CLASS_POLICY,
    contact_verdict,
    is_retry_eligible,
    reason_class_of,
)

# --------------------------------------------------------------------------
# Limits (BUILD-PLAN Day 4, block A)
# --------------------------------------------------------------------------

#: Wall-clock ceiling for one query. Enforced through a progress handler rather
#: than a signal, because signals are not portable to Windows and this project
#: runs there.
SQL_TIMEOUT_SECONDS = 5.0

#: Row ceiling. A query returning more than this is answered with the first
#: ROW_CAP rows and an explicit truncation flag -- never silently clipped, since
#: a claim resting on a truncated result set is exactly the kind of quiet wrong
#: answer this project spent Day 3 learning to distrust.
ROW_CAP = 10_000

#: Rows actually rendered into the prompt. Distinct from ROW_CAP on purpose:
#: the tool may legitimately aggregate over ten thousand rows, but a prompt
#: carrying more than a couple of dozen is both a token sink and an invitation
#: to the model to quote a row rather than reason about the aggregate.
RENDER_ROW_CAP = 30

#: The only tables the agent's SQL may touch.
AGENT_TABLES: Tuple[str, ...] = ("agent_events", "agent_traffic", "agent_downtime")

#: Statement prefixes permitted. Anything else is refused before SQLite sees it.
ALLOWED_PREFIXES: Tuple[str, ...] = ("select", "with")

#: Refused outright, with a readable message. The authorizer would refuse most of
#: these anyway; a named error is kinder to a model that can still recover.
FORBIDDEN_KEYWORDS: Tuple[str, ...] = (
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "attach", "detach", "pragma", "vacuum", "reindex", "trigger", "truncate",
    "grant", "revoke",
)


class ToolError(RuntimeError):
    """A tool refused. Returned to the model as an observation, never raised at it.

    The distinction matters for the loop: a refused query is information the
    agent can act on ("I may not read that table"), and killing the session over
    it would waste the turns already spent. So ``ToolBelt.call`` catches this and
    logs an error result -- which is still hashed, still gets a call_id, and can
    still be cited, so a claim resting on a *failed* call is auditable rather
    than invisible.
    """


# --------------------------------------------------------------------------
# Rendering -- canonical by construction
# --------------------------------------------------------------------------


def fmt_count(value: int) -> str:
    """An integer with thousands separators.

    The separators are not decoration. ``assert_no_identifiers`` refuses any run
    of four or more digits, because that is how an exact amount or an identifier
    gets into a prompt. ``1,362`` has runs of one and three; ``1362`` does not
    and would break the build. So this is the one correct way to put a count in
    front of the model, and the tests check that no tool bypasses it.
    """
    return format(int(value), ",")


def fmt_rate(value: float) -> str:
    """A rate as a percentage to one decimal place: ``43.7%``."""
    return "%.1f%%" % (100.0 * float(value))


def fmt_pp(value: float) -> str:
    """A difference of rates, in percentage points, always signed: ``+3.8pp``."""
    return "%+.1fpp" % (100.0 * float(value))


def fmt_ratio(value: float) -> str:
    """A bare ratio to three decimals. Three, not four: a fourth would give
    ``0.4370`` a four-digit run and the screen would refuse it."""
    return "%.3f" % float(value)


def _render_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return fmt_count(value)
    if isinstance(value, float):
        return fmt_ratio(value)
    return str(value)


def render_table(rows: Sequence[Dict[str, Any]], columns: Optional[Sequence[str]] = None) -> str:
    """A compact fixed-width table, safe for a prompt.

    Truncation is stated in the output rather than implied by a short table, so
    the model cannot mistake the first thirty rows for all of them and assert
    something about a population it never saw.
    """
    if not rows:
        return "(no rows)"
    cols = list(columns) if columns else list(rows[0].keys())
    shown = rows[:RENDER_ROW_CAP]
    rendered = [[_render_value(r.get(c)) for c in cols] for r in shown]
    widths = [
        max(len(cols[i]), max((len(r[i]) for r in rendered), default=0))
        for i in range(len(cols))
    ]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))]
    lines.append("  ".join("-" * widths[i] for i in range(len(cols))))
    for r in rendered:
        lines.append("  ".join(r[i].ljust(widths[i]) for i in range(len(cols))))
    if len(rows) > RENDER_ROW_CAP:
        lines.append(
            "... %s of %s rows shown. Aggregate in SQL rather than reasoning "
            "about the visible rows." % (fmt_count(RENDER_ROW_CAP), fmt_count(len(rows)))
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------

AGENT_SCHEMA = """
-- The agent's whole world. Every column is low-cardinality or banded, so a row
-- of this table is safe in prompt bytes; see the module docstring.
CREATE TABLE agent_events (
    day_index            INTEGER NOT NULL,   -- whole days since the epoch, IST
    hour_of_day          INTEGER NOT NULL,   -- 0-23, IST
    hour_bucket          TEXT NOT NULL,      -- business | evening_peak | night
    weekday              INTEGER NOT NULL,   -- 0 = Monday
    source_type          TEXT NOT NULL,
    cause_signal         TEXT NOT NULL,      -- the raw Razorpay reason code
    reason_class         TEXT NOT NULL,
    segment              TEXT NOT NULL,      -- metro | tier2 | tier3
    counterparty_kind    TEXT NOT NULL,
    legal_context        TEXT NOT NULL,
    decay_profile        TEXT NOT NULL,
    amount_band          INTEGER NOT NULL,   -- 1-5
    amount_band_label    TEXT NOT NULL,
    afa_exempt           INTEGER NOT NULL,   -- band <= 3, i.e. at or under 15k
    channel_eligibility  TEXT NOT NULL,
    retry_eligible       INTEGER NOT NULL,
    contact_verdict      TEXT NOT NULL,      -- allowed | waste | prohibited
    arm                  TEXT NOT NULL
);
CREATE INDEX idx_ae_day ON agent_events (day_index);
CREATE INDEX idx_ae_seg ON agent_events (segment, day_index);
CREATE INDEX idx_ae_code ON agent_events (cause_signal);

-- The denominator. Without this, a failure *rate* is not computable and every
-- comparison degenerates into comparing counts, which move with traffic.
CREATE TABLE agent_traffic (
    day_index  INTEGER NOT NULL,
    segment    TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    failures   INTEGER NOT NULL
);
CREATE INDEX idx_at_day ON agent_traffic (day_index, segment);

-- What the platform itself declared. Often empty, and empty is a finding.
CREATE TABLE agent_downtime (
    day_index_start INTEGER NOT NULL,
    hour_start      INTEGER NOT NULL,
    day_index_end   INTEGER NOT NULL,
    hour_end        INTEGER NOT NULL,
    method          TEXT NOT NULL,
    severity        TEXT NOT NULL,
    scope           TEXT NOT NULL,
    entity          TEXT NOT NULL,
    status          TEXT NOT NULL
);
"""


def _authorizer(action: int, arg1: Optional[str], arg2: Optional[str],
                db_name: Optional[str], trigger: Optional[str]) -> int:
    """Allow SELECT, READ on the projection, and function calls. Deny the rest.

    Action codes are SQLite's own. ``SQLITE_SELECT`` (21) authorises the
    statement as a whole; ``SQLITE_READ`` (20) authorises one column of one
    table and is where the table allowlist is applied; ``SQLITE_FUNCTION`` (31)
    covers ``COUNT``, ``SUM`` and the rest.

    Returning ``SQLITE_DENY`` rather than ``SQLITE_IGNORE`` matters: IGNORE would
    substitute NULL for a forbidden column and hand the agent a query that
    *succeeded* with silently blanked data. A denial the agent can see beats a
    result it cannot trust.
    """
    SQLITE_OK, SQLITE_DENY = 0, 1
    SQLITE_SELECT, SQLITE_READ, SQLITE_FUNCTION = 21, 20, 31
    if action == SQLITE_SELECT or action == SQLITE_FUNCTION:
        return SQLITE_OK
    if action == SQLITE_READ:
        return SQLITE_OK if arg1 in AGENT_TABLES else SQLITE_DENY
    return SQLITE_DENY


def build_agent_db(
    events: Iterable[Any],
    traffic: Sequence[Any] = (),
    downtime: Sequence[Any] = (),
    *,
    authorize: bool = True,
) -> sqlite3.Connection:
    """Project events into a fresh in-memory database the agent may query.

    Accepts ``RiskEvent`` objects and derives every projected column from them,
    so there is exactly one definition of what the agent can see. Note what is
    *not* derivable here: nothing in this function can reach ``event.latent``,
    because no projected column is a function of it.

    The authorizer is installed **after** the schema is created and the rows are
    inserted, for the obvious reason that the inserts would otherwise be denied.
    That ordering is the one thing to be careful about if this function is ever
    edited.

    ``authorize=False`` returns the connection without it. That exists for schema
    introspection in tests and nothing else -- the authorizer refuses reads of
    ``sqlite_master`` and refuses ``PRAGMA table_info``, which is correct
    behaviour and also makes "assert this database contains no latent table"
    impossible to write against an authorized connection. No production caller
    passes it, and ``test_there_is_no_unauthorized_caller`` checks that.
    """
    from sim.incident import day_index as _day_index, hour_of_day as _hour, weekday as _weekday

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(AGENT_SCHEMA)

    rows = []
    for event in events:
        code = event.cause_signal
        reason_class = reason_class_of(code)
        band = event.amount_band
        rows.append(
            (
                _day_index(event.detected_at),
                _hour(event.detected_at),
                event.hour_bucket,
                _weekday(event.detected_at),
                event.source_type,
                code,
                reason_class,
                event.counterparty.segment,
                event.counterparty.kind,
                event.legal_context,
                event.decay_profile,
                band,
                canonical.AMOUNT_BAND_LABELS[band],
                1 if band in canonical.AFA_EXEMPT_BANDS else 0,
                event.channel_eligibility,
                1 if is_retry_eligible(code) else 0,
                contact_verdict(code),
                event.arm,
            )
        )
    conn.executemany(
        "INSERT INTO agent_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    conn.executemany(
        "INSERT INTO agent_traffic VALUES (?,?,?,?)",
        [(t.day_index, t.segment, t.attempts, t.failures) for t in traffic],
    )
    conn.executemany(
        "INSERT INTO agent_downtime VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                d.day_index_start, d.hour_start, d.day_index_end, d.hour_end,
                d.method, d.severity, d.scope, d.entity, d.status,
            )
            for d in downtime
        ],
    )
    conn.commit()
    if authorize:
        conn.set_authorizer(_authorizer)
    return conn


# --------------------------------------------------------------------------
# Results and the session log
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolResult:
    """One tool call, its arguments, its result, and the hashes that bind them.

    ``call_id`` is sequential (``tc_01``, ``tc_02``) rather than a hash prefix,
    and that is a deliberate correction rather than laziness. A random hex id
    lands a four-or-more digit run in the prompt roughly a third of the time,
    which ``assert_no_identifiers`` correctly refuses -- so a hash-derived id
    would make the investigator fail intermittently, on a schedule determined by
    the hash. Sequential ids are deterministic, cheap for the model to cite
    accurately, and trivially auditable by eye.

    The ids being guessable is not a weakness, because guessing one buys nothing:
    the auditor's second check binds the receipt to the *bytes* of the result, so
    citing a real call_id for an unrelated claim is caught by the numeric
    consistency check rather than by the id being secret.
    """

    call_id: str
    tool: str
    args: Dict[str, Any]
    args_hash: str
    result: Dict[str, Any]
    result_hash: str
    row_count: int
    ok: bool
    rendered: str
    elapsed_ms: int

    def as_ledger_payload(self) -> Dict[str, Any]:
        """What the ledger records. The result itself is referenced by hash, not
        embedded: a ledger row is an audit record, not a data warehouse, and the
        session log holds the payload."""
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "args": self.args,
            "args_hash": self.args_hash,
            "result_hash": self.result_hash,
            "row_count": self.row_count,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
        }


def hash_of(payload: Any) -> str:
    """SHA-256 over canonical JSON, prefixed so a reader knows what it is.

    Both halves matter. ``canonical_json`` sorts keys and fixes separators, so
    two structurally identical results hash identically regardless of dict
    insertion order -- without it the hash would be a function of Python's
    iteration order and a receipt would stop verifying in a later process for no
    reason. The ``sha256:`` prefix is what the PRD's example carries and it makes
    a truncated or substituted digest obvious at a glance.
    """
    return "sha256:" + canonical.sha256_hex(payload)


class ToolBelt:
    """The tool belt for one investigation session, and its call log.

    The log is the auditor's source of truth. Nothing else in the system records
    what a tool returned, so a receipt citing ``tc_03`` is checkable exactly as
    long as this object is the one that produced it -- which is why
    ``receipts.audit`` takes the belt as an argument rather than reaching for a
    global.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        merchant_config: Optional[Dict[str, Any]] = None,
        clock=time.monotonic,
    ) -> None:
        self.conn = conn
        self._clock = clock
        self._log: Dict[str, ToolResult] = {}
        self._order: List[str] = []
        self.merchant_config = merchant_config or DEFAULT_MERCHANT_CONFIG

    # -- log ------------------------------------------------------------

    @property
    def log(self) -> Dict[str, ToolResult]:
        return dict(self._log)

    @property
    def call_ids(self) -> List[str]:
        return list(self._order)

    def get(self, call_id: str) -> Optional[ToolResult]:
        return self._log.get(call_id)

    def _next_call_id(self) -> str:
        return "tc_%02d" % (len(self._order) + 1)

    def _record(
        self,
        tool: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        *,
        row_count: int,
        ok: bool,
        rendered: str,
        elapsed_ms: int,
    ) -> ToolResult:
        entry = ToolResult(
            call_id=self._next_call_id(),
            tool=tool,
            args=args,
            args_hash=hash_of(args),
            result=result,
            result_hash=hash_of(result),
            row_count=row_count,
            ok=ok,
            rendered=assert_no_identifiers(rendered, context="tool result (%s)" % tool),
            elapsed_ms=elapsed_ms,
        )
        self._log[entry.call_id] = entry
        self._order.append(entry.call_id)
        return entry

    # -- dispatch -------------------------------------------------------

    #: Priority order from BUILD-PLAN Day 4. The first three carry the day.
    TOOLS: Tuple[str, ...] = (
        "query_sql",
        "compare_baseline",
        "get_downtime",
        "decompose",
        "get_merchant_config",
        "get_reason_taxonomy",
    )

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> ToolResult:
        """Run one tool. A refusal is logged as a result, never raised.

        The model gets a hashed, citable record even for a failed call. That is
        not politeness: without it, a claim resting on a query that errored would
        have no receipt to strip and would look unbacked for the wrong reason.
        """
        args = dict(args or {})
        started = self._clock()
        try:
            if tool not in self.TOOLS:
                raise ToolError(
                    "no such tool %r. Available: %s" % (tool, ", ".join(self.TOOLS))
                )
            handler = getattr(self, "_t_" + tool)
            result, rendered, row_count = handler(**args)
            ok = True
        except ToolError as exc:
            result = {"error": str(exc)}
            rendered = "ERROR: %s" % exc
            row_count = 0
            ok = False
        except TypeError as exc:
            # A wrong argument name. Common, recoverable, and worth naming
            # precisely rather than reporting as a generic failure.
            result = {"error": "bad arguments for %s: %s" % (tool, exc)}
            rendered = "ERROR: bad arguments for %s: %s" % (tool, exc)
            row_count = 0
            ok = False
        elapsed_ms = int((self._clock() - started) * 1000)
        return self._record(
            tool, args, result, row_count=row_count, ok=ok,
            rendered=rendered, elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # 1. query_sql -- where open-world discovery happens
    # ------------------------------------------------------------------

    def _t_query_sql(self, sql: str) -> Tuple[Dict[str, Any], str, int]:
        """Run one read-only SELECT against the projection.

        PRD 6.1's argument for an agent over a formula lives entirely in this
        method: the causes that a formula cannot find -- amounts clustering just
        above the AFA threshold, a method silently disabled, one segment's
        friction rising -- are all reachable by a query nobody wrote in advance.
        """
        if not isinstance(sql, str) or not sql.strip():
            raise ToolError("sql must be a non-empty string")
        cleaned = sql.strip().rstrip(";").strip()
        lowered = cleaned.lower()

        if not lowered.startswith(ALLOWED_PREFIXES):
            raise ToolError(
                "only SELECT and WITH statements are permitted; this belt has no "
                "write tool of any kind"
            )
        if ";" in cleaned:
            raise ToolError("one statement per call; a ';' separates two")
        for keyword in FORBIDDEN_KEYWORDS:
            if _has_word(lowered, keyword):
                raise ToolError(
                    "%r is a schema or write operation and is refused. The belt is "
                    "read-only." % keyword
                )

        deadline = self._clock() + SQL_TIMEOUT_SECONDS

        def _guard() -> int:
            # Non-zero aborts the statement. This is the portable timeout: a
            # signal-based one does not exist on Windows, and this project runs
            # there.
            return 1 if self._clock() > deadline else 0

        self.conn.set_progress_handler(_guard, 2000)
        try:
            cursor = self.conn.execute(cleaned)
            fetched = cursor.fetchmany(ROW_CAP + 1)
            columns = [d[0] for d in (cursor.description or [])]
        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "interrupted" in message.lower():
                raise ToolError(
                    "query exceeded the %.0fs limit. Aggregate in SQL rather than "
                    "scanning rows." % SQL_TIMEOUT_SECONDS
                ) from exc
            raise ToolError("SQL error: %s" % message) from exc
        except sqlite3.DatabaseError as exc:
            raise ToolError(
                "refused: %s. Readable tables are %s."
                % (exc, ", ".join(AGENT_TABLES))
            ) from exc
        finally:
            self.conn.set_progress_handler(None, 0)

        truncated = len(fetched) > ROW_CAP
        rows = [dict(r) for r in fetched[:ROW_CAP]]
        result = {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
        rendered = "rows: %s%s\n%s" % (
            fmt_count(len(rows)),
            " (TRUNCATED at the row cap)" if truncated else "",
            render_table(rows, columns),
        )
        return result, rendered, len(rows)

    # ------------------------------------------------------------------
    # 2. compare_baseline -- against the segment's own history, never the fleet
    # ------------------------------------------------------------------

    def _t_compare_baseline(
        self,
        segment: Optional[str] = None,
        window: Optional[Sequence[int]] = None,
        baseline: Optional[Sequence[int]] = None,
        dimension: str = "segment",
    ) -> Tuple[Dict[str, Any], str, int]:
        """Failure rate in a window against that slice's own trailing baseline.

        The word "own" is the whole tool. PRD 6.2: never the fleet average,
        because a structurally-weak-but-stable segment would then alarm forever.
        tier3 fails at roughly twice metro's rate and always has; comparing tier3
        to the fleet flags it every single day, which is the same as never
        flagging anything.

        ``dimension`` allows the same comparison along ``reason_class`` or
        ``amount_band``. Only ``segment`` has a traffic denominator, so the other
        dimensions report a *share of failures* and say so in the output rather
        than quietly reporting a different quantity under the same heading.
        """
        window_days = _day_range(window, "window")
        baseline_days = (
            _day_range(baseline, "baseline")
            if baseline is not None
            else _trailing(window_days, self._max_day())
        )
        if not baseline_days:
            raise ToolError(
                "no trailing baseline exists before day %d. A comparison needs "
                "history; widen the window or pick a later one." % min(window_days)
            )
        if dimension not in ("segment", "reason_class", "amount_band"):
            raise ToolError(
                "dimension must be segment, reason_class or amount_band, got %r" % dimension
            )

        if dimension == "segment":
            rows = self._segment_rates(window_days, baseline_days, only=segment)
            measure = "failure rate (failures / attempts)"
        else:
            rows = self._share_rates(dimension, window_days, baseline_days, only=segment)
            measure = "share of failures (no attempt denominator for this dimension)"

        result = {
            "dimension": dimension,
            "measure": measure,
            "window_days": list(window_days),
            "baseline_days": list(baseline_days),
            "rows": rows,
        }
        header = "%s, window days %s vs its own trailing baseline days %s" % (
            measure,
            "-".join(str(d) for d in (min(window_days), max(window_days))),
            "-".join(str(d) for d in (min(baseline_days), max(baseline_days))),
        )
        rendered = header + "\n" + render_table(
            [
                {
                    dimension: r["key"],
                    "baseline": fmt_rate(r["baseline_rate"]),
                    "window": fmt_rate(r["window_rate"]),
                    "delta": fmt_pp(r["delta"]),
                    "window_n": r["window_denominator"],
                }
                for r in rows
            ]
        )
        return result, rendered, len(rows)

    # ------------------------------------------------------------------
    # 3. get_downtime -- corroborate against the platform's own declaration
    # ------------------------------------------------------------------

    def _t_get_downtime(
        self, window: Optional[Sequence[int]] = None
    ) -> Tuple[Dict[str, Any], str, int]:
        """Declared ``payment.downtime`` entities overlapping a window [A].

        An empty result is a finding, not an absence of data, and the rendered
        text says so in as many words. It is the claim that distinguishes "the
        issuer broke" from "traffic moved": a real rate shift with no declared
        downtime is either something the platform has not noticed, or not a
        platform problem at all -- and the second reading is the one a
        decomposition can settle.
        """
        window_days = _day_range(window, "window") if window is not None else None
        if window_days is None:
            rows = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM agent_downtime ORDER BY day_index_start, hour_start"
                )
            ]
        else:
            lo, hi = min(window_days), max(window_days)
            rows = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM agent_downtime WHERE day_index_end >= ? AND "
                    "day_index_start <= ? ORDER BY day_index_start, hour_start",
                    (lo, hi),
                )
            ]
        result = {
            "window_days": list(window_days) if window_days else None,
            "declared": rows,
            "count": len(rows),
        }
        if rows:
            rendered = "declared downtimes: %s\n%s" % (fmt_count(len(rows)), render_table(rows))
        else:
            rendered = (
                "declared downtimes: 0.\n"
                "NOTE: this is a finding, not missing data. The platform declared no "
                "downtime overlapping this window. An elevated failure rate here is "
                "therefore either unnoticed upstream, or not an upstream problem."
            )
        return result, rendered, len(rows)

    # ------------------------------------------------------------------
    # 4. decompose -- the shift-share arithmetic (PRD 6.3)
    # ------------------------------------------------------------------

    def _t_decompose(
        self,
        dimension: str = "segment",
        window: Optional[Sequence[int]] = None,
        baseline: Optional[Sequence[int]] = None,
    ) -> Tuple[Dict[str, Any], str, int]:
        """Split a change in blended failure rate into rate, mix and interaction.

        For ``p = sum_i w_i * p_i`` (share x rate per segment):

            dp = sum_i w_i0 * (p_it - p_i0)          RATE         something broke
               + sum_i (w_it - w_i0) * p_i0          MIX          nothing broke
               + sum_i (w_it - w_i0)(p_it - p_i0)    INTERACTION

        The three terms sum to ``dp`` exactly -- an algebraic identity, not an
        approximation -- and the tool asserts it. That assertion is the reason
        this is a tool rather than a paragraph in a prompt: the agent chooses
        *what to decompose along*, which is a judgement across dozens of
        candidate dimensions, and gets back arithmetic it cannot fudge.

        Only ``segment`` carries an attempt denominator, so only ``segment``
        yields a true failure-rate decomposition. The tool refuses the other
        dimensions rather than silently decomposing a share, because a
        share-of-failures decomposition looks identical on screen and means
        something entirely different.
        """
        if dimension != "segment":
            raise ToolError(
                "decompose supports dimension='segment' only on this dataset: it "
                "is the one dimension with an attempt denominator, and a "
                "decomposition of a share-of-failures would read identically "
                "while meaning something else. Use compare_baseline for %r."
                % dimension
            )
        window_days = _day_range(window, "window")
        baseline_days = (
            _day_range(baseline, "baseline")
            if baseline is not None
            else _trailing(window_days, self._max_day())
        )
        if not baseline_days:
            raise ToolError(
                "no trailing baseline exists before day %d" % min(window_days)
            )

        base = self._segment_weights_and_rates(baseline_days)
        now = self._segment_weights_and_rates(window_days)
        keys = sorted(set(base) | set(now))

        p0 = sum(base.get(k, (0.0, 0.0))[0] * base.get(k, (0.0, 0.0))[1] for k in keys)
        pt = sum(now.get(k, (0.0, 0.0))[0] * now.get(k, (0.0, 0.0))[1] for k in keys)

        rate_effect = mix_effect = interaction = 0.0
        per_key = []
        for k in keys:
            w0, r0 = base.get(k, (0.0, 0.0))
            wt, rt = now.get(k, (0.0, 0.0))
            rate_i = w0 * (rt - r0)
            mix_i = (wt - w0) * r0
            inter_i = (wt - w0) * (rt - r0)
            rate_effect += rate_i
            mix_effect += mix_i
            interaction += inter_i
            per_key.append(
                {
                    "key": k,
                    "share_baseline": w0, "share_window": wt,
                    "rate_baseline": r0, "rate_window": rt,
                    "rate_effect": rate_i, "mix_effect": mix_i, "interaction": inter_i,
                }
            )

        total = rate_effect + mix_effect + interaction
        # The identity, checked. A residual here is a bug in this method, not a
        # property of the data, and it would silently mis-attribute a shift.
        if abs(total - (pt - p0)) > 1e-9:
            raise ToolError(
                "decomposition failed its own identity check: terms sum to %.9f "
                "but the observed change is %.9f. This is a defect in the tool."
                % (total, pt - p0)
            )

        result = {
            "dimension": dimension,
            "window_days": list(window_days),
            "baseline_days": list(baseline_days),
            "blended_rate_baseline": p0,
            "blended_rate_window": pt,
            "observed_change": pt - p0,
            "rate_effect": rate_effect,
            "mix_effect": mix_effect,
            "interaction": interaction,
            "per_key": per_key,
        }
        rendered = "\n".join(
            [
                "blended failure rate: %s -> %s   change %s"
                % (fmt_rate(p0), fmt_rate(pt), fmt_pp(pt - p0)),
                "  RATE        %s   something broke in one or more slices" % fmt_pp(rate_effect),
                "  MIX         %s   nothing broke; traffic moved between slices" % fmt_pp(mix_effect),
                "  INTERACTION %s" % fmt_pp(interaction),
                "  (the three terms sum to the observed change exactly)",
                "",
                render_table(
                    [
                        {
                            dimension: r["key"],
                            "share": "%s -> %s" % (fmt_rate(r["share_baseline"]), fmt_rate(r["share_window"])),
                            "rate": "%s -> %s" % (fmt_rate(r["rate_baseline"]), fmt_rate(r["rate_window"])),
                            "rate_eff": fmt_pp(r["rate_effect"]),
                            "mix_eff": fmt_pp(r["mix_effect"]),
                        }
                        for r in per_key
                    ]
                ),
            ]
        )
        return result, rendered, len(per_key)

    # ------------------------------------------------------------------
    # 5. get_merchant_config
    # ------------------------------------------------------------------

    def _t_get_merchant_config(self) -> Tuple[Dict[str, Any], str, int]:
        """Enabled methods, networks and mandate settings.

        Catches the ``merchant_misconfigured`` class of cause: a method silently
        disabled, a network dropped, an AFA threshold set where it collides with
        the amount distribution. On this dataset nothing is misconfigured, and
        that is stated in the rendered output rather than left for the agent to
        infer -- an agent that reads a healthy config and reports a
        misconfiguration anyway is making a claim its receipt contradicts, which
        is precisely what the numeric consistency check exists to catch.
        """
        config = dict(self.merchant_config)
        rendered = "\n".join(
            "%s: %s" % (k, ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else _render_value(v))
            for k, v in sorted(config.items())
        )
        return {"config": config}, rendered, len(config)

    # ------------------------------------------------------------------
    # 6. get_reason_taxonomy
    # ------------------------------------------------------------------

    def _t_get_reason_taxonomy(
        self, reason_class: Optional[str] = None, observed_only: bool = True
    ) -> Tuple[Dict[str, Any], str, int]:
        """The reason-code table with its action classes.

        PRD 6.2's justification is grounding: without this the agent is guessing
        what ``upi_autopay_not_supported_on_psp`` means from its name. Defaults to
        the codes actually present in this dataset, because the full 69-code
        table is most of a thousand tokens and the majority of it is irrelevant
        to any one incident.
        """
        observed = {
            r["cause_signal"]: int(r["n"])
            for r in self.conn.execute(
                "SELECT cause_signal, COUNT(*) AS n FROM agent_events "
                "GROUP BY cause_signal ORDER BY n DESC, cause_signal"
            )
        }
        codes = []
        for code, meta in sorted(BY_CODE.items()):
            if observed_only and code not in observed:
                continue
            if reason_class is not None and meta.reason_class != reason_class:
                continue
            policy = REASON_CLASS_POLICY[meta.reason_class]
            codes.append(
                {
                    "code": code,
                    "reason_class": meta.reason_class,
                    "retry": policy.retry_mode,
                    "contact": policy.contact,
                    "observed_n": observed.get(code, 0),
                }
            )
        result = {
            "codes": codes,
            "observed_only": observed_only,
            "reason_class_filter": reason_class,
        }
        rendered = "%s codes%s\n%s" % (
            fmt_count(len(codes)),
            " present in this dataset" if observed_only else " in the full taxonomy",
            render_table(codes),
        )
        return result, rendered, len(codes)

    # ------------------------------------------------------------------
    # shared query helpers
    # ------------------------------------------------------------------

    def _max_day(self) -> int:
        row = self.conn.execute("SELECT MAX(day_index) FROM agent_events").fetchone()
        return int(row[0] or 0)

    def _segment_weights_and_rates(self, days: Sequence[int]) -> Dict[str, Tuple[float, float]]:
        """Per-segment (share of attempts, failure rate) over a set of days."""
        placeholders = ",".join("?" * len(days))
        rows = self.conn.execute(
            "SELECT segment, SUM(attempts) AS a, SUM(failures) AS f FROM agent_traffic "
            "WHERE day_index IN (%s) GROUP BY segment ORDER BY segment" % placeholders,
            tuple(days),
        ).fetchall()
        total = sum(int(r["a"]) for r in rows) or 1
        return {
            r["segment"]: (int(r["a"]) / total, (int(r["f"]) / int(r["a"])) if int(r["a"]) else 0.0)
            for r in rows
        }

    def _segment_rates(
        self, window_days: Sequence[int], baseline_days: Sequence[int], only: Optional[str]
    ) -> List[Dict[str, Any]]:
        base = self._segment_totals(baseline_days)
        now = self._segment_totals(window_days)
        keys = sorted(set(base) | set(now))
        if only is not None:
            if only not in keys:
                raise ToolError(
                    "segment %r is not present. Known: %s" % (only, ", ".join(keys))
                )
            keys = [only]
        out = []
        for k in keys:
            a0, f0 = base.get(k, (0, 0))
            at, ft = now.get(k, (0, 0))
            r0 = f0 / a0 if a0 else 0.0
            rt = ft / at if at else 0.0
            out.append(
                {
                    "key": k,
                    "baseline_rate": r0,
                    "window_rate": rt,
                    "delta": rt - r0,
                    "baseline_denominator": a0,
                    "window_denominator": at,
                }
            )
        return out

    def _segment_totals(self, days: Sequence[int]) -> Dict[str, Tuple[int, int]]:
        placeholders = ",".join("?" * len(days))
        rows = self.conn.execute(
            "SELECT segment, SUM(attempts) AS a, SUM(failures) AS f FROM agent_traffic "
            "WHERE day_index IN (%s) GROUP BY segment" % placeholders,
            tuple(days),
        ).fetchall()
        return {r["segment"]: (int(r["a"]), int(r["f"])) for r in rows}

    def _share_rates(
        self,
        dimension: str,
        window_days: Sequence[int],
        baseline_days: Sequence[int],
        only: Optional[str],
    ) -> List[Dict[str, Any]]:
        def shares(days: Sequence[int]) -> Dict[str, Tuple[float, int]]:
            placeholders = ",".join("?" * len(days))
            rows = self.conn.execute(
                "SELECT %s AS k, COUNT(*) AS n FROM agent_events WHERE day_index IN (%s) "
                "GROUP BY k" % (dimension, placeholders),
                tuple(days),
            ).fetchall()
            total = sum(int(r["n"]) for r in rows) or 1
            return {str(r["k"]): (int(r["n"]) / total, int(r["n"])) for r in rows}

        base, now = shares(baseline_days), shares(window_days)
        keys = sorted(set(base) | set(now))
        if only is not None:
            keys = [k for k in keys if k == str(only)] or keys
        out = []
        for k in keys:
            s0 = base.get(k, (0.0, 0))
            st = now.get(k, (0.0, 0))
            out.append(
                {
                    "key": k,
                    "baseline_rate": s0[0],
                    "window_rate": st[0],
                    "delta": st[0] - s0[0],
                    "baseline_denominator": s0[1],
                    "window_denominator": st[1],
                }
            )
        return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _has_word(text: str, word: str) -> bool:
    """Whole-word keyword search.

    Substring matching would refuse ``SELECT created_day FROM ...`` for
    containing ``create``, and a tool that rejects valid queries teaches the
    agent to stop using it.
    """
    import re

    return re.search(r"\b%s\b" % re.escape(word), text) is not None


def _day_range(window: Optional[Sequence[int]], label: str) -> Tuple[int, ...]:
    """Parse ``[start, end]`` or ``[d1, d2, d3]`` into an explicit day tuple."""
    if window is None:
        raise ToolError("%s is required, as [start_day, end_day]" % label)
    if isinstance(window, (int, float)):
        window = [int(window)]
    try:
        days = [int(d) for d in window]
    except (TypeError, ValueError) as exc:
        raise ToolError("%s must be a list of integer day indices" % label) from exc
    if not days:
        raise ToolError("%s is empty" % label)
    if len(days) == 2 and days[1] > days[0] + 1:
        # Two values are read as an inclusive range, which is what a model
        # writing [3, 4] almost always means.
        days = list(range(days[0], days[1] + 1))
    return tuple(sorted(set(days)))


def _trailing(window_days: Sequence[int], max_day: int, length: int = 3) -> Tuple[int, ...]:
    """The ``length`` days immediately before a window. Clipped at day 0.

    A *trailing* baseline, never a surrounding one. Including days after the
    window would let the incident's own aftermath into the comparison, and if the
    elevation has not resolved the baseline absorbs it and the tool reports no
    change.
    """
    start = min(window_days)
    lo = max(0, start - length)
    return tuple(range(lo, start))


#: A healthy merchant. Nothing is misconfigured on this dataset, and the
#: ``get_merchant_config`` docstring says why that is stated rather than implied.
DEFAULT_MERCHANT_CONFIG: Dict[str, Any] = {
    "enabled_methods": ["upi", "card", "netbanking", "wallet"],
    "enabled_card_networks": ["visa", "mastercard", "rupay"],
    "upi_autopay_enabled": True,
    "mandate_max_amount_band": 5,
    "afa_threshold_band": 4,          # authentication required from band 4 up
    "three_ds_enabled": True,
    "retry_on_alternate_rail": True,
    "checkout_version": "standard",
}
