"""The planner: an LLM proposes an ordered, timed, multi-step plan.

PRD 6.4. Two properties carry the whole day, and both are structural rather than
prompt-engineering:

**The planner emits a plan OBJECT.** It never calls a tool, never touches
Razorpay, never sends anything. It returns a ``RecoveryPlan`` -- steps,
channels, timing, stop conditions and a ``why`` per step -- and that object is
judged by the envelope (``pramaan.plan.validate``) before anything downstream
may act on it. An LLM that emits an auditable plan is far safer than one that
calls tools directly, and PRD 6.4 calls this the single most important safety
decision in the architecture. So this module has no import of
``pramaan.execute`` anywhere, and none should ever be added.

**Signature memoisation is mandatory, built now, not as a later optimisation**
(BUILD-PLAN 1.7 rule 2). ``Planner`` caches a built plan by
``canonical.planner_signature(features)`` -- the same seven-field key that
``pramaan.llm.cache`` already uses to dedupe the underlying model call. The two
caches are not redundant: the LLM cache saves *tokens*, keyed on the exact
prompt bytes; this cache saves *work* -- parsing, coercion, validation -- and is
what lets a caller ask "how many distinct situations did the planner actually
reason about" without re-deriving it from the token ledger. Without this layer,
Day 5 is impossible on a free tier (BUILD-PLAN Day 5, LLM budget line).

**NFR-2: a cache miss must never block the first action.** On a cold signature
with no cached and no live response available, ``Planner`` does not raise and
does not stall -- it returns the deterministic reason-class default (Appendix
A), the same lookup table arm B uses. Money never waits on a model. The
fallback is recorded as a fallback (``PlannerStats.fallback_built``), never
silently presented as an LLM decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pramaan import canonical, taxonomy
from pramaan.envelope.reason_map import MIN_SCHEDULED_RETRY_DELAY_SECONDS
from pramaan.llm.cache import CacheMiss
from pramaan.llm.prompts import build_planner_prompt
from pramaan.schemas import PlanStep, RecoveryPlan

#: Mirrors ``pramaan.eval.arms.DEFAULT_CHANNEL``. Duplicated rather than
#: imported: ``pramaan.eval`` will come to import *this* module (arm C dispatch
#: wires here), and importing back from ``eval`` would be a cycle. The
#: precedent for this exact trade -- a small closed vocabulary copied rather
#: than shared across a package boundary that must stay one-directional -- is
#: ``pramaan.envelope.windows.CHANNELS`` duplicating ``schemas.Channel``.
DEFAULT_CHANNEL: Dict[str, str] = {"ACT_MESSAGE": "sms", "ACT_VOICE": "voice"}

#: PRD 6.4's own stop-condition vocabulary, used by the deterministic fallback
#: plan. The LLM is free to name others; the fallback uses the PRD's own list
#: because it has no model to ask.
DEFAULT_STOP_CONDITIONS: Tuple[str, ...] = (
    "paid",
    "promise_recorded",
    "consent_withdrawn",
    "contact_budget_exhausted",
)

MAX_COMPLETION_TOKENS = 900


# --------------------------------------------------------------------------
# The NFR-2 fallback
# --------------------------------------------------------------------------


def default_plan_for(features: Dict[str, Any]) -> RecoveryPlan:
    """The deterministic reason-class default, as a one-step plan.

    Exactly arm B's policy (``pramaan.eval.arms.arm_step``), rebuilt from a
    features dict rather than a ``RiskEvent`` because the planner operates at
    signature granularity, not event granularity. Kept in step with arm B
    deliberately: NFR-2 says the first action *is* this table, so the fallback
    plan and arm B's proposal must be the same policy or the cache-miss path
    would silently become a third, untested policy.
    """
    canonical.validate_features(features)
    reason_class = features["reason_class"]
    action = taxonomy.DEFAULT_ACTION_BY_CLASS[reason_class]
    delay = 0
    if action == "ACT_RETRY":
        mode = taxonomy.REASON_CLASS_POLICY[reason_class].retry_mode
        if mode == "scheduled":
            delay = MIN_SCHEDULED_RETRY_DELAY_SECONDS
    channel = DEFAULT_CHANNEL.get(action, "none")
    step = PlanStep(
        step_index=0,
        action=action,
        channel=channel,
        delay_seconds=delay,
        stop_conditions=list(DEFAULT_STOP_CONDITIONS),
        rationale=(
            "deterministic reason-class default (NFR-2): no cached or live "
            "planner response is available for this signature, so the first "
            "action comes from the same lookup table arm B uses rather than "
            "waiting on a model. This is a real fallback exercised on every "
            "cold signature, not dead code."
        ),
    )
    return RecoveryPlan(
        signature=canonical.planner_signature(features),
        steps=[step],
        rationale="NFR-2 deterministic fallback -- see the step rationale.",
        llm_call_ids=[],
    )


# --------------------------------------------------------------------------
# Parsing an LLM reply into a RecoveryPlan
# --------------------------------------------------------------------------


class PlanParseError(ValueError):
    """The model's reply could not be coerced into a single valid step."""


