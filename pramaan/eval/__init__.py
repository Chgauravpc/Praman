"""Measurement -- the layer that turns actions into a defensible number.

Three modules, and the split is by responsibility rather than by convenience:

``arms``      who is in which arm, and what each arm is permitted to do
``resolve``   run an arm's policy, ask the oracle, write the ledger rows
``metrics``   rates, money, bootstrap intervals, costs, sensitivity
``bootstrap`` the interval machinery, separated so it can be tested against
              known answers rather than only against this project's own data

Nothing here imports ``pramaan.llm``. Day 3's whole point is a headline number
that no rate limit can block, and the property is checked the same way Day 2
checked the envelope: by parsing imports, not by grepping for a string.
"""
from pramaan.eval.arms import ARM_POLICIES, ArmAssigner, arm_step
from pramaan.eval.metrics import BatchMetrics, compute_metrics
from pramaan.eval.resolve import OBSERVATION_WINDOW_SECONDS, resolve_batch

__all__ = [
    "ARM_POLICIES",
    "ArmAssigner",
    "arm_step",
    "BatchMetrics",
    "compute_metrics",
    "OBSERVATION_WINDOW_SECONDS",
    "resolve_batch",
]
