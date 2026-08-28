"""The LLM client. One interface, two providers, two tiers, zero scattered IDs.

``llm.call(prompt, tier, schema)`` is the only entry point in the codebase, per
HANDOFF 7. Model IDs live in exactly one table here, because free-tier model
lineups change without notice and a model ID grepped across nine files is a
half-day of work when Groq retires one.

**Tiers, not models.** ``strong`` is the investigator -- multi-turn tool use and
hypothesis revision, the one place reasoning quality shows. ``fast`` is the
planner, promise extraction and voice-turn policy: high volume,
schema-constrained, memoised, and latency is felt by a human on a phone call.

**Rate limits are the real constraint, not compute** (BUILD-PLAN 1). So:
a 429 is honoured via its ``Retry-After`` header rather than a blind sleep, and
after a small number of *consecutive* 429s the client fails over to the other
provider instead of sitting in a retry loop against a wall. HANDOFF 7 is explicit
that sitting in the loop is the wrong behaviour: the daily allowance does not
come back within the run.

Day 1 ships this with **no callers**. That is the plan (BUILD-PLAN Day 1, block
C): the plumbing exists, the token budget is zero, and the foundation does not
depend on a rate limit.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pramaan.config import LLM_CACHE_DIR, Config, load_config
from pramaan.llm.cache import CacheMiss, LLMCache, cache_key
from pramaan.llm.prompts import assert_no_identifiers

TIERS: Tuple[str, ...] = ("strong", "fast")

#: The only place a model ID appears.
#:
#: Ordered per tier: the first entry is tried first, and the order is fixed so
#: that offline replay is deterministic.
#:
#: **Corrected on Day 4, and the correction was overdue by twelve days.** Days
#: 1-3 named ``llama-3.3-70b-versatile`` and ``llama-3.1-8b-instant``, carried as
#: an open item in STATE.md ("model IDs are unverified; nothing has hit either
#: API"). Checked on 2026-08-28 against Groq's own deprecation page: both were
#: announced deprecated on 2026-06-17 and **shut down on 2026-08-16**. They had
#: been dead for twelve days. The first live call of the project would have
#: failed on a model that no longer exists, and the reason this cost nothing is
#: luck rather than design -- the item was scheduled for the same day the first
#: call was scheduled.
#:
#: The Groq replacements are the ones Groq's deprecation notice names. The
#: OpenRouter failovers are read from its live free-models collection on the same
#: date, and are a *rotating* roster -- the two Llama entries this file used to
#: carry are no longer on it at all, and neither is gpt-oss. So the failover
#: entries below are correct on the date stated and are not to be trusted
#: indefinitely.
#:
#: Which is why ``python -m pramaan.cli models`` exists. It asks each provider
#: what it actually serves and reports which of these IDs resolve. That turns a
#: fact with an expiry date into a check anyone can re-run, which is the only
#: durable answer to a free-tier lineup that changes without notice.
#:
#: Verified present: 2026-08-28.
MODELS: Dict[str, List[Tuple[str, str]]] = {
    # (provider, model_id)
    "strong": [
        # Groq's named replacement for llama-3.3-70b-versatile.
        ("groq", "openai/gpt-oss-120b"),
        # A 120B-class free model on OpenRouter, chosen to be comparable rather
        # than merely available: the strong tier is the investigator, and a small
        # failover model would silently change what the agent is capable of
        # reasoning about while every metric kept reporting normally.
        ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
    ],
    "fast": [
        # Groq's named replacement for llama-3.1-8b-instant.
        ("groq", "openai/gpt-oss-20b"),
        ("openrouter", "nvidia/nemotron-3.5-lightning:free"),
    ],
}

#: Where the two figures above came from, so a later reader can re-check rather
#: than re-derive.
MODEL_SOURCES: Dict[str, str] = {
    "groq": "https://console.groq.com/docs/deprecations",
    "openrouter": "https://openrouter.ai/collections/free-models",
}
MODELS_VERIFIED_ON = "2026-08-28"

PROVIDER_ENDPOINTS: Dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}

#: Consecutive 429s from one provider before failing over. Three, not thirty:
#: the free-tier wall is a daily allowance, so a fourth attempt is not going to
#: be the one that works.
FAILOVER_AFTER_429S = 3

#: Cap on a Retry-After we will actually wait out. A provider asking for ten
#: minutes is telling us the day is over; fail over instead of stalling the run.
MAX_RETRY_AFTER_SECONDS = 30


@dataclass
class TokenLedger:
    """Token accounting, so the budget is a measured number and not a hope."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    network_calls: int = 0
    rate_limited: int = 0
    failovers: int = 0
    by_tier: Dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def record(self, tier: str, usage: Dict[str, int]) -> None:
        self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        self.network_calls += 1
        self.by_tier[tier] = self.by_tier.get(tier, 0) + int(
            usage.get("total_tokens", 0) or 0
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "network_calls": self.network_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "rate_limited": self.rate_limited,
            "failovers": self.failovers,
            "by_tier": dict(sorted(self.by_tier.items())),
        }