def _coerce_step(raw: Any, index: int) -> Optional[PlanStep]:
    """One step, tolerantly. ``None`` if nothing usable survives.

    Tolerant where tolerance is harmless (an unknown stop condition is passed
    through as free text; a missing rationale becomes empty) and strict where
    it is not (an action outside ``canonical.ACTIONS`` or a channel outside
    ``schemas.Channel`` cannot be silently substituted, because that would be
    inventing what the model said rather than reading it). ``step_index`` is
    always the caller's own dense count of *surviving* steps, never whatever
    the model wrote: a model that emits steps out of order, with gaps, or with
    one unreadable step among readable ones would otherwise leave the kept
    steps out of order or with a hole at position zero, neither of which says
    anything about the plan's substance.
    """
    if not isinstance(raw, dict):
        return None
    stop_conditions = raw.get("stop_conditions") or []
    if isinstance(stop_conditions, str):
        stop_conditions = [stop_conditions]
    try:
        return PlanStep(
            step_index=index,
            action=str(raw.get("action", "")),
            channel=str(raw.get("channel", "none") or "none"),
            delay_seconds=max(0, int(raw.get("delay_seconds", 0) or 0)),
            expected_value_paise=max(0, int(raw.get("expected_value_paise", 0) or 0)),
            cost_paise=max(0, int(raw.get("cost_paise", 0) or 0)),
            stop_conditions=[str(c) for c in stop_conditions if c],
            rationale=str(raw.get("rationale", ""))[:600],
        )
    except (ValueError, TypeError):
        # Includes pydantic's ValidationError, a ValueError subclass: an
        # unknown action or channel, a negative that survived the max(0, ...)
        # guard on a non-numeric input, and so on. One bad step should not
        # sink a plan whose other steps are fine, so the caller drops it
        # rather than aborting the whole parse.
        return None


def _parse_plan(raw: Any, features: Dict[str, Any], call_id: str) -> RecoveryPlan:
    """Coerce a model's JSON reply into a ``RecoveryPlan``. Raises on failure.

    Raising (rather than returning an empty plan) is deliberate: an empty plan
    with ``llm_call_ids=[call_id]`` would look like the model chose to do
    nothing, when what actually happened is that its reply could not be read.
    Those are different findings -- PRD 6.4's ``ACT_WAIT`` is a real answer and
    a parse failure is not one -- so the caller (``Planner._build``) is the
    place that decides what a parse failure becomes, and it decides by falling
    back to ``default_plan_for``, exactly as a cache miss would.
    """
    if not isinstance(raw, dict):
        raise PlanParseError("top-level JSON is not an object")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanParseError("no steps in planner reply")

    steps: List[PlanStep] = []
    for item in raw_steps:
        step = _coerce_step(item, len(steps))
        if step is not None:
            steps.append(step)
    if not steps:
        raise PlanParseError("no step in the reply survived coercion")

    try:
        expected_value = max(0, int(raw.get("expected_value_paise", 0) or 0))
    except (TypeError, ValueError):
        expected_value = 0

    return RecoveryPlan(
        signature=canonical.planner_signature(features),
        steps=steps,
        expected_value_paise=expected_value,
        rationale=str(raw.get("rationale", ""))[:1200],
        llm_call_ids=[call_id],
    )


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


