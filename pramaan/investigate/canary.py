"""The canary protocol (BUILD-PLAN Day 6, block D): test a diagnosis against
evidence the investigator never had, and retract it on refutation.

The whole tool belt (Day 4) is read-only and deterministic-arithmetic except
for the model choosing which tools to call and how to narrate the result.
``decompose`` in particular is real arithmetic, not a language model's
opinion: given a window and a baseline, it returns exactly which segment's
own rate rose and which segment's traffic mix shifted, with the algebraic
identity checked before the result is even returned (``tools.py``). So the
canary does not need to re-derive anything or parse the model's prose --
it rereads the *same* ``decompose`` call the session already made, and
checks whether the segment it names as the rate-shift culprit (the
"something broke, act on this" one) is the one that was **actually**
injected. ``sim.incident.InjectedTruth`` is the ground truth the
investigator's tool belt never has access to (ADR-010's own defence, one
layer up): the events table carries no marker of what was injected, so a
model correctly identifying the segment did so from evidence, not from
reading an answer key.

This is deliberately narrower than "test the model's stated falsifier"
(``Diagnosis.falsifiable_by``), which is free text and would need its own
LLM call to interpret -- exactly the scope BUILD-PLAN's own contingency
table warns against growing on this day ("do not let the canary/retraction
work expand"). Checking the real tool call's own numbers against ground
truth is the version that is testable today with no new model dependency,
and it tests the part of the diagnosis that actually drives an action: which
segment gets treated as broken.

**Do not stage a refutation.** If a session's own ``decompose`` call already
points at the injected segment -- which is expected, because
``sim.incident.DEV_INCIDENT`` was built to be correctly diagnosable -- the
canary confirms it, honestly, and that is not a weaker result than a
refutation would have been. ``tests/test_canary.py`` proves the REFUTED path
and the RETRACTION row it writes against a deliberately wrong tool result
built for that one purpose, never against the real batch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from pramaan.investigate.tools import ToolResult

CONFIRMED = "CONFIRMED"
REFUTED = "REFUTED"
NOT_TESTABLE = "NOT_TESTABLE"
VERDICTS = (CONFIRMED, REFUTED, NOT_TESTABLE)

#: Below this, a decompose term is noise rather than a claimed effect. House
#: number, not a regulation -- chosen well under the +3.79pp/+3.48pp terms
#: the Day 4 incident actually produces.
EFFECT_FLOOR_PP = 0.005


@dataclass(frozen=True)
class CanaryVerdict:
    """The canary's answer, and everything needed to write it to the ledger."""

    verdict: str
    observed_rate_segment: Optional[str]
    observed_mix_segment: Optional[str]
    truth_rate_segment: Optional[str]
    truth_mix_segment: Optional[str]
    evidence: Dict[str, Any]

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError("verdict must be one of %r, got %r" % (VERDICTS, self.verdict))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "observed_rate_segment": self.observed_rate_segment,
            "observed_mix_segment": self.observed_mix_segment,
            "truth_rate_segment": self.truth_rate_segment,
            "truth_mix_segment": self.truth_mix_segment,
            "evidence": self.evidence,
        }


def _latest_decompose(tool_log: Sequence[ToolResult]) -> Optional[ToolResult]:
    calls = [t for t in tool_log if t.tool == "decompose" and t.ok]
    return calls[-1] if calls else None


def _dominant_segment(
    per_key: List[Dict[str, Any]], field: str, floor: float
) -> Optional[str]:
    """Which segment carries the largest-magnitude ``field`` term, or None if
    every term is inside the noise floor -- "nothing here rises to a claim"
    is a real, distinct answer from "segment X is the culprit"."""
    if not per_key:
        return None
    dominant = max(per_key, key=lambda row: abs(row[field]))
    return dominant["key"] if abs(dominant[field]) >= floor else None


def run_canary(tool_log: Sequence[ToolResult], truth: Any) -> CanaryVerdict:
    """Check the session's own ``decompose`` call against injected ground truth.

    ``truth`` is a ``sim.incident.InjectedTruth`` (not type-hinted directly,
    to avoid an import edge from ``pramaan.investigate`` into ``sim`` at
    module load -- the investigator's own package must stay importable with
    no simulator present, exactly like every other Day 4 module).
    """
    decompose_call = _latest_decompose(tool_log)
    if decompose_call is None:
        return CanaryVerdict(
            verdict=NOT_TESTABLE,
            observed_rate_segment=None,
            observed_mix_segment=None,
            truth_rate_segment=truth.rate_segment,
            truth_mix_segment=truth.mix_segment,
            evidence={
                "reason": "no decompose call in this session's tool log -- "
                "there is nothing to test the diagnosis against"
            },
        )

    per_key = decompose_call.result["per_key"]
    observed_rate_segment = _dominant_segment(per_key, "rate_effect", EFFECT_FLOOR_PP)
    observed_mix_segment = _dominant_segment(per_key, "mix_effect", EFFECT_FLOOR_PP)

    evidence = {
        "decompose_call_id": decompose_call.call_id,
        "per_key": per_key,
        "truth": truth.as_dict(),
    }

    confirmed = (
        observed_rate_segment == truth.rate_segment
        and observed_mix_segment == truth.mix_segment
    )
    return CanaryVerdict(
        verdict=CONFIRMED if confirmed else REFUTED,
        observed_rate_segment=observed_rate_segment,
        observed_mix_segment=observed_mix_segment,
        truth_rate_segment=truth.rate_segment,
        truth_mix_segment=truth.mix_segment,
        evidence=evidence,
    )


def run_canary_for_session(session: Any, truth: Any) -> CanaryVerdict:
    """Convenience: pull the tool log straight off a real investigation."""
    tool_log = [session.belt.get(cid) for cid in session.belt.call_ids]
    return run_canary(tool_log, truth)


# --------------------------------------------------------------------------
# The ledger writer -- CANARY always, RETRACTION only on refutation
# --------------------------------------------------------------------------


def write_canary_result(
    ledger: Any, *, ts: str, arm: str, verdict: CanaryVerdict
) -> None:
    """One CANARY row per check, always; one RETRACTION row, only if refuted.

    Two kinds because they are two different assertions (the same reasoning
    ADR-011 already applied to DIAGNOSIS/RECEIPT_AUDIT): CANARY is "this check
    ran, here is what it found"; RETRACTION is "a specific prior claim is
    withdrawn, and here is the contradicting evidence" -- collapsing them
    would erase the fact that most canary checks confirm rather than refute.
    """
    ledger.append("CANARY", ts=ts, arm=arm, payload=verdict.as_dict())
    if verdict.verdict == REFUTED:
        ledger.append(
            "RETRACTION",
            ts=ts,
            arm=arm,
            payload={
                "retracted_verdict": verdict.verdict,
                "contradicting_evidence": verdict.evidence,
            },
        )