@dataclass(frozen=True)
class LLMResponse:
    text: str
    call_id: str          # the cache key: what the ledger records
    model: str
    provider: str
    tier: str
    cache_hit: bool
    usage: Dict[str, int]

    def json(self) -> Any:
        """Parse the response as JSON.

        Small models wrap JSON in prose or a fenced block even when told not to,
        so the first and last brace are used rather than trusting the envelope.
        A parse failure is raised, not swallowed: a silently-dropped structured
        response would show up much later as a mysteriously empty plan.
        """
        text = self.text.strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in response for call %s" % self.call_id[:12])
        return json.loads(text[start : end + 1])


class LLMClient:
    def __init__(
        self,
        config: Optional[Config] = None,
        cache_dir: Path = LLM_CACHE_DIR,
        *,
        sleep=time.sleep,
    ) -> None:
        self.config = config or load_config()
        self.cache = LLMCache(cache_dir)
        self.tokens = TokenLedger()
        self._sleep = sleep  # injectable, so backoff is testable without waiting
        self._consecutive_429: Dict[str, int] = {}

    # -- the one entry point ---------------------------------------------

    def call(
        self,
        prompt: str,
        tier: str = "fast",
        schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        """Answer a prompt, from cache if possible.

        ``temperature=0`` is used but is explicitly **not** relied on for
        reproducibility -- the cache is the mechanism (PRD 9). Assuming
        temperature-zero implies determinism is a common and wrong belief, and
        DECISIONS.md says so out loud.
        """
        if tier not in TIERS:
            raise ValueError("tier must be one of %r, got %r" % (TIERS, tier))

        # A3 / F9 enforced at the chokepoint, not only where prompts are built.
        # build_planner_prompt and build_investigator_prompt already screen their
        # output, but they are not the only way a string can reach a provider --
        # any future caller that assembles a prompt by hand would bypass them.
        # Screening here makes the guarantee structural for every caller, which is
        # the whole claim in prompts.py's docstring. The cost is a few regexes per
        # call; the failure it prevents is silent (every call a cache miss, budget
        # 800K -> 15M) and would surface days later as a token overrun.
        assert_no_identifiers(prompt, context="llm.call prompt")

        params = self._params(schema, temperature, max_tokens)
        candidates = self._candidates(tier)

        # 1. Exact key for the preferred model.
        preferred_provider, preferred_model = candidates[0]
        key = cache_key(preferred_model, params, prompt)
        record = self.cache.get(key)
        if record is not None:
            return self._from_record(record, key, tier, cache_hit=True)

        # 2. Offline replay: a fallback model may have answered this during the
        #    live batch, so probe the remaining candidates in fixed order before
        #    giving up. Deterministic, and it means a mid-run failover does not
        #    leave a permanent hole in the reproducible demo.
        for provider, model in candidates[1:]:
            alt_key = cache_key(model, params, prompt)
            alt = self.cache.peek(alt_key)
            if alt is not None:
                self.cache.stats.hits += 1
                self.cache.stats.misses = max(0, self.cache.stats.misses - 1)
                return self._from_record(alt, alt_key, tier, cache_hit=True)

        if self.offline:
            raise CacheMiss(
                "No cached response for this prompt, and no network call is "
                "permitted (PRAMAAN_LLM_OFFLINE=1 or no API key is set).\n"
                "  tier=%s model=%s key=%s\n"
                "This is expected only if the prompt changed since the committed "
                "cache was generated. Run 'make demo-live' with GROQ_API_KEY set "
                "to regenerate, or 'git checkout fixtures/llm_cache' to restore."
                % (tier, preferred_model, key[:16])
            )

        # 3. Live call, with failover.
        return self._call_live(prompt, tier, params, candidates)

    # -- internals -------------------------------------------------------

    @property
    def offline(self) -> bool:
        """No network calls: forced by flag, or implied by having no key at all.

        The second half is what makes NFR-4 true by construction rather than by
        remembering to set an env var.
        """
        return self.config.llm_offline or not self.config.has_any_llm_key

    def _params(
        self, schema: Optional[Dict[str, Any]], temperature: float, max_tokens: int
    ) -> Dict[str, Any]:
        """The params half of the cache key.

        Only things that change the model's output belong here. The schema is
        included because it changes the request; the provider is not, because the
        model ID already identifies it and including both would split one logical
        entry across two keys.
        """
        params: Dict[str, Any] = {"temperature": temperature, "max_tokens": max_tokens}
        if schema is not None:
            params["schema"] = schema
        return params

    def _candidates(self, tier: str) -> List[Tuple[str, str]]:
        """Providers for this tier, preferred first, keyless ones dropped.

        Keeps the full list when offline: replay needs to probe every model that
        could have answered during the live batch, whether or not that provider's
        key happens to be present now.
        """
        candidates = MODELS[tier]
        if self.offline:
            return list(candidates)
        available = [(p, m) for p, m in candidates if self._api_key(p)]
        return available or list(candidates)

    def _api_key(self, provider: str) -> Optional[str]:
        return {
            "groq": self.config.groq_api_key,
            "openrouter": self.config.openrouter_api_key,
        }.get(provider)

    def _from_record(
        self, record: Dict[str, Any], key: str, tier: str, *, cache_hit: bool
    ) -> LLMResponse:
        return LLMResponse(
            text=record["response"],
            call_id=key,
            model=record.get("model", "unknown"),
            provider=record.get("provider", "cache"),
            tier=tier,
            cache_hit=cache_hit,
            usage=dict(record.get("usage", {})),
        )

    def _call_live(
        self,
        prompt: str,
        tier: str,
        params: Dict[str, Any],
        candidates: Sequence[Tuple[str, str]],
    ) -> LLMResponse:
        last_error: Optional[Exception] = None
        for index, (provider, model) in enumerate(candidates):
            if index > 0:
                self.tokens.failovers += 1
            try:
                text, usage = self._request_with_backoff(provider, model, prompt, params)
            except _RateLimited as exc:
                # This provider is walled. Move on rather than keep knocking.
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001 -- any provider fault fails over
                last_error = exc
                continue

            self.tokens.record(tier, usage)
            key = cache_key(model, params, prompt)
            self.cache.put(
                key,
                model=model,
                tier=tier,
                provider=provider,
                params=params,
                prompt=prompt,
                response=text,
                usage=usage,
            )
            return LLMResponse(
                text=text, call_id=key, model=model, provider=provider,
                tier=tier, cache_hit=False, usage=usage,
            )
        raise RuntimeError(
            "every provider for tier %s failed; last error: %s" % (tier, last_error)
        )

    def _request_with_backoff(
        self, provider: str, model: str, prompt: str, params: Dict[str, Any]
    ) -> Tuple[str, Dict[str, int]]:
        """One provider, with 429 handling. Raises _RateLimited to trigger failover."""
        import requests

        endpoint = PROVIDER_ENDPOINTS[provider]
        headers = {
            "Authorization": "Bearer %s" % (self._api_key(provider) or ""),
            "Content-Type": "application/json",
        }
        body: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": params["temperature"],
            "max_tokens": params["max_tokens"],
        }
        if "schema" in params:
            # Both providers speak the OpenAI-compatible surface. Structured
            # output is requested rather than assumed: LLMResponse.json() still
            # tolerates a model that ignores it.
            body["response_format"] = {"type": "json_object"}

        attempt = 0
        while True:
            response = requests.post(endpoint, headers=headers, json=body, timeout=90)
            if response.status_code == 429:
                self.tokens.rate_limited += 1
                self._consecutive_429[provider] = self._consecutive_429.get(provider, 0) + 1
                if self._consecutive_429[provider] >= FAILOVER_AFTER_429S:
                    raise _RateLimited(
                        "%s returned %d consecutive 429s; failing over"
                        % (provider, self._consecutive_429[provider])
                    )
                delay = self._retry_after(response, attempt)
                if delay is None:
                    raise _RateLimited(
                        "%s asked for a retry delay longer than %ds; failing over"
                        % (provider, MAX_RETRY_AFTER_SECONDS)
                    )
                self._sleep(delay)
                attempt += 1
                continue

            response.raise_for_status()
            self._consecutive_429[provider] = 0
            payload = response.json()
            text = payload["choices"][0]["message"]["content"] or ""
            usage = payload.get("usage", {}) or {}
            return text, {
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
            }

    @staticmethod
    def _retry_after(response: Any, attempt: int) -> Optional[float]:
        """Honour Retry-After when present; otherwise back off exponentially.

        Returns None when the provider is asking for longer than we are willing
        to wait, which the caller reads as "fail over now".
        """
        header = response.headers.get("Retry-After") or response.headers.get(
            "retry-after"
        )
        if header:
            try:
                delay = float(header)
            except ValueError:
                delay = 2.0 ** attempt
        else:
            delay = 2.0 ** attempt
        if delay > MAX_RETRY_AFTER_SECONDS:
            return None
        return max(0.1, delay)

    # -- reporting -------------------------------------------------------

    def available_models(self, provider: str) -> List[str]:
        """Ask a provider what it actually serves. Requires that provider's key.

        Both providers expose an OpenAI-compatible ``GET /models``. This is the
        cheapest possible answer to "is the configured ID real", it costs no
        completion tokens, and it is the thing that should have been run on Day 0.
        """
        import requests

        key = self._api_key(provider)
        if not key:
            raise RuntimeError("no API key for %s" % provider)
        endpoint = PROVIDER_ENDPOINTS[provider].replace("/chat/completions", "/models")
        response = requests.get(
            endpoint, headers={"Authorization": "Bearer %s" % key}, timeout=30
        )
        response.raise_for_status()
        payload = response.json()
        return sorted(str(item.get("id", "")) for item in payload.get("data", []))

    def verify_models(self) -> Dict[str, Any]:
        """Check every configured ID against what its provider serves.

        Returns a report rather than raising. A provider with no key is reported
        as ``skipped``, not as a failure: ``make demo`` must complete with every
        key unset (NFR-4), so a verification that raised without a key would make
        this command unusable in exactly the configuration the project promises to
        support.
        """
        report: Dict[str, Any] = {"verified_on_record": MODELS_VERIFIED_ON, "providers": {}}
        for provider in sorted(PROVIDER_ENDPOINTS):
            configured = sorted(
                {model for tier in MODELS.values() for prov, model in tier if prov == provider}
            )
            entry: Dict[str, Any] = {"configured": configured}
            if not self._api_key(provider):
                entry["status"] = "skipped -- no API key"
            else:
                try:
                    served = self.available_models(provider)
                except Exception as exc:  # noqa: BLE001 -- a report, not a raise
                    entry["status"] = "error: %s" % exc
                else:
                    entry["status"] = "checked"
                    entry["served_count"] = len(served)
                    entry["present"] = [m for m in configured if m in served]
                    entry["missing"] = [m for m in configured if m not in served]
            report["providers"][provider] = entry
        return report

    def stats(self) -> Dict[str, Any]:
        """Printed on every run.

        PRD 9.1 point 3: report the cache hit rate and the memoisation ratio
        every time. A hit rate that drops after a prompt change is the alarm --
        and without the metric on screen, the failure is invisible until the
        token budget is gone.
        """
        return {"cache": self.cache.stats.as_dict(), "tokens": self.tokens.as_dict()}


class _RateLimited(RuntimeError):
    """Internal signal: this provider is walled, try the next one."""
