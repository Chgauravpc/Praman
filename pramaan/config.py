"""Configuration -- one place, loaded from .env, never scattered.

Two properties matter here.

**No wall-clock, anywhere.** There is deliberately no ``now()`` in this module
and none anywhere in the write path. Every timestamp in the system is an *event*
time, carried by the event itself. A ``datetime.now()`` at ledger-write time
would make the ledger hash depend on when the run happened, and NFR-3 (same seed
plus same cache produces a byte-identical ledger) would be unsatisfiable.

**Keys are optional.** ``make demo`` must complete with every API key unset
(NFR-4, invariant I9). Nothing here raises on a missing key; the LLM client
decides, at call time, whether a missing key matters -- and with a warm cache it
does not.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

#: Repo root -- this file is <root>/pramaan/config.py.
ROOT = Path(__file__).resolve().parent.parent

#: The committed LLM response cache. This is infrastructure, not an
#: optimisation (BUILD-PLAN 1.7 rule 2): it is what makes a keyless demo produce
#: a real number, and it is committed to the repo on purpose (F10).
LLM_CACHE_DIR = ROOT / "fixtures" / "llm_cache"

#: Everything a run writes. Gitignored -- regenerable from seed plus cache.
BUILD_DIR = ROOT / "build"

#: Golden files under test (NFR-6): behaviour changes must surface as
#: reviewable diffs rather than as a number quietly moving.
GOLDEN_DIR = ROOT / "tests" / "golden"

#: The simulator's epoch. A *constant*, not a wall-clock read: the whole
#: synthetic stream is positioned relative to this, so the same seed produces
#: the same timestamps on any machine on any day. Changing it changes every
#: hour_bucket and therefore every planner signature.
SIM_EPOCH = "2026-08-01T00:00:00+05:30"

_ENV_LOADED = False


def _load_env() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(ROOT / ".env")
        _ENV_LOADED = True


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError("%s must be an integer, got %r" % (name, raw)) from exc


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    seed: int
    mode: str            # shadow | live  (live still means Razorpay TEST mode)
    llm_offline: bool    # True: a cache miss is a hard error, never a network call
    groq_api_key: Optional[str] = field(repr=False, default=None)
    openrouter_api_key: Optional[str] = field(repr=False, default=None)
    sarvam_api_key: Optional[str] = field(repr=False, default=None)
    razorpay_key_id: Optional[str] = field(repr=False, default=None)
    razorpay_key_secret: Optional[str] = field(repr=False, default=None)

    @property
    def has_any_llm_key(self) -> bool:
        return bool(self.groq_api_key or self.openrouter_api_key)

    def guard_test_mode(self) -> None:
        """Refuse to run against live Razorpay credentials. NFR-7, and F13.

        A live key in this project would be a genuine incident, not an
        inconvenience, so the check is a hard failure rather than a warning.
        """
        key = self.razorpay_key_id or ""
        if key and not key.startswith("rzp_test_"):
            raise RuntimeError(
                "RAZORPAY_KEY_ID=%r is not a test-mode key. This project runs in "
                "Razorpay test mode only (NFR-7)." % key[:12]
            )


def load_config() -> Config:
    _load_env()
    cfg = Config(
        seed=_env_int("PRAMAAN_SEED", 42),
        mode=os.environ.get("PRAMAAN_MODE", "shadow").strip() or "shadow",
        llm_offline=_env_flag("PRAMAAN_LLM_OFFLINE", False),
        groq_api_key=os.environ.get("GROQ_API_KEY") or None,
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
        sarvam_api_key=os.environ.get("SARVAM_API_KEY") or None,
        razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID") or None,
        razorpay_key_secret=os.environ.get("RAZORPAY_KEY_SECRET") or None,
    )
    if cfg.mode not in ("shadow", "live"):
        raise ValueError("PRAMAAN_MODE must be shadow or live, got %r" % cfg.mode)
    cfg.guard_test_mode()
    return cfg
