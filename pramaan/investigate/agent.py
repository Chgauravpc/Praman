"""The investigator loop. Bounded turns, bounded tokens, structured output.

The shape, and the reason for each bound:

**Max eight turns.** BUILD-PLAN Day 4. Not a performance guard -- a correctness
one. An agent with unlimited turns on a dataset this size will keep querying
until it finds a pattern, and on any real dataset it will eventually find one,
because noise contains patterns. A hard turn ceiling forces the model to spend
its evidence budget on the hypothesis it actually holds.

**A hard token ceiling per session.** The prompt grows with the transcript, so
turn 8 costs several times turn 1. Without a ceiling one runaway session -- a
model that keeps issuing near-identical queries -- can eat a whole day's free
allowance, and the failure is invisible until the allowance is gone. The ceiling
is checked *before* each call, using the previous turns' measured usage, so it
cannot be exceeded by the call that discovers it.

**One re-ask on malformed output, then stop.** Small models emit prose around
JSON. ``LLMResponse.json`` already tolerates a fenced block; what it cannot fix
is a response with no object at all. One retry is worth it; a retry loop is how
the budget disappears, so the second failure ends the session with whatever
evidence has accumulated.

**The detector, not the model, chooses the window.** ``detect_incidents`` is
arithmetic -- PRD 1.1 step 3, zero LLM calls. The model is told *where* to look
and nothing else; it is not handed rows. That division is what BUILD-PLAN Day 4
means by "the agent QUERIES; it does not get handed the data", and it is the
structural reason the agent cannot invent an entity: every entity it can name
came out of a query it issued and whose result is in the log.

**Determinism.** Every turn's prompt is a pure function of the incident, the
transcript so far, and the turn number. The transcript is a pure function of the
tool results, which are pure functions of the projected database. So a session
replays exactly from cache: the second run of an unchanged investigation costs
zero tokens, which is a Day 4 definition-of-done item and falls out of the design
rather than needing a mechanism.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pramaan import canonical
from pramaan.investigate import receipts as receipts_mod
from pramaan.investigate.tools import ToolBelt, ToolResult, fmt_pp, fmt_rate
from pramaan.llm.prompts import (
    build_incident_brief,
    build_investigator_turn_prompt,
    render_transcript,
)
from pramaan.schemas import Claim, Diagnosis

#: BUILD-PLAN Day 4. Eight, and the loop stops at eight whether or not the model
#: has concluded.
MAX_TURNS = 8

#: Tokens one session may consume. ~7 turns at ~3,000 tokens is the BUILD-PLAN
#: happy-path figure; 30,000 leaves headroom for a transcript that grows faster
#: than expected without letting one session take a meaningful bite out of a
#: daily allowance.
SESSION_TOKEN_CEILING = 30_000

#: Completion cap per turn. A tool call is a short object; a diagnosis with four
#: claims is longer. 1,200 fits both with room, and a smaller cap truncates the
#: final diagnosis mid-JSON, which reads as a parse failure and wastes the turn.
MAX_COMPLETION_TOKENS = 1_200

#: Elevation, in percentage points of blended failure rate against the trailing
#: baseline, at which a day is flagged. Three points is well outside day-to-day
#: sampling movement on this batch and comfortably inside the injected incident.
DETECT_THRESHOLD_PP = 0.03

#: Days of trailing baseline. Three: enough to average out one quiet or busy day,
#: short enough that a slow drift does not get absorbed into the baseline it is
#: supposed to be measured against.
BASELINE_DAYS = 3


@dataclass(frozen=True)
class Incident:
    """A window the detector flagged, and the arithmetic that flagged it."""

    window: Tuple[int, ...]
    baseline: Tuple[int, ...]
    blended_baseline: float
    blended_window: float

    @property
    def delta(self) -> float:
        return self.blended_window - self.blended_baseline

    @property
    def label(self) -> str:
        return "days %d-%d" % (min(self.window), max(self.window))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "window": list(self.window),
            "baseline": list(self.baseline),
            "blended_baseline": round(self.blended_baseline, 6),
            "blended_window": round(self.blended_window, 6),
            "delta": round(self.delta, 6),
        }


@dataclass
class Turn:
    """One model turn: what was asked, what came back, what it cost."""

    index: int
    llm_call_id: str
    cache_hit: bool
    tokens: int
    thought: str
    tool: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    tool_call_id: Optional[str] = None
    concluded: bool = False
    parse_error: str = ""


@dataclass
class Session:
    """One investigation, start to finish, with everything needed to audit it."""

    incident: Incident
    belt: ToolBelt
    turns: List[Turn] = field(default_factory=list)
    diagnosis: Optional[Diagnosis] = None
    audit: Optional["receipts_mod.AuditResult"] = None
    stop_reason: str = ""

    @property
    def tokens(self) -> int:
        return sum(t.tokens for t in self.turns)

    @property
    def cache_hits(self) -> int:
        return sum(1 for t in self.turns if t.cache_hit)

    @property
    def llm_calls(self) -> int:
        return len(self.turns)

    @property
    def tool_calls(self) -> int:
        return len(self.belt.call_ids)

    def as_ledger_payload(self) -> Dict[str, Any]:
        return {
            "incident": self.incident.as_dict(),
            "turns": len(self.turns),
            "tool_calls": self.tool_calls,
            "llm_calls": self.llm_calls,
            "cache_hits": self.cache_hits,
            "tokens": self.tokens,
            "stop_reason": self.stop_reason,
            "diagnosis_class": self.diagnosis.diagnosis_class if self.diagnosis else None,
            "confidence": self.diagnosis.confidence if self.diagnosis else 0.0,
            "falsifiable_by": self.diagnosis.falsifiable_by if self.diagnosis else "",
            "falsifier_stated": bool(self.diagnosis and self.diagnosis.falsifier_stated),
            "claims": len(self.diagnosis.claims) if self.diagnosis else 0,
            "tool_log": [self.belt.get(c).as_ledger_payload() for c in self.belt.call_ids],
        }


# --------------------------------------------------------------------------
# Detection -- arithmetic, no model
# --------------------------------------------------------------------------


def blended_rate(belt: ToolBelt, days: Sequence[int]) -> float:
    """Failure rate over a set of days: total failures over total attempts.

    The *blended* rate, deliberately -- this is the number that misleads, and the
    detector's job is to notice it moving, not to explain it. Explaining it is
    what the decomposition tool is for, and the separation is the point: a
    detector that decomposed would be a detector that had already formed a
    hypothesis.
    """
    if not days:
        return 0.0
    placeholders = ",".join("?" * len(days))
    row = belt.conn.execute(
        "SELECT COALESCE(SUM(attempts),0) a, COALESCE(SUM(failures),0) f "
        "FROM agent_traffic WHERE day_index IN (%s)" % placeholders,
        tuple(days),
    ).fetchone()
    attempts, failures = int(row[0]), int(row[1])
    return failures / attempts if attempts else 0.0


def detect_incidents(
    belt: ToolBelt,
    *,
    threshold: float = DETECT_THRESHOLD_PP,
    baseline_days: int = BASELINE_DAYS,
) -> List[Incident]:
    """Flag windows whose blended failure rate is elevated against their own past.

    Adjacent flagged days merge into one window, which matters more than it looks:
    a two-day incident detected as two one-day incidents would be investigated
    twice, and PRD 1.1's whole cost argument is that one incident gets one
    diagnosis. Merging is what keeps the LLM off the per-event path in practice
    rather than only in principle.

    **The baseline must be the last CLEAN days, not simply the preceding ones.**
    This is a defect found by running the detector against a known two-day
    incident and getting a one-day window back. The naive version compares each
    day against the three days before it; on the second day of an incident, one of
    those three is the incident's own first day, so the baseline rises, the
    measured elevation shrinks below the threshold, and day two is not flagged.
    The result is that a multi-day degradation is *always* detected as its first
    day only -- the window handed to the investigator is too short, the
    decomposition is computed over part of the episode, and every figure
    downstream is quietly wrong about a real incident.

    Nothing raises when this happens. The detector returns a window, the
    investigation succeeds, and the diagnosis is about half an incident. So the
    baseline freezes at the last clean stretch and stays frozen for as long as
    consecutive days keep flagging.
    """
    max_day = int(
        (belt.conn.execute("SELECT MAX(day_index) FROM agent_traffic").fetchone()[0]) or 0
    )

    flagged: List[Tuple[int, Tuple[int, ...]]] = []
    frozen_baseline: Optional[Tuple[int, ...]] = None

    for day in range(baseline_days, max_day + 1):
        if frozen_baseline is not None:
            # Mid-run: keep comparing against the stretch that was clean before
            # the run started.
            base_days = frozen_baseline
        else:
            base_days = tuple(range(max(0, day - baseline_days), day))
        if not base_days:
            continue
        base = blended_rate(belt, base_days)
        now = blended_rate(belt, [day])
        if now - base >= threshold:
            if frozen_baseline is None:
                frozen_baseline = base_days
            flagged.append((day, frozen_baseline))
        else:
            frozen_baseline = None

    incidents: List[Incident] = []
    run: List[Tuple[int, Tuple[int, ...]]] = []

    def close(run_days: List[Tuple[int, Tuple[int, ...]]]) -> None:
        if not run_days:
            return
        window = tuple(d for d, _b in run_days)
        base_window = run_days[0][1]
        incidents.append(
            Incident(
                window=window,
                baseline=base_window,
                blended_baseline=blended_rate(belt, base_window),
                blended_window=blended_rate(belt, window),
            )
        )

    for entry in flagged:
        if run and entry[0] == run[-1][0] + 1:
            run.append(entry)
        else:
            close(run)
            run = [entry]
    close(run)
    return incidents


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


class ProtocolError(RuntimeError):
    """The model's output could not be read as a tool call or a diagnosis."""


