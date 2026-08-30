# Pramaan — AI Revenue Recovery

<!--
Day 6 draft of the PRD §14.2 first screen (BUILD-PLAN Day 6, block E).
Everything in this fenced-off block is a placeholder for Day 8, once the full
five-event-type batch and the voice recording both exist: **every bracketed
[…] figure below is provisional and must be replaced from a real run before
submission — do not quote a number in this block as final.** The shape and
the copy are final; the digits are not.
-->

**On [N] revenue-at-risk events, spanning all five event types, Pramaan
recovered ₹[X] incremental (95% CI: ₹[Y]–₹[Z]) against a randomised control
arm. Gross recovery was ₹[3X]. Most of that was customers retrying on their
own, and I can show you the difference.**

Razorpay's own webhook docs warn that `payment.failed` is often followed by
`payment.captured` for the same transaction — customers fix a wrong UPI PIN
and retry inside their banking app. So gross recovered-rupees is not a
measure of an agent's value. This one measures against a control arm.

```
git clone <this repo> && cd pramaan && make demo      # ~90s, no API key needed, reproduces every number above
```

![`make execute-full`, keyless, reproduces the headline from the committed cache: C−A +17.49pp, C−B +16.91pp (both excluding zero), Rs 7,44,967.63 incremental, ALL CHECKS PASS](assets/demo.gif)

*(`make demo` is the ~90s keyless smoke run; the GIF above is `make execute-full`, the offline command that reproduces the incremental-recovery headline. Both need no API key.)*

[ 40-second audio: the agent calling a customer in Hinglish, negotiating a
  payment date, extracting the promise, logging it to the ledger ]

|                         |                                             |
|-------------------------|---------------------------------------------|
| Incremental recovery    | [X]% (95% CI […, …]) vs randomised holdout   |
| Investigator precision  | [X]% of hypotheses confirmed by the canary   |
| Planner violation rate  | [X]% — envelope caught 100% of them          |
| Receipt coverage        | [X]% of claims backed by a real tool call    |
| Cost per incremental ₹  | ₹[X]                                         |
| Events refused          | [N], with reasons below                      |

**All seven brief directions:** payment degradation · checkout drop-off ·
failed subscription · mandate retry · B2B receivables · Hinglish voice ·
promise-to-pay — coverage table below, seven for seven.

**The bar:** measured money ✓ · compliant escalation ✓ · stopping rules ✓ ·
audit trail ✓ — see "The invariants" and "The envelope" below.

---

## Coverage — all seven brief directions

One loop, five adapters, one channel, one cross-cutting state machine (PRD
§3) — not seven separate products. Every direction the track brief names,
with its home in this repo:

| Brief direction | Status | Component |
|---|---|---|
| **Payment degradation → root cause → recovery action** | Core | Degradation-aware simulator → investigator (`decompose`/`get_downtime`) → canary → planner → envelope |
| **Checkout drop-off recovery** | Core | `pramaan/sense/adapters/checkout.py` — abandonment-*stage*-aware (method-selection vs OTP-entry vs processing), stage-appropriate plan |
| **Failed-subscription recovery** | Core | `pramaan/sense/adapters/subscription.py` — intervenes in the `pending` window, before `halted` |
| **Mandate retry sequencer** | Core | `pramaan/sense/adapters/mandate.py` — schedule → notify at T−24h → attempt, R1/R6-governed |
| **B2B receivables chaser** | Core | `pramaan/sense/adapters/receivable.py` — Smart Collect virtual-account reconciliation feeds S1, so a paid invoice is never chased |
| **Hinglish voice recovery** | Core | `pramaan/converse/voice.py` — Sarvam STT → LLM turn policy → Sarvam TTS, **AI disclosure first (R10)**, R8/R9 windows and self-identification, S7 distress/dispute/legal stand-down, promise extracted from speech into the state machine and the ledger |
| **Promise-to-pay tracker** | Core | `pramaan/converse/promises.py` — `NONE → PROMISED → KEPT/PARTIAL/BROKEN`, per-counterparty reliability, Brier-scored calibration |

Seven for seven, all Core. The voice loop lands as `pramaan/converse/voice.py`:
`make voice` places the canonical Hinglish recovery call with **no key**, writing
the verbatim transcript (`assets/voice-transcript.md`) and its `CONVERSE`/`PROMISE`
rows to a hash-chained ledger. The **audio clip** (`assets/voice-demo.mp3`) is the
one artifact that needs a live `SARVAM_API_KEY` — `make voice-live` synthesises it;
without the key the transcript is still complete and the run exits 0.

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

