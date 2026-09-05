# Pramaan — AI Revenue Recovery

**On 6,000 revenue-at-risk events spanning all five event types, Pramaan's
LLM-planned arm lifted recovery by +10.43pp against a randomised control arm
(95% CI +7.63 to +13.33pp, BCa, 10,000 resamples), worth ₹88,747 incremental.
Gross recovery was ₹47,54,868, roughly 4.9× the incremental. Most of that gross
was customers retrying on their own, and this repo shows you the difference —
reproducibly, with no API key.**

**The lift replicates; the rupees are a lottery.** Re-run on five independent
seeds, the recovery-rate lift lands between **+8.38pp and +11.04pp** (mean
+9.92) and **every one of the ten intervals excludes zero** — C−A and C−B alike.
The rupee figure on the same five draws spans **₹0.89L to ₹12.82L**, a 14×
range, because order amounts are log-normal and a handful of band-5 events move
a mean that no single event can move a proportion. **Seed 42 — the committed
default everything here reproduces from — is the lowest of the five.** It stays
the default anyway: the honest thing is to ship the draw the artifacts were
built on, not to go shopping for a flattering one. Read the pp.

**The envelope refused 1,140 of those 6,000 proposed actions outright**, every
one citing RBI's e-mandate pre-debit notification rule (R1). That is the number
worth reading twice: recovery on mandates and subscriptions is capped by law,
not by the agent, and a system that cannot show you its refusals has not been
tested against the part of the problem that bites.

Razorpay's own webhook docs warn that `payment.failed` is often followed by
`payment.captured` for the same transaction — customers fix a wrong UPI PIN
and retry inside their banking app. So gross recovered-rupees is not a measure
of an agent's value. This one measures against a control arm.

```
git clone <this repo> && cd pramaan && make demo   # ~40s, NO API KEY, reproduces the invariants
make execute-full                                  # ~2.5m, NO API KEY, reproduces the headline number
```

![make execute-full on the earlier payment-only batch: C-A +17.49pp, C-B +16.91pp (both excluding zero), Rs 7,44,967.63 incremental, ALL CHECKS PASS](assets/demo.gif)

*The GIF is a faithful render of real `make execute-full` output (deterministic
bytes → deterministic frames), not a screen capture and not staged.* **⚠ It
records the earlier payment-only batch and therefore shows the older figures
(+17.49pp, ₹7,44,967), not the five-type numbers above.** *It is kept, clearly
labelled, rather than quietly deleted or captioned as though it matched — but it
needs re-recording before submission. Both commands run offline from the
committed cache.*