def investigate(
    incident: Incident,
    belt: ToolBelt,
    client: Any,
    *,
    max_turns: int = MAX_TURNS,
    token_ceiling: int = SESSION_TOKEN_CEILING,
    tier: str = "strong",
    allowed_classes: Sequence[str] = canonical.DIAGNOSIS_CLASSES,
    audit: bool = True,
) -> Session:
    """Run one investigation to a diagnosis, or to a bound.

    ``client`` is anything exposing ``llm.LLMClient.call``. Injected rather than
    constructed here, which is what lets ``tests/test_receipt_auditor.py`` and
    ``tests/test_investigator_loop.py`` drive the whole loop with a scripted
    client and consume no tokens at all.

    The audit runs inside this function rather than being left to the caller. A
    caller who forgot would hold an unaudited ``Diagnosis`` whose ``status``
    field says UNSUPPORTED but whose claims are all present -- and the temptation
    to read the claims anyway is exactly the failure the auditor exists to
    prevent. Auditing here means the only diagnosis this module ever returns is
    one that has been through it.
    """
    session = Session(incident=incident, belt=belt)
    brief = build_incident_brief(
        incident.window, incident.baseline, incident.blended_baseline, incident.blended_window
    )
    entries: List[Tuple[str, str, str, str]] = []
    consecutive_parse_errors = 0

    for turn_index in range(1, max_turns + 1):
        # Checked before the call, using measured usage from prior turns, so the
        # ceiling cannot be exceeded by the call that notices it.
        if session.tokens >= token_ceiling:
            session.stop_reason = "token ceiling of %s reached after %d turns" % (
                format(token_ceiling, ","), len(session.turns),
            )
            break

        prompt = build_investigator_turn_prompt(
            brief, render_transcript(entries), allowed_classes, turn_index, max_turns
        )
        response = client.call(
            prompt,
            tier=tier,
            schema={"type": "object"},
            temperature=0.0,
            max_tokens=MAX_COMPLETION_TOKENS,
        )
        usage = response.usage or {}
        turn = Turn(
            index=turn_index,
            llm_call_id=response.call_id,
            cache_hit=bool(response.cache_hit),
            tokens=int(usage.get("total_tokens", 0) or 0),
            thought="",
        )

        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ProtocolError("top-level JSON is not an object")
        except Exception as exc:  # noqa: BLE001 -- any unreadable output is one case
            turn.parse_error = str(exc)[:200]
            session.turns.append(turn)
            consecutive_parse_errors += 1
            if consecutive_parse_errors >= 2:
                session.stop_reason = "two consecutive unreadable responses"
                break
            continue
        consecutive_parse_errors = 0

        turn.thought = str(payload.get("thought", ""))[:400]

        if "diagnosis" in payload and payload["diagnosis"]:
            diagnosis = _parse_diagnosis(payload["diagnosis"], belt, response.call_id)
            turn.concluded = True
            session.turns.append(turn)
            session.diagnosis = diagnosis
            session.stop_reason = "the model concluded on turn %d" % turn_index
            break

        tool = payload.get("tool")
        if not tool:
            turn.parse_error = "neither a tool nor a diagnosis was named"
            session.turns.append(turn)
            consecutive_parse_errors += 1
            if consecutive_parse_errors >= 2:
                session.stop_reason = "two consecutive responses named no action"
                break
            continue

        args = payload.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        result = belt.call(str(tool), args)
        turn.tool = str(tool)
        turn.args = dict(args)
        turn.tool_call_id = result.call_id
        session.turns.append(turn)
        entries.append(
            (result.call_id, result.tool, _summarise_args(result.args), result.rendered)
        )
    else:
        session.stop_reason = "turn limit of %d reached without a diagnosis" % max_turns

    if not session.stop_reason:
        session.stop_reason = "loop ended"

    # A session that ran out of turns still gets an audit, of an empty diagnosis.
    # That is deliberate: it produces an UNSUPPORTED verdict and a coverage entry,
    # so an investigation that failed to conclude appears in the metrics rather
    # than vanishing from the denominator.
    if audit:
        subject = session.diagnosis or Diagnosis(
            diagnosis_class="undiagnosed",
            summary="the investigation did not reach a diagnosis: %s" % session.stop_reason,
        )
        session.audit = receipts_mod.audit(subject, belt)
        session.diagnosis = session.audit.diagnosis

    return session