### The number, as of Day 3

Arms A and B are measured. Arm C is present, holds its third of the events, and
is wired on Day 5 — its figures are **withheld** rather than printed, because an
arm that takes no action has arm A's outcomes and printing them would read as a
finding about the LLM.

> **Intent-to-treat, all 6,000 events: +0.58 pp of at-risk events, 95% CI
> [−2.27, +3.43].** The interval spans zero.
>
> **On the 25.6% of events the rules-only policy actually acts on: +9.22 pp, 95%
> CI [+4.96, +13.46].** The interval excludes zero.

Both are printed by `make demo-full`, the second labelled a pre-specified
subgroup. **The gap between them is the finding**, and its cause is measured
rather than guessed: the rules-only table answers 72.1% of volume with
`ACT_WAIT`, where the true effect is *exactly zero* — verified per event, not
asserted. So a real +3.02 pp effect is being measured through a sample in which
three-quarters of the observations are known-null.

That is unflattering to the lookup table, and it is exactly the headroom `C − B`
is measured against on Day 5 — stated now, before any LLM result exists to be
flattered by it.

Reported in three units, because two of them look the same and are not: **+0.58 pp
of at-risk events** (event-weighted), **+5.99 pp of failed value**
(value-weighted), **₹533.09 per at-risk event**. A percentage-point figure quoted
without saying which is a defect.

### The estimator is validated against ground truth, not just run

In production the counterfactual is unobservable. In simulation it is known — so
both potential outcomes are computable for every event and the true effect is an
*exact* quantity rather than an estimate. `make demo` prints the comparison:

| | true effect | estimate (95% CI) | |
|---|---|---|---|
| event-weighted | +3.02 pp | +0.58 pp [−2.27, +3.43] | covered |
| value-weighted | +6.73 pp | +5.99 pp [−7.82, +19.69] | covered |
| ₹ per at-risk event | ₹483.97 | ₹533.09 [−₹776.29, +₹2,148.75] | covered |

`tests/test_estimator_unbiased.py` goes further: the estimator is unbiased over 60
independent batches, its intervals cover at the nominal rate, and — the one that
matters most — **stripping the counterfactual from the outcomes changes no
estimate**, so the agreement above is not circular.

Intervals are **BCa bootstrap, 10,000 resamples**, never a normal approximation:
order amounts are log-normal, and the demo prints two heavy-tail signatures to
show the interval was read off the resample distribution rather than a standard
error — the money interval is 5.8× relatively wider than the rate interval, and
it is visibly asymmetric (1.23 upper/lower) where a normal interval is 1.00 by
construction.

Full method, calibration and threats to validity: **[EVALUATION.md](EVALUATION.md)**.

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

Day 3 of 8. **The spine, the compliance gate and the measurement layer are
complete; the intelligence is not.** This section is accurate rather than
aspirational, and is updated as days land.

**Zero LLM calls and zero tokens so far.** That ordering is deliberate: the
headline number cannot be blocked by a rate limit, and what Day 3 built is arm B —
the baseline the LLM has to beat from Day 5.

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

Since then: the three-arm estimator that produces the headline number, and the
**investigator** — an LLM agent with a read-only tool belt that writes its own
SQL, plus the deterministic receipt auditor that strips any claim it cannot
evidence.

**Not built yet:** the planner, the executor, the four remaining adapters, the
voice channel, and the canary. Arm C is present in every batch, holds its third
of the events, and takes no action — its figures are withheld by design until it
is wired, because an unwired arm's outcomes are identical to the control's and
printing them would read as a finding about the LLM.