@dataclass
class PlannerStats:
    """Printed alongside the LLM cache stats on every run (PRD 9.1 point 3)."""

    #: Every call to ``plan_for``, cache hit or not. The event-level count.
    signatures_seen: int = 0
    #: Served from this Planner's own signature cache -- no prompt built, no
    #: LLM call attempted, not even a cache lookup against the LLM cache.
    cache_hits: int = 0
    #: Genuinely built from a parsed LLM response (cached or live).
    llm_built: int = 0
    #: NFR-2: no LLM response was available at all.
    fallback_built: int = 0
    #: The LLM answered but the reply could not be coerced into a plan.
    parse_failures: int = 0

    @property
    def distinct_signatures(self) -> int:
        return self.llm_built + self.fallback_built + self.parse_failures

    @property
    def memoisation_ratio(self) -> float:
        """Calls to ``plan_for`` per distinct signature actually built.

        The same efficiency figure PRD 1.6 asks for at the LLM-cache layer,
        recomputed at the plan layer: it should track the cache's own ratio
        closely, and a material gap between the two is the alarm that this
        cache and the LLM cache have stopped agreeing on what a signature is.
        """
        distinct = self.distinct_signatures or 1
        return self.signatures_seen / distinct

    def as_dict(self) -> Dict[str, Any]:
        return {
            "signatures_seen": self.signatures_seen,
            "cache_hits": self.cache_hits,
            "distinct_signatures": self.distinct_signatures,
            "llm_built": self.llm_built,
            "fallback_built": self.fallback_built,
            "parse_failures": self.parse_failures,
            "memoisation_ratio": round(self.memoisation_ratio, 1),
        }


# --------------------------------------------------------------------------
# The planner
# --------------------------------------------------------------------------


class Planner:
    """Builds a ``RecoveryPlan`` per signature, memoised for the run's lifetime.

    One instance is meant to live for the duration of a batch (or a session);
    constructing a fresh one per event would defeat the whole point of the
    cache. ``eval.arms`` holds a lazily-constructed default instance for
    exactly that reason -- see its module docstring.
    """

    def __init__(self, client: Any, *, tier: str = "fast") -> None:
        self.client = client
        self.tier = tier
        self._cache: Dict[str, RecoveryPlan] = {}
        #: One entry per distinct signature, in first-seen order, with the
        #: event time (if given) that first produced it. Consumed by
        #: ``pramaan.execute.runner`` to write one PLAN ledger row per
        #: distinct plan -- the artefact ``LEDGER_KINDS`` gained a writer for
        #: on Day 5.
        self.newly_built: List[Tuple[str, RecoveryPlan, Optional[str]]] = []
        self.stats = PlannerStats()

    def plan_for(
        self,
        features: Dict[str, Any],
        *,
        situation_ts: Optional[str] = None,
    ) -> RecoveryPlan:
        """The plan for this signature. Built once; every later call is a hit.

        ``situation_ts`` is metadata only -- an event time carried *alongside*
        the signature for ledger timestamping, per PRD 9.1's rule that an
        identifier (and a timestamp is exactly the high-cardinality kind PRD
        9.1 names) travels beside a prompt and never inside one. It plays no
        part in the cache key or the prompt.
        """
        canonical.validate_features(features)
        signature = canonical.planner_signature(features)
        self.stats.signatures_seen += 1

        cached = self._cache.get(signature)
        if cached is not None:
            self.stats.cache_hits += 1
            return cached

        plan = self._build(features, signature)
        self._cache[signature] = plan
        self.newly_built.append((signature, plan, situation_ts))
        return plan

    def _build(self, features: Dict[str, Any], signature: str) -> RecoveryPlan:
        prompt = build_planner_prompt(features)
        try:
            response = self.client.call(
                prompt,
                tier=self.tier,
                schema={"type": "object"},
                temperature=0.0,
                max_tokens=MAX_COMPLETION_TOKENS,
            )
        except CacheMiss:
            self.stats.fallback_built += 1
            return default_plan_for(features)

        try:
            plan = _parse_plan(response.json(), features, response.call_id)
        except Exception:  # noqa: BLE001 -- any unreadable reply is one case
            self.stats.parse_failures += 1
            fallback = default_plan_for(features)
            # The network call still happened and still cost tokens; recording
            # its call_id is what lets a reviewer find the malformed reply in
            # the committed cache rather than the failure vanishing into a
            # silently-substituted plan.
            return fallback.model_copy(update={"llm_call_ids": [response.call_id]})

        self.stats.llm_built += 1
        return plan

    def known_signatures(self) -> Tuple[str, ...]:
        return tuple(self._cache)