def _summarise_args(args: Dict[str, Any]) -> str:
    """A short, canonical rendering of a tool call's arguments for the transcript.

    SQL is truncated. A model that wrote a two-hundred-character query does not
    need to re-read it in full on every subsequent turn, and the transcript is
    the term that grows quadratically in a multi-turn loop -- so this is where
    the token budget is either kept or lost.
    """
    parts = []
    for key, value in sorted(args.items()):
        text = str(value)
        if len(text) > 160:
            text = text[:157] + "..."
        parts.append("%s=%s" % (key, text))
    return ", ".join(parts)


def _parse_diagnosis(raw: Any, belt: ToolBelt, llm_call_id: str) -> Diagnosis:
    """Coerce the model's diagnosis object into the schema.

    Tolerant where tolerance is harmless and strict where it is not. An unknown
    ``diagnosis_class`` becomes ``undiagnosed`` rather than raising, because the
    class is a signature field with a closed domain and a model that invents one
    should degrade to "I do not know" rather than take the session down. A
    *claim* that cites nothing is passed through untouched, because that is the
    auditor's judgement to make and silently dropping it here would hide the
    receipt-coverage number this project publishes.
    """
    if not isinstance(raw, dict):
        raw = {}

    diagnosis_class = str(raw.get("diagnosis_class", "undiagnosed") or "undiagnosed")
    if diagnosis_class not in canonical.DIAGNOSIS_CLASSES:
        diagnosis_class = "undiagnosed"

    try:
        confidence = float(raw.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    claims: List[Claim] = []
    for index, item in enumerate(raw.get("claims") or []):
        if not isinstance(item, dict):
            continue
        receipt_ids = item.get("receipt_ids") or item.get("receipts") or []
        if isinstance(receipt_ids, str):
            receipt_ids = [receipt_ids]
        claims.append(
            Claim(
                claim_id=str(item.get("claim_id") or "c%d" % (index + 1)),
                statement=str(item.get("statement") or item.get("text") or ""),
                receipt_ids=[str(r) for r in receipt_ids if r],
            )
        )

    cited = [rid for c in claims for rid in c.receipt_ids]
    return Diagnosis(
        diagnosis_class=diagnosis_class,
        summary=str(raw.get("summary", ""))[:1200],
        claims=claims,
        receipts=receipts_mod.receipts_for(belt, dict.fromkeys(cited)),
        llm_call_ids=[llm_call_id],
        confidence=confidence,
        falsifiable_by=str(raw.get("falsifiable_by", ""))[:600],
        # Always UNSUPPORTED at construction. The auditor is the only thing that
        # may set it otherwise, and defaulting the other way would mean a parse
        # failure produced a supported diagnosis.
        status="UNSUPPORTED",
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def session_lines(session: Session) -> List[str]:
    """The per-session block printed on every run."""
    lines = [
        "incident %s   blended %s -> %s (%s)"
        % (
            session.incident.label,
            fmt_rate(session.incident.blended_baseline),
            fmt_rate(session.incident.blended_window),
            fmt_pp(session.incident.delta),
        ),
        "  turns %d   tool calls %d   llm calls %d (%d from cache)   tokens %s"
        % (
            len(session.turns),
            session.tool_calls,
            session.llm_calls,
            session.cache_hits,
            format(session.tokens, ","),
        ),
        "  stop: %s" % session.stop_reason,
    ]
    for turn in session.turns:
        if turn.tool:
            lines.append(
                "  turn %d  %s -> %s(%s)"
                % (turn.index, turn.tool_call_id, turn.tool, _summarise_args(turn.args or {}))
            )
        elif turn.concluded:
            lines.append("  turn %d  concluded" % turn.index)
        elif turn.parse_error:
            lines.append("  turn %d  unreadable: %s" % (turn.index, turn.parse_error))
    if session.diagnosis is not None:
        d = session.diagnosis
        lines.append("  class: %s   confidence %.2f" % (d.diagnosis_class, d.confidence))
        lines.append("  summary: %s" % d.summary)
        lines.append(
            "  falsifiable_by: %s"
            % (d.falsifiable_by if d.falsifiable_by else "(NOT STATED -- a failed instruction)")
        )
    if session.audit is not None:
        lines.extend("  " + line for line in session.audit.summary_lines())
    return lines
