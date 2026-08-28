"""The receipt auditor. Deterministic, model-free, and the reason a diagnosis
from an LLM is allowed to influence anything at all.

PRD 6.2 states the mechanism and this module is it, in four steps:

1. Every claim must reference a real ``call_id`` from *this session's* tool log.
2. The claim's ``result_hash`` must match the recorded tool output, so a claim
   cannot drift from its evidence.
3. Claims failing either check are **stripped**, and the diagnosis is re-scored
   without them.
4. If stripping drops support below threshold, the diagnosis is downgraded to
   ``UNSUPPORTED`` and no action is planned.

There is no LLM in this file and there is no import path from here to one. That
is what makes the guarantee mean anything: ``tests/test_receipt_auditor.py``
constructs fabricated diagnoses by hand and checks they are stripped, so the
property is enforced by code rather than by a sentence in a prompt asking the
model to be honest.

**Why the model does not type the hash, and why that is stronger.**

The PRD's illustrative JSON shows a receipt carrying ``result_hash`` alongside
``call_id``, which reads as though the model transcribes the digest. Implemented
literally that is *weaker*, for two reasons and one of them is fatal:

- A 64-character hex digest in prompt bytes is exactly what
  ``prompts.assert_no_identifiers`` refuses -- correctly, since a hex digest is
  the canonical high-cardinality string. Showing the model a hash to copy means
  either weakening the canonicality screen or truncating the digest until it is
  no longer tamper-evident.
- A model that can transcribe a hash for a true claim can transcribe it just as
  accurately for a false one. Transcription proves the model read the tool
  output; it does not bind the claim to it.

So the harness stamps ``result_hash`` from its own log when the model cites a
``call_id``, and this auditor then **independently recomputes** the digest from
the stored result and compares. That catches strictly more: a fabricated call_id,
a diagnosis edited after the fact, and a tool log whose stored result was altered
after the receipt was issued. What it deliberately does not claim to catch is
covered under "the limit" below.

**Check 3, which the PRD does not specify, and the reason it is here.**

Provenance is not relevance. A claim can cite a real call whose result does not
support it -- "tier3's failure rate rose 20 points, receipt tc_02" when tc_02
reported 2.0pp. Both PRD checks pass: the call exists, the hash matches. The
claim is still false, and it is false in the direction that moves money.

So every *number* appearing in a claim's text must also appear in one of that
claim's cited results, within tolerance. This is mechanical, it is cheap, and it
binds precisely the subset of claims that quantify something -- which is the
subset a planner acts on. A claim whose numbers are not in its own evidence is
stripped like any other unbacked claim.

**Two limits, stated rather than papered over.** Both were found by writing a
test that expected the auditor to catch something and discovering it did not.

*A qualitative claim is unverifiable by construction.* "This looks like an issuer
problem", citing a real receipt, passes all three checks and none of them says
anything about whether it is true. Receipts bound what a diagnosis may *assert as
fact*; they do not make its judgement correct.

*Check 3 verifies that a number is present in the evidence, not that it is
attached to the right thing.* A claim of "tier3's failure rate rose 41.0pp"
citing a ``compare_baseline`` call passes if 41.0 appears anywhere in that
result -- and on this dataset it does, because 41.0% is tier2's window rate. The
claim is false about tier3 and the number is real, so the check clears it.
Binding a number to its subject means parsing the claim's grammar, which is not a
mechanical operation and would put a language model back inside the verifier it
is being verified by. ``test_number_present_but_attached_to_the_wrong_entity``
records this as a known limit rather than leaving it as a silent hole.

What check 3 does catch is the case that matters most in practice: a magnitude
that exists nowhere in the evidence at all, which is what invention looks like
when a model reaches for a number it did not read.

The measurement that covers what receipts cannot is hypothesis precision against
canary outcomes (PRD 8), which is Day 6's work. Reporting receipt coverage as
though it were accuracy would be exactly the overclaim this apparatus exists to
avoid.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pramaan.investigate.tools import ToolBelt, ToolResult, hash_of
from pramaan.schemas import Claim, Diagnosis, Receipt

#: A diagnosis needs at least this share of its claims to survive the audit.
#: Two thirds, and the reasoning is that a diagnosis is a *conjunction*: the
#: hypothesis rests on its claims jointly, so losing a third of them means the
#: argument being made is no longer the argument that was checked.
SUPPORT_THRESHOLD = 2.0 / 3.0

#: And at least this many surviving claims, regardless of share. Without a floor,
#: a one-claim diagnosis whose single claim survives scores 100% support -- which
#: is arithmetically true and substantively worthless, since PRD 6.2's example
#: diagnosis triangulates across three independent tools precisely because one
#: query is not an investigation.
MIN_SURVIVING_CLAIMS = 2

#: Tolerance when matching a number in a claim against a number in its evidence.
#:
#: Both sides are first normalised to a **single scale** -- percentage points,
#: 0 to 100 -- and only then compared. That normalisation is the whole design,
#: and it replaces a first attempt that used one relative tolerance across both
#: scales. That attempt was wrong in the direction that matters, and the test
#: that caught it is ``test_quantity_absent_from_cited_evidence_is_stripped``:
#: a 2% relative tolerance at the 41.7% level is +/-0.83 percentage points of
#: slack, so a claim of "41.0pp" against evidence of 41.7% verified happily. The
#: check was admitting fabrications a third of a point wide of the truth while
#: appearing to work.
#:
#: A single relative tolerance cannot serve both scales, because the same
#: quantity appears in evidence as 0.417 and in a claim as 41.7%, and any
#: absolute floor generous enough for one is enormous on the other -- 0.05 is
#: five percentage points when read as a fraction. Normalising first means one
#: tolerance is enough.
#:
#: The absolute floor is 0.15 percentage points: the tools render rates to one
#: decimal place, so a claim quoting what it was shown lands within 0.1, and the
#: floor is that plus rounding. The relative term takes over for counts, where
#: quoting 1,238 as 1,240 is accuracy rather than invention.
TOLERANCE_PP = 0.15
TOLERANCE_RELATIVE = 0.01

#: A value at or below this is read as a fraction and scaled up. Above it, a
#: count or an already-percentage figure. 1.5 rather than 1.0 so that a rate of
#: 1.0 (100%) is still treated as a fraction, which is the reading that makes a
#: claim of "100%" match evidence of 1.0.
FRACTION_CEILING = 1.5

#: Numbers a claim may state without evidence. Small integers are usually
#: structural ("all 3 segments", "the 2 days of the window") rather than
#: measurements, and requiring evidence for "3" would strip claims for counting.
#: Checked against the *raw* claim value, before scale normalisation, since it is
#: the literal token in the text that is structural rather than measured.
FREE_NUMBERS: Set[float] = {0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 100.0}

STATUS_SUPPORTED = "SUPPORTED"
STATUS_UNSUPPORTED = "UNSUPPORTED"

#: Reasons a claim can be stripped. Named, so the ledger can cite one and the
#: metric can be broken down by failure mode rather than reported as a single
#: opaque coverage figure.
REASON_NO_RECEIPT = "no_receipt_cited"
REASON_UNKNOWN_CALL = "call_id_not_in_session_log"
REASON_HASH_MISMATCH = "result_hash_does_not_match_recorded_output"
REASON_ARGS_MISMATCH = "args_hash_does_not_match_recorded_arguments"
REASON_FAILED_CALL = "cited_call_failed"
REASON_NUMBER_NOT_IN_EVIDENCE = "quantity_absent_from_cited_evidence"


@dataclass(frozen=True)
class StrippedClaim:
    """A claim that did not survive, and precisely why.

    Kept rather than discarded because the *reasons* are the interesting output.
    "Receipt coverage was 94%" is a number; "the six stripped claims were all
    quantities absent from their own evidence" is a finding about the model.
    """

    claim_id: str
    statement: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class AuditResult:
    """The audited diagnosis, and the arithmetic that got there."""

    diagnosis: Diagnosis
    status: str
    surviving: Tuple[Claim, ...]
    stripped: Tuple[StrippedClaim, ...]
    claims_in: int
    receipt_coverage: float
    support: float
    threshold: float
    notes: Tuple[str, ...] = ()

    @property
    def is_supported(self) -> bool:
        return self.status == STATUS_SUPPORTED

    @property
    def may_plan_action(self) -> bool:
        """The one question the rest of the system asks this module.

        An UNSUPPORTED diagnosis returns False, and Day 5's planner is wired to
        this property rather than to the diagnosis object -- so an unsupported
        claim is mechanically incapable of moving money rather than merely
        discouraged from it.
        """
        return self.status == STATUS_SUPPORTED

    def as_ledger_payload(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "diagnosis_class": self.diagnosis.diagnosis_class,
            "claims_in": self.claims_in,
            "claims_surviving": len(self.surviving),
            "claims_stripped": len(self.stripped),
            "receipt_coverage": round(self.receipt_coverage, 4),
            "support": round(self.support, 4),
            "threshold": round(self.threshold, 4),
            "stripped": [
                {"claim_id": s.claim_id, "reason": s.reason, "detail": s.detail}
                for s in self.stripped
            ],
            "may_plan_action": self.may_plan_action,
        }

    def summary_lines(self) -> List[str]:
        lines = [
            "receipt audit: %s" % self.status,
            "  claims in            %d" % self.claims_in,
            "  surviving            %d" % len(self.surviving),
            "  stripped             %d" % len(self.stripped),
            "  receipt coverage     %.1f%%" % (100.0 * self.receipt_coverage),
            "  support              %.1f%% (threshold %.1f%%)"
            % (100.0 * self.support, 100.0 * self.threshold),
        ]
        for s in self.stripped:
            lines.append("  STRIPPED %-6s %s" % (s.claim_id, s.reason))
            if s.detail:
                lines.append("           %s" % s.detail)
        for note in self.notes:
            lines.append("  note: %s" % note)
        return lines


# --------------------------------------------------------------------------
# Numeric extraction
# --------------------------------------------------------------------------

#: Numbers as a model writes them in prose: ``41.0%``, ``+12pp``, ``1,240``,
#: ``0.437``. The trailing-unit groups are captured so a percentage can be
#: compared against a rate expressed as a fraction.
_NUMBER = re.compile(r"(?<![\w.])([+-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(%|pp|percentage points)?")


def numbers_in(text: str) -> List[Tuple[float, str]]:
    """Every number in a string, with the unit it was written in.

    Returns ``(value, unit)`` where unit is ``''``, ``'%'`` or ``'pp'``. Both
    forms are kept because a model may write a rate either way and the evidence
    stores it as a fraction; collapsing them here would make ``41%`` and
    ``0.41`` fail to match each other.
    """
    out: List[Tuple[float, str]] = []
    for raw, unit in _NUMBER.findall(text):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        normalised = "%" if unit == "%" else ("pp" if unit in ("pp", "percentage points") else "")
        out.append((value, normalised))
    return out


def to_percent_scale(value: float) -> float:
    """Normalise a figure to percentage points.

    A fraction becomes a percentage; a count or an existing percentage is left
    alone. Applied identically to both sides of every comparison, which is what
    makes one tolerance sufficient -- see ``TOLERANCE_PP``.

    The ambiguity is real and is accepted knowingly: a bare ``1`` might be a
    count of one or a rate of 100%, and this function cannot tell. It does not
    need to, because it transforms claim and evidence the same way, so the two
    readings agree with each other whichever one is right. What it buys in
    exchange is that no comparison ever straddles two scales, which is where the
    original defect lived.
    """
    return value * 100.0 if abs(value) <= FRACTION_CEILING else value


def _numbers_in_payload(payload: Any, into: Optional[List[float]] = None) -> List[float]:
    """Every numeric leaf of a result payload, flattened onto the percent scale."""
    if into is None:
        into = []
    if isinstance(payload, bool):
        return into
    if isinstance(payload, (int, float)):
        into.append(to_percent_scale(float(payload)))
        return into
    if isinstance(payload, dict):
        for value in payload.values():
            _numbers_in_payload(value, into)
        return into
    if isinstance(payload, (list, tuple)):
        for value in payload:
            _numbers_in_payload(value, into)
        return into
    if isinstance(payload, str):
        # Numbers embedded in a string leaf still count as evidence: a rendered
        # figure the tool itself produced is the tool's own output.
        for value, unit in numbers_in(payload):
            into.append(value if unit in ("%", "pp") else to_percent_scale(value))
    return into


def _matches_evidence(value: float, unit: str, evidence: Sequence[float]) -> bool:
    """Whether a claimed figure appears in its cited evidence, within tolerance.

    A figure written with an explicit ``%`` or ``pp`` is already on the percent
    scale and is taken as given; a bare figure is normalised. Everything in
    ``evidence`` arrived normalised, so the comparison happens on one scale.
    """
    claimed = value if unit in ("%", "pp") else to_percent_scale(value)
    for known in evidence:
        tolerance = max(TOLERANCE_PP, TOLERANCE_RELATIVE * abs(known))
        if abs(claimed - known) <= tolerance:
            return True
    return False


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------


def audit(
    diagnosis: Diagnosis,
    belt: ToolBelt,
    *,
    threshold: float = SUPPORT_THRESHOLD,
    min_surviving: int = MIN_SURVIVING_CLAIMS,
    check_numbers: bool = True,
) -> AuditResult:
    """Audit a diagnosis against a session's tool log. Pure and deterministic.

    ``belt`` is the session's own tool belt, passed rather than looked up. A
    global registry would make "this session's log" a hopeful phrase instead of a
    checkable one, and cross-session receipt reuse is exactly the hole the first
    check exists to close.
    """
    receipts_by_id = {r.receipt_id: r for r in diagnosis.receipts}
    log = belt.log

    surviving: List[Claim] = []
    stripped: List[StrippedClaim] = []
    notes: List[str] = []

    for claim in diagnosis.claims:
        verdict = _audit_one(claim, receipts_by_id, log, check_numbers=check_numbers)
        if verdict is None:
            surviving.append(claim)
        else:
            stripped.append(verdict)

    claims_in = len(diagnosis.claims)
    backed = sum(1 for c in diagnosis.claims if c.is_backed)
    receipt_coverage = (backed / claims_in) if claims_in else 1.0
    support = (len(surviving) / claims_in) if claims_in else 0.0

    # A diagnosis with no claims at all is not "fully supported"; it is an
    # assertion with nothing behind it, and the arithmetic above would otherwise
    # hand it a coverage of 100%.
    if claims_in == 0:
        status = STATUS_UNSUPPORTED
        notes.append("no claims were made, so there is nothing to support the class")
    elif len(surviving) < min_surviving:
        status = STATUS_UNSUPPORTED
        notes.append(
            "only %d claim(s) survived; a diagnosis needs at least %d independent "
            "checks" % (len(surviving), min_surviving)
        )
    elif support < threshold:
        status = STATUS_UNSUPPORTED
        notes.append(
            "support %.1f%% is below the %.1f%% threshold, so the argument that "
            "survived is not the argument that was made"
            % (100.0 * support, 100.0 * threshold)
        )
    else:
        status = STATUS_SUPPORTED

    # Re-scored without the stripped claims, which is step 3 of PRD 6.2. The
    # receipts of stripped claims go too: a receipt nothing cites is not evidence,
    # and leaving it in would let a later reader believe the diagnosis rested on
    # more than it does.
    cited: Set[str] = {rid for c in surviving for rid in c.receipt_ids}
    rescored = diagnosis.model_copy(
        update={
            "claims": list(surviving),
            "receipts": [r for r in diagnosis.receipts if r.receipt_id in cited],
            "status": status,
            # An UNSUPPORTED diagnosis keeps its class for the record but is
            # barred from action by may_plan_action. Blanking the class here
            # would destroy the audit trail of what the model actually said.
            "confidence": diagnosis.confidence if status == STATUS_SUPPORTED else 0.0,
        }
    )

    return AuditResult(
        diagnosis=rescored,
        status=status,
        surviving=tuple(surviving),
        stripped=tuple(stripped),
        claims_in=claims_in,
        receipt_coverage=receipt_coverage,
        support=support,
        threshold=threshold,
        notes=tuple(notes),
    )


def _audit_one(
    claim: Claim,
    receipts_by_id: Dict[str, Receipt],
    log: Dict[str, ToolResult],
    *,
    check_numbers: bool,
) -> Optional[StrippedClaim]:
    """Audit one claim. Returns None if it survives, else why it did not."""
    if not claim.receipt_ids:
        return StrippedClaim(
            claim_id=claim.claim_id,
            statement=claim.statement,
            reason=REASON_NO_RECEIPT,
            detail="the claim cites no tool call",
        )

    evidence: List[float] = []
    for receipt_id in claim.receipt_ids:
        receipt = receipts_by_id.get(receipt_id)
        entry = log.get(receipt_id)

        # Check 1: the cited call must exist in this session's log.
        if entry is None:
            return StrippedClaim(
                claim_id=claim.claim_id,
                statement=claim.statement,
                reason=REASON_UNKNOWN_CALL,
                detail="cited %r, which is not a call in this session" % receipt_id,
            )

        # A receipt object is expected but its absence is not fatal on its own:
        # the log is authoritative, so a claim citing a real call_id with no
        # accompanying receipt object is treated as citing the log entry. What is
        # fatal is a receipt object that *disagrees* with the log.
        if receipt is not None:
            # Check 2: the recorded digests must match the stored output. Both
            # sides are recomputed rather than compared as strings, so an altered
            # payload fails even if its recorded hash was altered to match.
            if receipt.result_hash != hash_of(entry.result):
                return StrippedClaim(
                    claim_id=claim.claim_id,
                    statement=claim.statement,
                    reason=REASON_HASH_MISMATCH,
                    detail="receipt %s claims a result hash that is not the hash of "
                    "the recorded output" % receipt_id,
                )
            if receipt.args_hash != hash_of(entry.args):
                return StrippedClaim(
                    claim_id=claim.claim_id,
                    statement=claim.statement,
                    reason=REASON_ARGS_MISMATCH,
                    detail="receipt %s claims arguments that are not the recorded "
                    "arguments" % receipt_id,
                )
            if receipt.tool != entry.tool:
                return StrippedClaim(
                    claim_id=claim.claim_id,
                    statement=claim.statement,
                    reason=REASON_ARGS_MISMATCH,
                    detail="receipt %s names tool %r but %s was a call to %r"
                    % (receipt_id, receipt.tool, receipt_id, entry.tool),
                )

        # A failed call is a real log entry and hashes correctly, so checks 1 and
        # 2 pass on it. It supports nothing, though, and a claim resting on an
        # error message is exactly the kind of thing that should not reach a
        # planner.
        if not entry.ok:
            return StrippedClaim(
                claim_id=claim.claim_id,
                statement=claim.statement,
                reason=REASON_FAILED_CALL,
                detail="cited %s, which returned an error rather than a result"
                % receipt_id,
            )

        _numbers_in_payload(entry.result, evidence)

    # Check 3: every quantity the claim states must appear in its own evidence.
    if check_numbers:
        for value, unit in numbers_in(claim.statement):
            if value in FREE_NUMBERS and unit == "":
                continue
            if not _matches_evidence(value, unit, evidence):
                return StrippedClaim(
                    claim_id=claim.claim_id,
                    statement=claim.statement,
                    reason=REASON_NUMBER_NOT_IN_EVIDENCE,
                    detail="the claim states %g%s, which does not appear in the "
                    "output of %s" % (value, unit, ", ".join(claim.receipt_ids)),
                )

    return None


# --------------------------------------------------------------------------
# Receipt construction -- the harness side
# --------------------------------------------------------------------------


def receipts_for(belt: ToolBelt, call_ids: Iterable[str]) -> List[Receipt]:
    """Build receipt objects from the session log for the calls cited.

    This is the harness stamping the digests, per the module docstring. It is a
    separate function from ``audit`` on purpose: the thing that issues a receipt
    and the thing that verifies it must be able to disagree, or the verification
    is a tautology. ``tests/test_receipt_auditor.py`` exploits exactly that
    separation, hand-building receipts that disagree with the log.
    """
    out: List[Receipt] = []
    for call_id in call_ids:
        entry = belt.get(call_id)
        if entry is None:
            continue
        out.append(
            Receipt(
                receipt_id=entry.call_id,
                tool=entry.tool,
                args_hash=entry.args_hash,
                result_hash=entry.result_hash,
                row_count=entry.row_count,
            )
        )
    return out


def coverage_of(results: Sequence[AuditResult]) -> Dict[str, Any]:
    """Aggregate receipt coverage across a run. Printed as a headline (PRD 8).

    Reported alongside the *stripped* count rather than alone. Coverage is the
    share of claims that cited anything at all; on its own it flatters a model
    that cites a receipt for every claim and gets half of them wrong.
    """
    claims = sum(r.claims_in for r in results)
    backed = sum(int(round(r.receipt_coverage * r.claims_in)) for r in results)
    surviving = sum(len(r.surviving) for r in results)
    stripped: Dict[str, int] = {}
    for result in results:
        for s in result.stripped:
            stripped[s.reason] = stripped.get(s.reason, 0) + 1
    return {
        "diagnoses": len(results),
        "supported": sum(1 for r in results if r.is_supported),
        "unsupported": sum(1 for r in results if not r.is_supported),
        "claims": claims,
        "claims_backed": backed,
        "claims_surviving": surviving,
        "receipt_coverage": (backed / claims) if claims else 1.0,
        "survival_rate": (surviving / claims) if claims else 1.0,
        "stripped_by_reason": dict(sorted(stripped.items())),
    }
