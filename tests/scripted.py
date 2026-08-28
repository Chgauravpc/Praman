"""A canned LLM, for driving the investigator loop with no key and no tokens.

Not a mock in the usual sense: it returns a real ``LLMResponse``, so every code
path the loop takes -- JSON extraction, usage accounting, cache-hit accounting --
is the production path. Only the network is absent.

That distinction matters for what the tests can claim. A test that stubbed out
``investigate`` itself would prove nothing about the loop. These tests drive the
actual loop, the actual tool belt, the actual projected database and the actual
auditor; the model is the only thing replaced, and it is replaced with a script
precisely so that the *harness* is what is under test.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from pramaan.llm.client import LLMResponse


class ScriptedLLM:
    """Replays a fixed list of responses, one per call.

    ``responses`` may be JSON-serialisable objects (encoded here) or raw strings,
    the latter so a test can hand back deliberately malformed output and check the
    loop survives it.
    """

    def __init__(self, responses: Sequence[Any], *, tokens_per_call: int = 1_000) -> None:
        self._responses = list(responses)
        self._tokens = tokens_per_call
        self.prompts: List[str] = []
        self.calls = 0

    def call(
        self,
        prompt: str,
        tier: str = "fast",
        schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        index = self.calls
        self.calls += 1
        if index < len(self._responses):
            item = self._responses[index]
        else:
            # Past the end of the script the model says nothing useful. A test
            # that runs off the end should hit the loop's own bounds rather than
            # an IndexError, so this is deliberately a legal-but-useless reply.
            item = {"thought": "the script is exhausted"}
        text = item if isinstance(item, str) else json.dumps(item)
        return LLMResponse(
            text=text,
            call_id="scripted_%03d" % self.calls,
            model="scripted",
            provider="scripted",
            tier=tier,
            cache_hit=False,
            usage={
                "prompt_tokens": self._tokens,
                "completion_tokens": 0,
                "total_tokens": self._tokens,
            },
        )

    def stats(self) -> Dict[str, Any]:
        return {"cache": {}, "tokens": {"network_calls": self.calls}}


def tool_call(tool: str, **args: Any) -> Dict[str, Any]:
    return {"thought": "testing %s" % tool, "tool": tool, "args": args}


def conclusion(
    diagnosis_class: str,
    claims: Sequence[Dict[str, Any]],
    *,
    summary: str = "a summary",
    confidence: float = 0.7,
    falsifiable_by: str = "if the same pattern holds outside the window this is wrong",
) -> Dict[str, Any]:
    return {
        "thought": "concluding",
        "diagnosis": {
            "diagnosis_class": diagnosis_class,
            "summary": summary,
            "confidence": confidence,
            "falsifiable_by": falsifiable_by,
            "claims": list(claims),
        },
    }
