"""The ``RecoveryPlan`` schema -- re-exported, not redefined.

``pramaan/schemas.py`` is the authority (Day 1: "Defined on Day 1, populated on
Days 4-5"). BUILD-PLAN Day 5 names this file, ``pramaan/plan/schema.py``, as
where the schema lives; the two instructions are reconciled by having this
module be a pure re-export rather than a second definition. Two copies of a
pydantic model is exactly the kind of drift this project keeps catching itself
on elsewhere (the reason taxonomy, the rule-id registry) -- one authority, one
place that imports it.
"""
from __future__ import annotations

from pramaan.schemas import (
    Action,
    Channel,
    Claim,
    Diagnosis,
    PlannerFeatures,
    PlanStep,
    Receipt,
    RecoveryPlan,
    Strict,
)

__all__ = [
    "Action",
    "Channel",
    "Claim",
    "Diagnosis",
    "PlannerFeatures",
    "PlanStep",
    "Receipt",
    "RecoveryPlan",
    "Strict",
]
