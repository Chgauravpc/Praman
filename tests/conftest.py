"""Make the offline default the *default*, not a thing you have to remember.

``make test`` sets ``PRAMAAN_LLM_OFFLINE=1``, and every test that builds its own
``Config`` passes ``llm_offline=True``. But some code under test reaches the
planner without either -- ``resolve_batch(events)`` with no ``planner=`` falls
back to a shared default instance, which calls ``load_config()``, which reads
this variable from the environment. So a bare ``pytest`` (which is what a
reviewer types) resolves six thousand events against a *live* LLM: it spends
money, it takes twenty minutes instead of one second, and it writes cache
entries into ``fixtures/`` that were never part of the committed replay set.

Measured, not assumed: with the variable set, ``resolve_batch`` over the full
five-type batch takes 1.1 seconds; without it, the same call had not finished
after twenty minutes and was still writing new cache files.

Nothing here overrides an explicit choice. A test that wants the online path
constructs ``Config(llm_offline=False)`` directly, which never consults the
environment. This only closes the hole where *no one* chose.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _no_network_by_default() -> None:
    os.environ.setdefault("PRAMAAN_LLM_OFFLINE", "1")
