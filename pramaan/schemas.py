"""Structured-output schemas. Defined on Day 1, populated on Days 4-5.

Writing these before anything calls a model is not busywork. Two of them encode
design commitments that are hard to retrofit:

``Receipt`` / ``Claim``
    Every claim an investigator makes carries a receipt naming the tool call that
    backs it (PRD 4). The receipt auditor then strips unbacked claims *before*
    they can move money (invariant I5). If claims and receipts were not separate
    objects from the start, "this claim is fabricated" would not be a
    representable state and the auditor could not exist.

``PlanStep.delay_seconds``
    Step timing is a **relative offset**, never an absolute timestamp. This is
    the memoisation invariant showing up in the schema layer: a plan is cached
    per signature and reused across every event that shares it, so a plan
    carrying an absolute time would be valid for exactly one event and the ~30x
    ratio would collapse to 1x (PRD 9.1). Relative offsets make a plan a
    *policy* rather than a schedule.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pramaan.canonical import (
    ACTIONS,
    AMOUNT_BANDS,
    CHANNEL_ELIGIBILITY,
    DIAGNOSIS_CLASSES,
    HOUR_BUCKETS,
    LEGAL_CONTEXTS,
    SEGMENTS,
)

Action = Literal[
    "ACT_WAIT",
    "ACT_ROUTE",
    "ACT_RETRY",
    "ACT_MESSAGE",
    "ACT_VOICE",
    "ACT_CONCESSION",
    "ACT_ALERT_MERCHANT",
    "ACT_PAGE_ENGINEER",
    "ACT_ESCALATE_HUMAN",
    "ACT_STOP",
]

Channel = Literal["none", "sms", "whatsapp", "email", "in_app", "voice"]


class Strict(BaseModel):
    """Reject unknown fields rather than ignoring them.

    A model that invents a field is telling you it misread the schema, and
    silently dropping it is how a plan ends up missing a stop condition the
    model thought it had set.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Receipt(Strict):
    """Proof that a tool call happened and produced what a claim says it did."""

    receipt_id: str
    tool: str = Field(description="query_sql | compare_baseline | decompose | get_downtime | get_config")
    args_hash: str = Field(description="sha256 of the canonical JSON of the arguments")
    result_hash: str = Field(description="sha256 of the canonical JSON of the result")
    row_count: int = Field(default=0, ge=0)


class Claim(Strict):
    """One assertion in a diagnosis, and the receipts that back it.

    ``receipt_ids`` being empty is a legal *parse* and an illegal *claim*. That
    asymmetry is deliberate: the model is allowed to emit an unbacked claim, and
    the auditor is what refuses it. Making it a validation error would hide the
    failure rate, and the receipt-coverage metric (PRD 4) is a number worth
    publishing.
    """

    claim_id: str
    statement: str
    receipt_ids: List[str] = Field(default_factory=list)
    #: Rupees this claim says are at stake, when it makes a quantitative point.
    magnitude_paise: Optional[int] = Field(default=None, ge=0)

    @property
    def is_backed(self) -> bool:
        return bool(self.receipt_ids)


class Diagnosis(Strict):
    """The investigator's output: a class, a narrative, and checkable claims."""

    diagnosis_class: str
    summary: str
    claims: List[Claim] = Field(default_factory=list)
    receipts: List[Receipt] = Field(default_factory=list)
    #: Cache keys of the LLM calls that produced this, so the ledger can link an
    #: artifact back to its exact prompt and response (PRD 12.2).
    llm_call_ids: List[str] = Field(default_factory=list)

    @field_validator("diagnosis_class")
    @classmethod
    def _known_class(cls, value: str) -> str:
        if value not in DIAGNOSIS_CLASSES:
            raise ValueError(
                "diagnosis_class %r is not one of %r -- the class is a signature "
                "field, so an unbounded value would break memoisation"
                % (value, DIAGNOSIS_CLASSES)
            )
        return value

    @property
    def receipt_coverage(self) -> float:
        """Share of claims that carry at least one receipt. An LLM quality metric."""
        return (
            sum(1 for c in self.claims if c.is_backed) / len(self.claims)
            if self.claims
            else 1.0
        )


class PlanStep(Strict):
    """One bounded action, with its timing expressed as an offset."""

    step_index: int = Field(ge=0)
    action: Action
    channel: Channel = "none"
    #: Seconds after the event becomes *actionable* (i.e. after the settle
    #: window), never an absolute timestamp. See the module docstring.
    delay_seconds: int = Field(ge=0)
    expected_value_paise: int = Field(default=0, ge=0)
    cost_paise: int = Field(default=0, ge=0)
    #: Which of S1..S7 abort this step. Named, so the ledger can cite one.
    stop_conditions: List[str] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in ACTIONS:
            raise ValueError("unknown action %r" % value)
        return value


class RecoveryPlan(Strict):
    """An ordered, time-sequenced, multi-channel plan for one signature.

    Keyed by signature rather than by event, which is the whole memoisation
    design: one plan serves every event sharing the signature, and the ledger
    records which plan a given event used.
    """

    signature: str
    steps: List[PlanStep] = Field(default_factory=list)
    expected_value_paise: int = Field(default=0, ge=0)
    rationale: str = ""
    llm_call_ids: List[str] = Field(default_factory=list)

    @field_validator("steps")
    @classmethod
    def _ordered(cls, steps: List[PlanStep]) -> List[PlanStep]:
        indices = [s.step_index for s in steps]
        if indices != sorted(indices):
            raise ValueError("plan steps must be in ascending step_index order")
        return steps

    @property
    def total_cost_paise(self) -> int:
        return sum(s.cost_paise for s in self.steps)


class PlannerFeatures(Strict):
    """The seven signature fields, as a validated object.

    Mirrors canonical.PLANNER_SIGNATURE_FIELDS. The tuple in canonical.py is the
    authority and this is the typed face of it, so
    ``tests/test_prompt_canonical.py`` checks the two cannot drift.
    """

    reason_class: str
    diagnosis_class: str
    amount_band: int
    segment: str
    legal_context: str
    channel_eligibility: str
    hour_bucket: str

    @field_validator("amount_band")
    @classmethod
    def _band(cls, value: int) -> int:
        if value not in AMOUNT_BANDS:
            raise ValueError("amount_band must be one of %r" % (AMOUNT_BANDS,))
        return value

    @field_validator("segment")
    @classmethod
    def _segment(cls, value: str) -> str:
        if value not in SEGMENTS:
            raise ValueError("unknown segment %r" % value)
        return value

    @field_validator("legal_context")
    @classmethod
    def _legal(cls, value: str) -> str:
        if value not in LEGAL_CONTEXTS:
            raise ValueError("unknown legal_context %r" % value)
        return value

    @field_validator("channel_eligibility")
    @classmethod
    def _channels(cls, value: str) -> str:
        if value not in CHANNEL_ELIGIBILITY:
            raise ValueError("unknown channel_eligibility %r" % value)
        return value

    @field_validator("hour_bucket")
    @classmethod
    def _hours(cls, value: str) -> str:
        if value not in HOUR_BUCKETS:
            raise ValueError("unknown hour_bucket %r" % value)
        return value

    def as_features(self) -> Dict[str, object]:
        return self.model_dump()
