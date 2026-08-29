"""The planner package -- Day 5. The LLM proposes; it never executes.

``pramaan/schemas.py`` already carries ``RecoveryPlan``, ``PlanStep`` and
``PlannerFeatures`` -- they were written on Day 1 with a docstring saying they
would be populated on Days 4-5, because retrofitting a schema after the thing
that fills it exists is how a plan ends up shaped around whatever the model
happened to emit first. This package is what populates them:

``schema.py``    re-exports the Day 1 schema under this package's own name, so
                 BUILD-PLAN Day 5's file layout (``pramaan/plan/schema.py``)
                 and the Day 1 authority (``pramaan/schemas.py``) do not
                 disagree about which one to read.
``planner.py``   the planner itself. Signature memoisation is mandatory here,
                 not a later optimisation (BUILD-PLAN 1.7 rule 2, PRD 9.1) --
                 without it this day is impossible on a free tier.
``validate.py``  every step judged before execution (PRD 6.5, 12.1). A thin
                 wrapper over the Day 2 envelope, because the envelope does not
                 change to accommodate a plan; the plan is data the envelope
                 already knows how to judge.
"""
from __future__ import annotations
