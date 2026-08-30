# QUESTIONS WE EXPECT — pre-answered

The questions a payments panel actually asks, each answered with the **mechanism**
and where to read it, not a claim.

---

### "How do you know it was *you* that recovered the money, and not the customer retrying anyway?"

The whole project exists for this question. Razorpay's own webhook docs warn that
`payment.failed` is frequently followed by `payment.captured` for the same
transaction — customers fix a wrong UPI PIN and retry inside their banking app. So
**gross recovered-rupees is not a measure of an agent's value.**

Every event is randomly assigned to one of three arms — **A** control (detected,
logged, not acted on), **B** rules-only, **C** LLM-planned — stratified on (event
type × amount band × segment). The reported number is **C − A: +17.49pp of at-risk
events, 95% CI [+14.64, +20.45]** (BCa bootstrap, 10,000 resamples), which is
₹7,44,967 incremental against ₹59,91,945 gross. Arm A *is* the population that
recovers on its own, measured. → `eval/`, `EVALUATION.md`.

And the estimator itself is validated: in simulation both potential outcomes are
known, so the true effect is exact, and `tests/test_estimator_unbiased.py` shows
the holdout estimate is unbiased over 60 batches **and never reads the
counterfactual** — so the agreement is not circular.

### "What stops it from spamming people?"

Seven stopping rules, each with a numeric trigger and a unit test, plus the
reversibility tiers. **S2** caps contacts per counterparty over a rolling window;
**R8** hard-caps unsolicited voice calls at 3/day/number; **R9** permits collection
contact only 08:00–19:00; **S3** pauses all contact once a promise is recorded;
**S5** halts the whole campaign if the merchant's complaint rate crosses a
threshold; **S6** stops when the next step costs more than it can return. On the
full batch the system contacts **96 of 6,000** counterparties. → `SAFETY.md`,
`envelope/stopping.py`.

### "What if the LLM hallucinates a root cause?"

Two independent mechanisms, neither of which trusts the model.

1. **Tool-call receipts.** The investigator writes its own SQL against a read-only
   projected database and **cannot assert anything it has not checked** — a
   deterministic receipt auditor (no LLM in it) strips any claim not backed by a
   real tool call. The harness recomputes each receipt's hash from the stored
   result rather than trusting a digest the model transcribed, so a fabricated
   `call_id` or an edited diagnosis is caught. Receipt coverage on the batch is
   **100%**. → `investigate/receipts.py`.
2. **The canary.** After a diagnosis, a canary re-checks the specific structured
   claim that drives the action (which segment a real `decompose` call names as
   broken) against ground truth the agent's tools structurally cannot reach. On the
   real injected incident it **confirmed** (tier2 rate, tier3 mix, matching the
   spec exactly); a wrong diagnosis is **refuted** and writes a `RETRACTION` row
   with the contradicting evidence. → `investigate/canary.py`.

### "Webhooks arrive out of order and more than once. What happens?"

The ledger is **event-sourced and idempotent on `event_id`** — replaying the whole
stream twice (shuffled) produces a **byte-identical** ledger, and there is a test
(`I1`). Every execute call carries an idempotency key
`hash(payment_id, action_type, attempt_ordinal)`, verified against the real
Razorpay TEST API to fire exactly once for a repeated key. A per-counterparty lock
stops two detections firing on one payer, and a **terminal-state guard** re-reads
order status before acting and aborts on `order_already_paid` (S1). →
`execute/`, `tests/test_idempotent_replay.py`.

### "What does it cost per rupee recovered?"

**₹0.0003 per incremental rupee** — cost ₹214.40 against ₹7,44,967 incremental.
Retries are free; only `ACT_MESSAGE` / `ACT_VOICE` carry a per-contact cost, and
the planner's key move is an *immediate, silent, server-side retry* where the
lookup table waits — recovering the payment at zero contact cost. Cost is charged
against **incremental**, never gross (gross would flatter the ratio because most of
the denominator was coming back anyway). → the `execute` shadow report, `EVALUATION.md`.

### "Where does it fail? What are you not showing me?"

Named before you find them, in `LIMITATIONS.md`:

- **SMS/WhatsApp transport is not implemented** — no send path exists, so nothing
  is presented as sent; voice is real Sarvam over local audio, not a dialed call.
- **The reflection / playbook-learning loop is design-only** — the verification
  gate (the three-arm harness) is built; the generator that proposes new arms is
  not.
- **The workload is synthetic** ([C]); **ten of eleven rules are graded [B]**
  (R9 alone [A]); and **the simulator, the policy and the injected incident were
  written by the same hand** — a real threat to validity, stated as one.

The daily record of what actually broke, and the mechanism added so it could not
recur, is in `FAILURES.md` — including the Day-4 investigator bug that stayed hidden
for four days because the live run never completed a write, and was caught on ship
day by a coverage test that drives every ledger writer.

### "Why let an LLM near the money at all?"

It isn't, in three places, each for a stated engineering reason: **money movement**
is LLM-free (idempotency — a non-deterministic component can double-charge),
**arm assignment and estimation** are LLM-free (verifiability — an LLM near
randomisation invalidates the experiment), and **the policy envelope** is LLM-free
(citability — a compliance decision that cannot name its rule is not one, and this
layer's job is to catch the LLM). The published **organic planner violation rate is
0.0%**, reported separately from the redteam's 100% catch on engineered violations.
→ `DECISIONS.md` (the three-absences ADR).

### "Is the 100% redteam catch rate meaningful?"

It is honest about its own scope: **one** engineered violation per rule R1–R11 (plus
G1–G8), each asserting the rule id fires — so it proves every rule works and cites
itself, **not** how many *ways* each can be violated. That distinction is in the
README and `SAFETY.md` rather than left for you to infer.