**On the token count:** the pipeline still reports zero tokens consumed. Days 1–3
spend nothing on purpose, so neither the foundation nor the safety layer depends
on a rate limit. Day 4's investigator is built and tested end to end against a
scripted model; the live run needs an API key and is the one thing outstanding.
Two consequences worth being explicit about — the published receipt-coverage
figure is currently measured against a scripted model rather than a real one, and
`make investigate` stops with an actionable message rather than a number if no key
is present.

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
make investigate # the LLM investigator, from the committed cache
make models      # print the configured model IDs and check they still resolve
make test        # 438 tests, including the invariants below
make verify      # tests, plus a byte-identical-output check across two runs
```

`make models` exists because of a genuine and slightly embarrassing finding: the
two model IDs this repo carried for its first three days had been switched off by
the provider twelve days before anyone checked. Nothing caught it, because the
project had a source comment reading *"verify these"* where it needed a command.
A fact with an expiry date belongs in a query, not in a constant with a reminder
attached — so the check is now one target, and it costs no tokens.

`make investigate` runs the agent against a batch carrying one **injected**
degradation, and the interesting part is that the incident is deliberately
ambiguous. The blended failure rate rises 7.3 points, and that splits into 3.8
points of real rate shift on one segment and 3.5 points of traffic mix toward a
segment that always failed more, with nothing broken in it. An agent that reports
the blended figure has failed. The decomposition tool returns arithmetic the agent
cannot fudge, and its three terms sum to the observed change exactly.

`make demo` prints the ingest result, the envelope's verdict on every event with
the rule each one cited, the ledger head hash, a live tamper demonstration, the
reason distribution against its published ranges, the amount bands, the arm
balance, the organic self-recovery rate, and the memoisation ratio.

From Day 3 it also prints, in this order: the **power analysis first** (so the
interval that follows is read against what the sample could ever have resolved),
the three arms, `B − A` with its confidence interval in three labelled units, the
pre-specified actioned subgroup, a per-class attribution table marking the classes
where the true effect is *exactly zero*, interval diagnostics including a
cross-check against the closed-form Wald interval, gross-versus-incremental, costs
and the false-intervention rate, refusals broken down by rule, an
**organic-recovery sensitivity sweep** at 15/30/50/70%, an **observation-window
curve**, and the estimator validated against ground truth.

Two of those are worth calling out because most submissions will not have them.
The sensitivity sweep prints the *estimand* and the *estimate* side by side —
+3.25 → +2.33 pp as organic recovery goes 15% → 70%, next to intervals showing
that a holdout of this size cannot tell those scenarios apart. And the refusal
table is **empty by design**: arm B never proposes what the envelope refuses,
which is what makes it a fair baseline, so the per-rule table prints the verdicts
that *did* fire instead. A refusal count on compliant input measures the input.

## The invariants

These are properties, each with a test, and they hold at every commit.

| # | Invariant | Where |
|---|---|---|
| **I1** | Replaying the webhook stream twice produces an identical ledger | `tests/test_idempotent_replay.py` |
| **I2** | Two different events sharing a signature produce **byte-identical prompt bytes** | `tests/test_prompt_canonical.py` |
| **I3** | The envelope returns a verdict **and a rule id** for every action × context | `tests/test_envelope_matrix.py` |
| **I4** | Every rule R1–R11 catches its engineered violation | `tests/test_redteam_envelope.py` |
| **I5** | The observation window is applied identically to every arm, and an arm whose action is inert produces the control's outcome unchanged — verified per event | `tests/test_resolve.py` |
| **I6** | The holdout estimator recovers the simulator's known true effect, is unbiased over 60 batches, and **never reads the counterfactual** | `tests/test_estimator_unbiased.py` |
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
│   ├── investigate/
│   │   ├── tools.py       the read-only tool belt, over a projection of the store
│   │   ├── receipts.py    the receipt auditor — deterministic, and no LLM in it
│   │   └── agent.py       the loop: 8 turns, a token ceiling, and the detector
│   ├── eval/            arms, outcome resolution, BCa intervals, metrics
│   ├── sense/           RiskEvent, the event store
│   ├── ledger/          the hash chain
│   └── llm/             client, cache, prompt construction
├── sim/
│   ├── generate.py      seeded generator + latent ground truth
│   ├── latent.py        the capability/intent world model
│   ├── outcomes.py      the outcome oracle
│   └── incident.py      the injected degradation, and the attempt denominators
├── tests/
├── fixtures/llm_cache/  committed — what makes the demo keyless
└── DECISIONS.md
```

### The one design decision worth reading

The investigator's SQL runs against a **separate in-memory database** holding a
banded projection of the events — not against the event store. Two guarantees fall
out of the same construction.

The simulator's ground truth (*would this customer have paid anyway? do they still
want to?*) lives in its own table, and that table is not in the database the agent
queries. So it is not merely un-joined; it is absent. An agent that could read
`has_intent` would know which customers will respond before contacting any of
them, which is not a diagnosis, it is the answer key with extra steps. A denylist
of table names was the cheap version, and it would have been one creative spelling
from failing open.

And because the projection carries no identifier, no timestamp and no rupee
figure, tool output is safe to put in a prompt *by construction*. That matters
because the alternative was tempting and wrong: a multi-turn agent has to feed
results back into its own prompt, and the easy fix is to relax the screen that
keeps high-cardinality strings out. Doing that costs nothing visible and destroys
the response cache, which takes the token budget from roughly 800K to 15M without
anything failing. The screen stayed strict; the data changed.

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
