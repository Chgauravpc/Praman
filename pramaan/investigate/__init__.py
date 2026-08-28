"""The investigator: an LLM that writes its own queries and cannot assert
anything it has not checked.

Three modules, and the order they are listed in is the order they matter in:

``tools.py``
    A read-only tool belt over a *projection* of the event store. Pure functions,
    each returning a hashed, logged ``ToolResult``.
``receipts.py``
    The deterministic auditor. Strips any claim whose receipt does not check out,
    and downgrades the diagnosis to UNSUPPORTED if too little survives. This is
    the module that makes an LLM diagnosis trustworthy, and it contains no LLM.
``agent.py``
    The loop. Bounded turns, bounded tokens, structured output.

The dependency direction is deliberate: ``receipts`` imports ``tools`` and knows
nothing about ``agent``. The auditor can therefore be tested, and is tested,
against hand-fabricated diagnoses with no model involved at all -- which is the
only way its guarantee means anything.
"""
