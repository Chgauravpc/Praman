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

Day 2 of 8. **The spine and the compliance gate are complete; the intelligence is
not.** This section is accurate rather than aspirational, and is updated as days
land.

**Working:**

- `RiskEvent` — the abstraction all five event types normalise into
- SQLite event store, idempotent on `event_id` (webhooks are at-least-once)
- Hash-chained, append-only ledger with tamper detection
- **The policy envelope.** R1–R11 with an instrument and a citation grade each,
  the `(legal_context × channel × hour)` window matrix, reversibility tiers
  T0–T4, the eight decline-reason guardrails G1–G8, and the seven stopping rules
  S1–S7 as independent predicates. `judge(step, context)` returns
  ALLOW / AMEND / REJECT and always names the rule
- LLM client and committed response cache — provider router, tier mapping, 429
  backoff honouring `Retry-After`, failover after consecutive 429s
- Seeded simulator: payment failures weighted by the real reason distribution,
  each carrying a latent counterfactual
- Canonical prompt construction, with the identifier screen and its invariant test

**Not built yet:** the investigator, the planner, the executor, the four
remaining adapters, the voice channel, and the estimator that produces the
headline number. There are still no LLM calls in the pipeline — Days 1 and 2
spend zero tokens on purpose, so neither the foundation nor the safety layer
depends on a rate limit.

### The envelope, and why it was built before the LLM

The envelope is deterministic, has no clock and no I/O, and contains **zero
import edges into `pramaan/llm/`** — checked by parsing imports, plus a
subprocess check that importing it does not pull the LLM package in
transitively. Its job is to catch the LLM, so it cannot be the LLM.

Three examples of what it does, each with a test behind it:

- A mandate retry with **no T−24h pre-debit notification** is refused citing
  **R1** — and R1 measures the gap to the *debit*, not to the decision, so a
  notification sent an hour ago is fine for tomorrow's retry and not for now.
  That is why the retry scheduler and the notification scheduler have to be one
  component.
- A debt-collection contact at **19:05** is refused citing **R9**; the same
  message at **18:55** is allowed, *also citing R9*. 19:00:00 exactly is
  permitted and 19:00:01 is not, because RBI's wording prohibits contact "after
  7:00 p.m." An allow needs a citation as much as a refusal does.
- A retry on **`card_expired`** is refused as structurally futile — at any delay.
  45 of the 69 documented decline reasons cannot be resolved by a retry, and in
  the `RISK` and `ALREADY_PAID` classes retrying causes real harm rather than
  merely wasting money.

**Rule ids carry a prefix that declares their authority**, because a system that
cites a regulator for a rule it invented is worth less than one that admits which
is which: `R` regulation (each naming an instrument), `G` Razorpay
decline-reason guardrail (futility, not law), `S` stopping rule, `P` house
policy. **One of the eleven regulatory rules is verified against a primary
instrument. The other ten say so.** And one of the eleven — R6, the
one-attempt-plus-three-retries norm — cites Razorpay's own documentation rather
than a regulator's; it declares `authority="vendor"` and is excluded from the
regulator-backed count, because the argument for grading sources collapses the
moment the R namespace is padded.

On the 6,000-event batch the envelope judges every one of the deterministic
default actions and refuses none of them — which is the expected result, not an
inert gate: that map is *built* compliant, which is what makes it a fair arm B
rather than a strawman.

**Be precise about what the demo therefore demonstrates.** Those 6,000 verdicts
cite **three** rule ids — R3, G7 and R5 — out of the twenty-five the envelope
implements, because the default actions are overwhelmingly silent ones and for a
silent action almost nothing has jurisdiction. The demo proves the gate runs on
every event and records a citable verdict. It does not exercise the gate's range.

That lives in the tests: `test_envelope_matrix.py` sweeps 3,600 (action × context
× hour × reason class) combinations plus a full amendment re-judgement pass, and
`test_redteam_envelope.py` carries one engineered violation per rule R1–R11, each
asserting the rule id rather than merely the refusal. The injected catch rate is
100% — over **one** constructed case per rule. It shows each rule fires and cites
itself; it is not a measure of how many *ways* each rule can be violated.

## Try it

