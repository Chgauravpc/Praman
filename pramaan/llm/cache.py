"""The on-disk LLM response cache. Infrastructure, not an optimisation.

BUILD-PLAN 1.7, rule 2. Three consequences follow from building this on Day 1,
before any caller exists:

1. Re-runs cost zero tokens, so iterating on the pipeline does not burn the
   free-tier daily allowance.
2. The committed cache is what makes ``make demo`` keyless (F10, NFR-4). A judge
   with a thousand submissions will not provision API keys for one of them.
3. A prompt edit invalidates only the entries whose prompts changed, so the cost
   of a prompt change is visible as a miss count rather than hidden.

Keyed on ``sha256(model + params + full prompt)`` per PRD 9. Nothing else --
notably not a timestamp, a request id, or an event id, any of which would make
every key unique and quietly turn a 30x memoisation ratio into 1x.

**Nothing time-varying is stored in a cache file.** No ``created_at``, no
latency. Two reasons: the files are committed, so a timestamp would produce a
diff on every regeneration and make the cache unreviewable; and a cached record
is meant to be a pure function of its key.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from pramaan.canonical import canonical_json, sha256_hex


def cache_key(model: str, params: Dict[str, Any], prompt: str) -> str:
    """The cache key, and also the ``llm_call_id`` recorded in the ledger.

    One identifier doing both jobs is deliberate. PRD 12.2 gives every ledger row
    an ``llm_call_ids[]`` field linking an artifact to the call that produced it;
    if that id *is* the cache key, then a reviewer holding the ledger can look up
    the exact prompt and the exact response from the committed repo. A claim
    becomes checkable rather than merely logged, which is what the receipt
    auditor (Day 4) needs in order to strip fabricated claims.
    """
    return sha256_hex(canonical_json({"model": model, "params": params, "prompt": prompt}))


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    #: Prompt+completion tokens that a hit did not have to spend, taken from the
    #: usage recorded when the entry was first written. This is the number that
    #: makes the memoisation argument concrete instead of rhetorical.
    tokens_saved: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return (self.hits / self.lookups) if self.lookups else 0.0

    @property
    def memoisation_ratio(self) -> float:
        """Lookups per distinct prompt actually sent to a model.

        PRD 1.6 asks for this to be reported as an efficiency result rather than
        buried, because "790 LLM calls for 6,000 events" reads as *barely uses
        AI* until you say it the right way round: the LLM makes every judgment in
        the system, and makes each one once per distinct situation instead of
        re-deriving the same conclusion six thousand times. A human ops lead does
        not re-think policy for every ticket either.
        """
        distinct = self.misses or 1
        return self.lookups / distinct

    def as_dict(self) -> Dict[str, Any]:
        return {
            "lookups": self.lookups,
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate_pct": round(self.hit_rate * 100, 1),
            "memoisation_ratio": round(self.memoisation_ratio, 1),
            "tokens_saved": self.tokens_saved,
        }


class CacheMiss(RuntimeError):
    """Raised when a cache miss cannot be served and no network call is allowed.

    The message is written for the person who will actually hit this: a reviewer
    who cloned the repo, ran ``make demo``, and would otherwise conclude the
    project is broken.
    """


@dataclass
class LLMCache:
    directory: Path
    stats: CacheStats = field(default_factory=CacheStats)

    def path_for(self, key: str) -> Path:
        # Sharded on the first two hex characters. A 6,000-event batch produces
        # only a few hundred entries, but a flat directory of thousands of files
        # is slow to browse on Windows and unpleasant to review on GitHub.
        return self.directory / key[:2] / ("%s.json" % key)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(key)
        if not path.exists():
            self.stats.misses += 1
            return None
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
        self.stats.hits += 1
        self.stats.tokens_saved += int(record.get("usage", {}).get("total_tokens", 0) or 0)
        return record

    def peek(self, key: str) -> Optional[Dict[str, Any]]:
        """Read without touching the counters.

        Used when probing a fallback model's key during offline replay: a probe
        that misses is not a real cache miss, and counting it would corrupt the
        hit rate that PRD 9.1 asks to be reported as the alarm signal.
        """
        path = self.path_for(key)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def put(
        self,
        key: str,
        *,
        model: str,
        tier: str,
        provider: str,
        params: Dict[str, Any],
        prompt: str,
        response: str,
        usage: Dict[str, int],
    ) -> Path:
        """Write an entry. Stores the prompt in full, on purpose.

        It roughly doubles the cache size and it is worth it: a reviewer can read
        exactly what the model was asked, which is the difference between a
        reproducible claim and a stored number. It also makes a canonicalisation
        regression visible by inspection -- an identifier in a committed prompt
        is something you can see in a diff.
        """
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "key": key,
            "model": model,
            "tier": tier,
            "provider": provider,
            "params": params,
            "prompt": prompt,
            "response": response,
            "usage": usage,
        }
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            # indent=2 with sorted keys: readable in a PR, and still byte-stable.
            json.dump(record, handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
        self.stats.writes += 1
        return path

    def entry_count(self) -> int:
        return sum(1 for _ in self.directory.glob("*/*.json"))
