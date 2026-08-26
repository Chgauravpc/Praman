# Pramaan

An AI revenue-recovery agent that detects revenue at risk, diagnoses why, chooses
a bounded intervention, executes it — and proves against a randomised control arm
which rupees it actually caused to be recovered.

```
git clone <this repo> && cd pramaan
pip install -r requirements.txt
make demo            # no API key required
```

---

## The problem with "recovered revenue"

Razorpay's own webhook documentation warns that `payment.failed` is often
followed by `payment.captured` for the same transaction. Customers fix a wrong
UPI PIN and retry inside their banking app, without anyone contacting them.

So a large share of revenue that recovery tools report as "recovered" was never
lost. Gross recovered-rupees is therefore not a measure of an agent's value; it
is a category error — it credits the agent for money that was coming back anyway.

On the seeded 6,000-event batch in this repo, **30.4% of failed payments recover
with no intervention at all.** Any tool reporting gross recovery on this workload
would over-claim by roughly that much.

Pramaan's headline metric is therefore **incremental** recovery, measured against
a randomised control arm that is detected, diagnosed, logged — and deliberately
not acted on.

## Three arms, and the second contrast is the interesting one

| Arm | Behaviour | Answers |
|---|---|---|
| **A** control | detected, diagnosed, logged, **not acted on** | Does any of this recover money at all? |
| **B** rules-only | deterministic reason-code → action map, no LLM | Does a lookup table already solve this? |
| **C** LLM-planned | investigator → planner → policy envelope | Does the LLM earn its place? |

`B − A` is the incremental recovery number. `C − B` is the measured answer to
"why use AI here" — an ablation rather than an assertion, and publishable in
either direction. If `C − B ≈ 0`, the honest finding is that a lookup table
matches the LLM for choosing the *action*, and the LLM's value lies in diagnosis
and conversation instead.

Arm assignment happens at detection, before the settle window, stratified on
(event type × amount band × segment) — because order amounts are log-normal and
a handful of large payments would otherwise stack into one arm by luck.

## Where the LLM is, and where it deliberately is not

The LLM makes every judgment in the system: diagnosis, planning, channel, timing,
negotiation. It is absent from exactly three places, each for a stated
engineering reason rather than a general distrust of models:

| Layer | LLM? | Reason |
|---|---|---|
| Money movement | **No** | Idempotency. A non-deterministic component here can double-charge. |
| Arm assignment and estimation | **No** | Verifiability. An LLM near randomisation invalidates the experiment. |
| The policy envelope | **No** | Citability. A compliance decision that cannot name the rule it enforced is not a compliance decision — and this layer's job is to catch the LLM, so it cannot be the same LLM. |

The LLM also runs once per *situation*, not once per event. On the 6,000-event
batch there are 273 distinct planner signatures — a **22× memoisation ratio**. A
human ops lead does not re-think policy for every ticket either.

---

## Build status

Day 1 of 8. **The spine is complete; the intelligence is not.** This section is
accurate rather than aspirational, and will be updated as days land.

**Working:**

- `RiskEvent` — the abstraction all five event types normalise into
- SQLite event store, idempotent on `event_id` (webhooks are at-least-once)
- Hash-chained, append-only ledger with tamper detection
- LLM client and committed response cache — provider router, tier mapping, 429
  backoff honouring `Retry-After`, failover after consecutive 429s
- Seeded simulator: payment failures weighted by the real reason distribution,
  each carrying a latent counterfactual
- Canonical prompt construction, with the identifier screen and its invariant test

**Not built yet:** the policy envelope (R1–R11), the investigator, the planner,
the executor, the four remaining adapters, the voice channel, and the estimator
that produces the headline number. There are no LLM calls in the pipeline yet —
Day 1 spends zero tokens on purpose, so the foundation does not depend on a rate
limit.

## Try it

```bash
make demo        # 200-event dev batch, keyless, no network call
make demo-full   # 6,000-event batch (sized from a power calculation)
make test        # 140 tests, including the invariants below
make verify      # tests, plus a byte-identical-output check across two runs
```

`make demo` prints the ingest result, the ledger head hash, a live tamper
demonstration, the reason distribution against its published ranges, the amount
bands, the arm balance, the organic self-recovery rate, and the memoisation
ratio.

## The invariants

These are properties, each with a test, and they hold at every commit.

| # | Invariant | Where |
|---|---|---|
| **I1** | Replaying the webhook stream twice produces an identical ledger | `tests/test_idempotent_replay.py` |
| **I2** | Two different events sharing a signature produce **byte-identical prompt bytes** | `tests/test_prompt_canonical.py` |
| **I7** | The hash chain detects any row mutation, deletion or reordering | `tests/test_ledger_chain.py` |
| **I8** | Same seed and same cache → byte-identical output | `make verify` |
| **I9** | `make demo` completes with every API key unset | `make demo` |

I2 is the one that matters most and the reason it is a Day 1 test rather than a
Day 5 one. If a prompt embeds a `payment_id`, an exact rupee amount or a raw
timestamp, every LLM call becomes a cache miss and the token budget goes from
~800K to ~15M — silently, because the system still works. See
[DECISIONS.md](DECISIONS.md) ADR-004.

## Reproducibility

`make demo` makes no network call and needs no API key. The LLM response cache is
committed to the repo (ADR-006) and keyed on
`sha256(model + params + full prompt)`; the cache key doubles as the
`llm_call_id` recorded in the ledger, so any ledger row can be traced back to the
exact prompt and response that produced it.

There is no `datetime.now()` anywhere in the write path. Every ledger timestamp
is the event's own time, and the simulator positions its stream relative to a
constant epoch (ADR-009). `temperature=0` is used but is explicitly *not* relied
on for reproducibility — the cache is the mechanism.

One note on reading `tests/golden/ledger.jsonl`: the `payload` field is a JSON
*string* rather than a nested object. That is deliberate — the row hash is
computed over exactly those bytes, so the export shows what was actually hashed.

## Repository

```
pramaan/
├── pramaan/
│   ├── canonical.py     amount bands, signature fields, hashing primitives
│   ├── taxonomy.py      69 Razorpay decline reasons -> 10 classes
│   ├── schemas.py       Diagnosis, Claim, Receipt, RecoveryPlan, PlanStep
│   ├── config.py        .env loading; no wall-clock, keys optional
│   ├── cli.py           the demo
│   ├── sense/           RiskEvent, the event store
│   ├── ledger/          the hash chain
│   └── llm/             client, cache, prompt construction
├── sim/                 seeded generator + latent ground truth
├── tests/
├── fixtures/llm_cache/  committed — what makes the demo keyless
└── DECISIONS.md
```

## Limitations, stated up front

- **SMS, WhatsApp and voice transport are stubbed** behind a documented `Channel`
  interface. DLT registration requires a registered business entity, which is not
  obtainable for this project. No message is ever presented as having been sent.
- **Razorpay test mode only.** The config refuses to run against a key that is
  not `rzp_test_`.
- **The event stream is synthetic.** The reason distribution is drawn from PSP
  audit figures that the source itself grades as directional, and it is used for
  its shape rather than its digits.
- **Regulatory provisions R1–R11 are drawn from secondary summaries** and are
  being verified against the primary RBI and TRAI instruments before they are
  presented as legal thresholds.