🔊 **[Hinglish recovery call — 29s MP3](assets/voice-demo.mp3)** · **[transcript, for review on mute](assets/voice-transcript.md)** — a two-voice Sarvam **TTS** rendering of a scripted Hinglish recovery call. It opens with the **AI disclosure (R10)**, and the agent's turn policy, envelope gating, and promise extraction are all real: the customer's line *"Friday tak pakka kar dunga"* becomes a structured promise on the hash-chained ledger. **Honest scope:** the customer lines here are a scripted transcript, *not* transcribed audio — the Sarvam **STT** leg is implemented and verified to transcribe the Hinglish audio, but the shipped clip does not run it ([why](LIMITATIONS.md#the-voice-clip-is-real-tts-of-a-scripted-call-the-stt-leg-is-not-exercised-in-it)). `make voice` reproduces the transcript keyless; `make voice-live` synthesises the audio.

| Metric | Value |
|---|---|
| **Incremental recovery** | **+10.43pp** recovery-rate lift (95% CI +7.63, +13.33) vs randomised holdout; **₹88,747** incremental. Across 5 seeds: **+8.38 to +11.04pp**, all excluding zero |
| Did the LLM earn its place (C−B) | **+9.12pp** [+6.32, +12.07], excludes zero; exact true effect **+7.88pp**. Across 5 seeds: **+5.68 to +9.12pp**, all excluding zero |
| Investigator | Canary **confirmed** the injected incident's rate/mix split exactly — observed tier2 rate / tier3 mix against truth tier2 / tier3. `make investigate` prints the verdict and writes a `CANARY` row; a refuted verdict additionally writes a `RETRACTION`, the system withdrawing a claim it had already made |
| Planner violation rate | **19.0%** organic, event-weighted (**7.7%** per distinct signature) — the planner proposes something the envelope must correct on nearly a fifth of traffic, almost all of it mandate debits hitting R1. Base: **33 of 303** signatures are LLM-authored, the rest the deterministic fallback. Separately, the envelope caught **100%** of 26 engineered violations (one per R1–R11 + G1–G8) |
| Receipt coverage | **100%** of diagnosis claims backed by a real tool call |
| Cost per incremental ₹ | **₹0.0017** (retries are free; only voice/message carry a per-contact cost) |
| Envelope activity | **935** actions amended / **1,140** hard-rejected on the 6,000-event gate, each citing its rule → [breakdown](SAFETY.md) |

> **Design for three time budgets** (respecting a reviewer's time is itself a signal):
> **60 seconds** — the number above and the voice clip · **5 minutes** — the pitch video ·
> **30 minutes** — [`EVALUATION.md`](EVALUATION.md) (holdout, power, bootstrap) and [`SAFETY.md`](SAFETY.md) (rule by rule) ·
> **Actually running it** — `make demo`, ~40s, no API key.

**All seven brief directions:** payment degradation · checkout drop-off ·
failed subscription · mandate retry · B2B receivables · Hinglish voice ·
promise-to-pay — [coverage table](#coverage--all-seven-brief-directions) below, seven for seven.

**The bar:** measured money ✓ · compliant escalation ✓ · stopping rules ✓ ·
audit trail ✓ — see [the invariants](#the-invariants) and [the envelope](#the-envelope-and-why-it-was-built-before-the-llm) below.

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
| **Hinglish voice recovery** | Core | `pramaan/converse/voice.py` — real turn policy + Sarvam TTS: **AI disclosure first (R10)**, R8/R9 windows and self-identification, S7 distress/dispute/legal stand-down, and a promise extracted from the customer's **utterance** into the state machine and the ledger. The Sarvam **STT** leg is implemented and verified, but **not exercised in the shipped clip** ([LIMITATIONS.md](LIMITATIONS.md)) |
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

### The number

All three arms are measured. The two contrasts, on the full 6,000-event batch,
reproduced offline from the committed cache with **no API key**:

> **C − A — does the LLM-planned loop recover money? +10.43pp of at-risk events,
> 95% CI [+7.63, +13.33].** Excludes zero. **₹88,747 incremental**, 191 customers
> contacted, cost ₹153.65 (₹0.0017 per incremental ₹).

**Read the pp, not the ₹.** *The rate lift is stable across seeds and the rupee
figure is not, because order amounts are log-normal and a handful of large events
move a mean far more than any single event moves a proportion. Seed 42 gives
+10.43pp / ₹88,747; seed 7 gives* ***+11.04pp / ₹8,99,561*** *— the lift moves
0.6pp and the money moves 10×. Quoting ₹88,747 as "the" number would be as
misleading as quoting ₹8,99,561; the interval on the rate is the claim, and the
rupees are one draw's illustration of it.* ***The contacted count is
near-invariant too*** *(191 at seed 42, 183 at seed 7) — contact eligibility is
fixed by the class-weight distribution and the P2 value floor, not by the
per-event latent draws, so which events are contactable barely moves while what
they recover does.*
>
> **C − B — did the LLM earn its place over the lookup table? +9.12pp, 95% CI
> [+6.32, +12.07].** Excludes zero, and the exact true effect (**+7.88pp**,
> potential outcomes) confirms it is real rather than sampling noise.
>
> **On rate, yes. On value, no** — C − B's value-weighted contrast is
> **−6.33pp** of at-risk rupees. Arm C recovers more *events* than the lookup
> table and fewer *rupees*, because the events it wins are the cheap ones. Both
> figures come from the same run and both are printed; a headline that quoted
> only the first would be picking the flattering half of one contrast.

What the LLM actually decided, inspected directly: for `TECH_TRANSIENT` and
`AUTH_DROPOFF` — the two highest-volume classes — the planner proposes an
*immediate, silent, server-side* `ACT_RETRY` where the rules-only table proposes
`ACT_WAIT` and does nothing. Both are legal under the taxonomy; where the block has
already cleared by detection time, the server-side attempt recovers the payment at
**zero customer-contact cost**, which is exactly what the true-effect number
measures. The B−A contrast below is the earlier, weaker story (the lookup table
barely beats control) — kept because the *gap* between B−A and C−B is the point.

> **B − A intent-to-treat, all 6,000 events: +0.58 pp, 95% CI [−2.27, +3.43].**
> Spans zero. **On the 25.6% of events the rules-only policy acts on: +9.22 pp,
> 95% CI [+4.96, +13.46].** Excludes zero.

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
error — the money interval is relatively wider than the rate interval, and it is
visibly asymmetric (1.35 upper/lower) where a normal interval is 1.00 by
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
batch there are 273 distinct planner signatures — a **29.3× memoisation ratio**
(7,996 `plan_for` calls / 273 distinct plans). A human ops lead does not re-think
policy for every ticket either.

---

## Build status

**Shipped — all eight days landed.** The spine, the compliance envelope, the
three-arm measurement layer, the LLM investigator, the planner+executor, all five
adapters, the promise machine, the canary, and the Hinglish voice loop are built,
tested, and reproducible offline. **692 tests, 0 skipped.**

The whole loop, end to end:

- **Sense** — `RiskEvent`, the abstraction all five event types normalise into,
  via five adapters (`payment`/degradation, `checkout`, `subscription`, `mandate`,
  `receivable`). SQLite event store, idempotent on `event_id` (webhooks are
  at-least-once). Hash-chained, append-only ledger with tamper + truncation
  detection.
- **Investigate** *(LLM)* — an agent with a **read-only, six-tool belt** that
  writes its own SQL against a physically separate projected database, plus a
  deterministic **receipt auditor** that strips any claim it cannot evidence. A
  **canary** re-checks the diagnosis against ground truth the agent cannot reach.
- **Plan** *(LLM)* — a `RecoveryPlan` object, memoised per signature (**29.3×**
  on the full batch), that never calls a tool; on a true cache miss it falls back
  to the deterministic default rather than blocking (NFR-2).
- **Envelope** *(no LLM, by design)* — `judge(step, context)` → ALLOW/AMEND/REJECT,
  always naming the rule. R1–R11, the window matrix, tiers T0–T4, guardrails
  G1–G8, stopping rules S1–S7.
- **Converse** *(LLM)* — the promise-to-pay state machine and the **Sarvam voice
  loop**: AI disclosure first (R10), R8/R9 windows, S7 stand-down, promise
  extracted from the customer's utterance into the ledger. (The STT leg is
  implemented and verified but not exercised in the shipped clip — see
  [LIMITATIONS.md](LIMITATIONS.md).)
- **Execute** *(no LLM)* — Razorpay TEST-mode orders and payment links,
  `hash(payment_id, action_type, attempt_ordinal)` idempotency, a per-counterparty
  lock, and a terminal-state guard — one real order and payment link were created
  live (test mode) with idempotency and the guard both confirmed against the API.

**Tokens are now > 0.** Days 1–3 spent nothing on purpose (the number must not
depend on a rate limit); the LLM layers were then run live against real keys, and
the committed cache replays them so `make demo`/`make execute-full` reproduce every
figure with **no key at all**. On the full batch, 36 of 273 planner signatures
resolve to a real LLM reply; the rest fall back deterministically and are labelled
as such.

**What is not built** is stated plainly and without apology in
**[LIMITATIONS.md](LIMITATIONS.md)**: the reflection/playbook-learning loop (design
only), and the parts that could not be provisioned inside the window — SMS/WhatsApp
transport, outbound PSTN (needs a DLT-registered caller ID), live production data.
The transport is stubbed; the voice call runs over local audio, and this repo says
so.

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
make demo        # 200-event dev batch, keyless, no network call (~40s)
make demo-full   # 6,000-event batch (sized from a power calculation)
make execute-full# arm C wired: the C-A / C-B headline, keyless, from cache (~2.5m)
make investigate # the LLM investigator, from the committed cache
make voice       # the Hinglish recovery call -> transcript + ledger, keyless
make voice-live  # the same, synthesised to assets/voice-demo.mp3 (needs SARVAM_API_KEY)
make models      # print the configured model IDs and check they still resolve
make dashboard   # aggregate the committed ledger into build/dashboard.json
make test        # 692 tests, including the invariants below
make verify      # tests, plus a byte-identical-output check across two runs
```

`pytest` on its own is equivalent: `tests/conftest.py` sets
`PRAMAAN_LLM_OFFLINE=1` unless you have already set it, so the suite cannot
reach a model by accident. It is `setdefault`, not an assignment — an explicit
`PRAMAAN_LLM_OFFLINE=0` still wins.

`make dashboard` needs `make demo-full` (for the ledger) and `make execute-full`
(for the contrast tiles) to have run first. It opens those artifacts read-only,
aggregates them into one JSON file, and re-runs nothing — so it cannot move a
number the two commands above already fixed.

To **drive the pipeline from the page** rather than only read its output:

```bash
python -m pramaan.report.server    # then open http://127.0.0.1:8000/dashboard/
```

That serves the same static files *and* exposes a small run API, so the
dashboard gets a **Run it** panel: click `demo-dev` and the real
`python -m pramaan.cli demo --dev` starts, its stdout streams into the page, and
when it finishes the ledger is re-aggregated and every tile updates in place —
6,000 events become 200, and the gate counts change with them. Click
`snapshot` to come back to the full batch in about five seconds.

The server runs a **fixed table of commands** keyed by id; nothing from the
request reaches a shell, it binds to `127.0.0.1` only, it refuses a second
concurrent run (two writers on one SQLite ledger is a corrupted ledger), and it
forces `PRAMAAN_LLM_OFFLINE=1` on every run — so no button can spend a token or
open a socket. It reimplements nothing: every button is the CLI a reviewer would
type, so there is still exactly one path into the envelope.

### Talk to it

The same server exposes the one path in this project that **cannot** be keyless:
a real conversation with the agent. Press **Start call** and the agent speaks
first — R10's requirement is that the AI disclosure is the *first utterance*, not
that it appears somewhere in the transcript, so the opening is a separate
endpoint from a turn and the ordering is structural. Then hold the button and
reply in Hinglish. One turn is `your voice → Sarvam STT (saaras:v3) → the
turn-policy LLM → Sarvam TTS (bulbul:v3) → audio you hear`, and a commitment in
what you said is extracted into the promise state machine and shown against the
verbatim line it came from.

This is the honest exception to everything above: it needs `SARVAM_API_KEY` plus
an LLM key, it spends money per turn, and it cannot be replayed from the fixture
cache — a conversation that has not happened yet cannot have been recorded. The
envelope still gates it: the preflight ruling, with the rule id and the citation,
is printed above the transcript before the first word. Everything *else* on the
page runs with `PRAMAAN_LLM_OFFLINE=1` forced and no key at all.

A plain static server still works, and the page degrades to the read-only report
it was — the replay and live-call panels stay hidden, and the **Run it** panel
says why it has no buttons:

```bash
python -m http.server 8000   # then open http://localhost:8000/dashboard/
```

The page renders no figure it computes itself. Every contrast comes from the
`BatchMetrics` object `make execute-full` printed, serialised to disk by the same
run, and every count is a count of rows that exist in the chain — whose head hash
is displayed so it can be compared against the one the demo prints.

`make dashboard` also writes **`build/actions.csv`** — one line per resolved
event (6,000 rows, 20 columns), joined across its `DETECT`/`GATE`/`OUTCOME`/
`EXCEPTION` ledger rows, including the envelope's verbatim reason for every
refusal. Open it in a spreadsheet directly, or browse it in the dashboard's
**Every row** section, which filters by arm, source type, action, gate verdict
and outcome, exports the filtered subset as CSV, and prints to PDF through the
browser's own print dialog. No latent field is in it: the counterfactual answer
key stays quarantined in its own table (ADR-010), so the export is an audit
artifact and not an answer sheet.

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
│   │   ├── canary.py      re-checks a diagnosis against ground truth it cannot reach
│   │   └── agent.py       the loop: 8 turns, a token ceiling, and the detector
│   ├── plan/           the planner (emits a RecoveryPlan; never calls a tool)
│   ├── execute/        Razorpay TEST-mode client, idempotency, locks, guard
│   ├── converse/       promises.py (the state machine) + voice.py (Sarvam loop)
│   ├── eval/            arms, outcome resolution, BCa intervals, metrics
│   ├── sense/           RiskEvent, the event store, and the five adapters/
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

- **SMS and WhatsApp transport is not implemented — there is no send path, so no
  message is ever presented as having been sent.** DLT registration requires a
  registered business entity, which is not obtainable for this project. What *is*
  real is the constraint the regulation imposes: R5's template gate is enforced in
  the envelope, and the `channel` is a first-class field on every action. **The
  voice audio is real Sarvam TTS** (a scripted call over local audio, not a dialed
  PSTN call, which needs a DLT-registered caller ID); the turn policy and promise
  extraction are real, but the **STT leg is implemented and verified, not run in
  the shipped clip** ([LIMITATIONS.md](LIMITATIONS.md)). See
  [LIMITATIONS.md](LIMITATIONS.md).
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
