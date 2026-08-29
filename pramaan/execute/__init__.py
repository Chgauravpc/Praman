"""Execution -- the unglamorous parts that decide trust (PRD 12.1).

Idempotency, locks and the terminal-state guard live here because they are the
difference between a demo and something a payments company would let touch
production, and none of them are interesting until the day something can
actually be double-executed. That day is Day 5.

``locks.py``     per-counterparty advisory lock with a TTL, so two detections
                  cannot both fire on one payer at once.
``razorpay.py``   the Razorpay TEST-mode client, an idempotency key derived
                  exactly as PRD 12.1 specifies
                  (``hash(payment_id, action_type, attempt_ordinal)``), and the
                  terminal-state guard that re-reads state before acting and
                  aborts on ``order_already_paid`` (S1).
``runner.py``     plan -> envelope -> execute, wired end to end. Shadow mode is
                  the default: everything up to and including the envelope
                  runs, and nothing is sent.
"""
from __future__ import annotations