```bash
make demo        # 200-event dev batch, keyless, no network call
make demo-full   # 6,000-event batch (sized from a power calculation)
make test        # 232 tests, including the invariants below
make verify      # tests, plus a byte-identical-output check across two runs
```

`make demo` prints the ingest result, the envelope's verdict on every event with
the rule each one cited, the ledger head hash, a live tamper demonstration, the
reason distribution against its published ranges, the amount bands, the arm
balance, the organic self-recovery rate, and the memoisation ratio.

## The invariants

These are properties, each with a test, and they hold at every commit.

| # | Invariant | Where |
|---|---|---|
| **I1** | Replaying the webhook stream twice produces an identical ledger | `tests/test_idempotent_replay.py` |
| **I2** | Two different events sharing a signature produce **byte-identical prompt bytes** | `tests/test_prompt_canonical.py` |
| **I3** | The envelope returns a verdict **and a rule id** for every action × context | `tests/test_envelope_matrix.py` |
| **I4** | Every rule R1–R11 catches its engineered violation | `tests/test_redteam_envelope.py` |
| **I7** | The hash chain detects any row mutation or reordering. **Truncation of the tail needs the row-count or head anchor** — a shortened chain is internally perfect, so re-hashing cannot see it | `tests/test_ledger_chain.py` |
| **I8** | Same seed and same cache → byte-identical output | `make verify` |
| **I9** | `make demo` completes with every API key unset | `make demo` |

I3 is enumerated from the vocabularies themselves — every action × every legal
context × twelve regulatory-edge timestamps × every reason class, 3,600
combinations — so adding an action or a channel without judging it is a test
failure rather than a silent hole in the gate.

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
│   ├── envelope/        the compliance gate — no LLM, by construction
│   │   ├── rules.py       R1–R11, each with its instrument and grade
│   │   ├── windows.py     the (context × channel × hour) matrix, as a table
│   │   ├── reason_map.py  G1–G8, the decline-reason guardrails
│   │   ├── tiers.py       T0–T4 reversibility and the gates each one clears
│   │   ├── stopping.py    S1–S7 as independent predicates
│   │   └── judge.py       the evaluation order, and why that order matters
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
- **Ten of the eleven regulatory provisions are graded [B]** — drawn from
  secondary summaries of a named primary instrument, consistent across sources,
  and not yet matched to a clause. **R9 alone is [A]:** RBI/2022-23/108,
  `DOR.ORG.REC.65/21.04.158/2022-23`, 12 August 2022 — and that reading is one
  person's, unreviewed. The TRAI 09:00–21:00 figure everybody quotes is the one
  that could *not* be traced to a primary clause, which was the opposite of what
  was expected.

  The grade is recorded *in the rule* (`envelope/rules.py`), and the code refuses
  at import to let a rule claim [A] without quoting the operative words. **That
  guard was not enough**: the first version of this section said "nine of eleven"
  and `rules.py` said "two of eleven", while the dict itself graded one — the
  guard protects the data structure and had nothing to say about the prose beside
  it. The counts in this file are now derived-checked against the dict by
  `test_the_published_grade_counts_match_the_prose`, because a compliance count
  is a claim and claims need mechanisms rather than care.
- **R6 sits in the regulatory namespace and cites a vendor document.** The
  retry-cap norm comes from Razorpay's subscription docs, not a regulator. It
  keeps its `R6` id for cross-referencing and declares the discrepancy in
  `RULE_SOURCES`; it is the one rule where the namespace and the source
  disagree, and it is labelled rather than left to be found.
- **Whether a payment-retry message is a "service" or a "promotional"
  communication is a legal judgment, and this repo assumes the former.** It is
  the single biggest compliance assumption in the system, because the service
  classification is what buys access to the 19:00–22:00 failure peak — the hours
  when the most money is at risk. It is stated in `envelope/windows.py` rather
  than buried, and it needs a lawyer and a correctly-categorised DLT template,
  not more code.
- **Some thresholds are ours, not a regulator's** — the ₹500 floor below which a
  voice call is not worth placing, the ₹5,000 ceiling above which a concession
  needs human approval, the per-counterparty contact budget. Every one of them
  carries a `P` rule id precisely so that nobody mistakes a preference for an
  obligation.
