# EVALUATION

How the headline number is produced, what it assumes, and where it could be
wrong. Written on Day 3, when the measurement layer landed.

The short version: **an assumption declared and defended is a strength; the same
assumption buried is a flaw.** Everything below is an assumption this project
makes. None of it is hidden in a comment.

---

## 1. What the headline number is

> On a 6,000-event synthetic batch, a rules-only recovery policy raised recovery
> by **+0.58 percentage points of at-risk events, 95% CI [−2.27, +3.43]** — an
> interval that spans zero.
>
> On the **25.6% of events the policy actually acts on**, the lift is
> **+9.22 pp, 95% CI [+4.96, +13.46]** — an interval that excludes zero.

Both are printed by `make demo-full`, with the second labelled as a
pre-specified subgroup rather than the headline. The gap between them is the
single most informative thing Day 3 produced, and §6 is about why.

Three quantities get reported and they are **not interchangeable** (PRD §10.2):

| Quantity | Unit | Full batch |
|---|---|---|
| Event-weighted recovery | proportion of **events** | +0.58 pp [−2.27, +3.43] |
| Value-weighted recovery | proportion of **rupees at risk** | +5.99 pp [−7.82, +19.69] |
| Money per at-risk event | ₹ | ₹533.09 [−₹776.29, +₹2,148.75] |

A "pp" figure quoted without saying which of the first two it is, is a defect.
The demo labels both on screen for that reason.

---

## 2. The experiment

Three arms, equal thirds, assigned **at detection** and never re-derived.

| Arm | Behaviour | n (6,000 batch) |
|---|---|---|
| **A** — control | Detected, diagnosed, logged, **not acted on** | 2,003 |
| **B** — rules-only | `taxonomy.DEFAULT_ACTION_BY_CLASS` + that table's own `retry_mode` | 2,001 |
| **C** — LLM-planned | Present, holds its third, **takes no action**. Wired Day 5 | 1,996 |

**Stratified** on `(source_type × amount_band × segment)` in permuted blocks of
three. Order amounts are log-normal, so a simple coin flip lets a handful of very
large payments stack into one arm and decide the money metric by luck.
Within-stratum spread is bounded at one event by construction and asserted at
every batch size in `tests/test_arms.py`.

**Arm C reports no figures, and the refusal is enforced rather than documented.**
An unwired arm takes no action, so its outcomes are identical to arm A's. A table
printing `C: 29.7%` next to `B: 30.2%` would read as *the LLM is no better than
nothing* — a statement about the calendar dressed as a finding about the model.
`metrics.contrast` raises if asked.

### Power, computed rather than picked

`n_per_arm = 2·(z_α + z_β)²·p̄(1−p̄)/δ²`, at α=0.05, 80% power, p̄=0.30
event-weighted.

| Detectable lift | Per arm | Three arms |
|---|---|---|
| 10 pp | 330 | 990 |
| 5 pp | 1,319 | 3,957 |
| 2 pp | 8,242 | 24,726 |

So 6,000 events clears a 5pp contrast and does not clear a 2pp one. The dev batch
at 67 events per arm has a **minimum detectable effect of 22.2 pp** and cannot
resolve anything a recovery system would plausibly produce — which the demo says
on screen, above the interval, so that a CI spanning zero is read as a fact about
the sample size rather than about the interventions.

> PRD §8.1 publishes 8,241 for the 2pp row. The exact value is 8,241.32 and a
> sample size is a floor requirement, so the correct figure is **8,242**. The PRD
> rounded where it should have taken a ceiling. Recorded in `FAILURES.md`.

### The observation window

**72 hours, applied identically to every arm.** Recoveries landing after it are
counted as non-recoveries *everywhere*, so the censoring cancels in B−A and shows
up only in the absolute levels. Censoring the control less than the treatment
would manufacture an effect out of the measurement, which is why the window is a
parameter of `resolve_batch` and never of an arm.

72h is a consequence of two things already fixed, not a free parameter:

1. FUNDS is the class arm B acts on at scale; its balance-arrival median is ~2
   days and the taxonomy's own scheduled retry lands at +24h. A shorter window
   would censor the treatment before it could fire and report "scheduled retries
   do not work" when what happened is that nobody waited for one. Asserted at
   import in `eval/resolve.py`.
2. It has to be a real merchant reconciliation horizon. A 30-day window would let
   organic recovery swallow the incremental effect.

The trade-off is genuine in both directions, so PRD §5.1 asks for a curve rather
than a defended constant, and `make demo` prints one at 24h/48h/72h/7d.

**`T_settle` is a different quantity and is named apart.** The observation window
is how long a recovery counts for; `T_settle` is how long the policy waits before
acting, so it does not pay to message someone who is mid-retry. Per class, and
derived from that class's own self-recovery speed. Conflating the two is how a
measurement window quietly becomes a treatment.

---

## 3. The simulator, and the one real anchor

This is the section a sceptical reviewer should read first, because **"your
simulator is made up" is the most obvious attack on the whole submission** and it
is partly correct.

### What is anchored, and what is judgement

The one **[A]**-graded fact available is Razorpay's own webhook documentation:
`payment.failed` is frequently followed by `payment.captured` for the same
transaction, because customers correct a wrong UPI PIN and retry inside their own
banking app.

That single documented sentence is load-bearing three times over:

1. It is why **gross recovered-rupees is a category error** and the headline is
   incremental (PRD §3).
2. It fixes **AUTH_DROPOFF** as a fast-decaying, customer-driven hazard: the
   instrument works, the money is there, nothing is blocked, and the delay is
   entirely the human. Median self-recovery lag **540 s**.
3. By contrast it fixes **FUNDS** as slow and block-driven — the wait is for money
   to arrive, not for attention. Median **172,800 s (~2 days)**.

Everything else in the latent model is **[C]** — judgement, not measurement. The
*magnitudes* are mine; the *shape* is anchored, and the shape is what the
estimate depends on. `tests/test_latent_world.py` pins the shape so it cannot
drift quietly:

- `AUTH_DROPOFF.blocking_share == 0.0` (nothing was blocked)
- `AUTH_DROPOFF.retry_needs_customer is True` (a merchant charge cannot supply a PIN)
- `FUNDS.blocking_share > AUTH_DROPOFF.blocking_share` (the contrast *is* the calibration)
- `FUNDS median > 100 × AUTH_DROPOFF median` (different regimes: an app retry vs a credit cycle)
- AUTH_DROPOFF is the highest-probability self-recovery class (PRD Appendix A)

### Why an uplift table would have made all of this worthless

The cheap way to simulate arm B is one line per action:

```python
P(recover | ACT_RETRY, FUNDS) = organic_rate + 0.12   # <- worthless
```

It is worthless in a specific and fatal way: **the headline incremental number
then *is* the 0.12, restated.** Sweeping the organic rate around it does not help,
because the uplift was never derived from the organic rate — every row of the
sensitivity table would print the same lift and the table would prove nothing. A
reviewer asking "where does 0.12 come from" would have no answer, and would be
right to ask.

So `sim/latent.py` models the world instead and lets the uplift fall out. Two
quantities, and the split between them is the whole idea:

**Capability** — when does the *blocking condition* stop blocking? The balance
arrives, the bank returns, the daily cap rolls over. Indifferent to what the
customer does.

**Intent** — does the customer still want this payment to complete? Indifferent
to whether the money is there.

A failed payment completes when the two coincide. And each available intervention
supplies exactly one of them:

| Intervention | Supplies | Needs | Therefore converts |
|---|---|---|---|
| Merchant-initiated retry | intent (the merchant's) | capability | *capability without intent* |
| Message | a reminder | capability, unless the block **was** the customer not having acted | *intent that never got round to it* |

Which yields the decomposition the model rests on. If a customer paid unaided at
time τ, the block cleared at some point at or before τ and the rest of the delay
was them noticing. Write that split as `blocking_share` — the fraction of the
observed self-recovery delay that was the block rather than the human — and **a
retry's value becomes precisely the noticing lag it skips.** Nothing was assumed
about the size of the effect.

Measured on the 6,000-event batch: **84.5% of events have capability clear at some
point, 58.5% have intent, and 30.4% recover unaided.** That gap is the entire
space an intervention operates in, and it is a consequence of the parameters
rather than a target of them.

### The world model, in full

Six numbers per reason class. Every one is a claim about the world, not about the
size of the effect.

| Class | `blocking_share` | retry needs customer | P(capability \| no self-recovery) | P(intent \| no self-recovery) | P(route clears) | message clears block |
|---|---|---|---|---|---|---|
| TECH_TRANSIENT | 0.85 | no | 0.90 | 0.55 | 0.45 | no |
| **AUTH_DROPOFF** | **0.00** | **yes** | 1.00 | 0.35 | 0.00 | yes |
| **FUNDS** | **0.90** | no | 0.55 | 0.45 | 0.00 | no |
| LIMIT | 0.80 | no | 0.85 | 0.40 | 0.60 | yes |
| INSTRUMENT_DEAD | 1.00 | yes | 0.10 | 0.30 | 0.00 | yes |
| MERCHANT_CONFIG | 1.00 | yes | 0.00 | 0.50 | 0.00 | no |
| INTEGRATION_BUG | 1.00 | yes | 0.00 | 0.50 | 0.00 | no |
| ALREADY_PAID | 0.00 | — | 1.00 | 1.00 | 0.00 | no |
| RISK | 1.00 | yes | 0.00 | 0.00 | 0.00 | no |
| ELIGIBILITY | 1.00 | yes | 0.05 | 0.40 | 0.00 | yes |

Response to contact, both **[C]**: `P(message response) = 0.28` with a 4-hour
median action lag; `P(voice response) = 0.55` with 30 minutes. Voice is declared
now, before any arm C result exists to tune it against, and arm B never calls.

**One independent check worth stating.** The world model and the policy were
derived separately and they agree. `AUTH_DROPOFF.retry_needs_customer` is True
because a merchant charge cannot supply a PIN — and the taxonomy, written on Day
1 with none of this in mind, answers AUTH_DROPOFF with `ACT_WAIT` rather than
`ACT_RETRY`. Same for FUNDS: `retry_mode == "scheduled"` in the taxonomy,
`blocking_share = 0.90` in the world model. Agreement between two independently
written tables is weak evidence, and it is better than none.

### How much can the invented numbers move the answer?

`P(message response) = 0.28` is the parameter most open to the accusation that it
sets the answer. It bounds only the **contact** channel, and arm B's only contact
actions are INSTRUMENT_DEAD and ELIGIBILITY — together 5.9% of volume,
contributing **+0.23 pp** of the +2.35 pp that actions actually produce. Halving
it moves the headline by roughly a tenth of a percentage point. The demo prints
the per-class contribution table so this is checkable rather than asserted.

The parameters that *do* move the answer materially are FUNDS's
`blocking_share` and `P(capability | no self-recovery)`, because FUNDS is 18.6%
of volume and carries +1.62 pp of the effect. Both are stated above, and the
organic-recovery sweep below is the sensitivity analysis on the closest thing to
them.

---

## 4. Sensitivity to the organic-recovery assumption

PRD §10.2's sweep, printed in the **standard output** rather than an appendix
(BUILD-PLAN Day 3), because it converts "your simulator is made up" from an
objection into a question already answered on screen.

Each row is a **full re-simulation** at a rescaled self-recovery probability, not
a rescaling of the central row. That distinction is the whole value of the table:
capability and intent are modelled separately, so raising organic recovery
genuinely eats the headroom an intervention has to work in, and the headline moves
**non-proportionally**. Under an uplift table every row would be identical.

`achieved` is printed next to the target because the two can differ —
ALREADY_PAID sits at probability 1.0 as a matter of definition and the dead
classes at 0.0, so no scale factor moves them. Printing the achieved value is the
difference between a sensitivity table and a wish.

The 70% row is the one that matters: **if most failed payments come back on their
own, most of what a recovery product reports was never its own work.** That row is
why this project measures incrementally at all.

Sweep intervals use 2,000 resamples rather than 10,000, stated on screen, because
the point of the table is the direction of movement across rows.

---

## 5. The estimator, and how it is verified

### BCa, hand-rolled, and checked against a closed form

Recovery rate is a proportion, so a normal CI would be adequate for it. **Money
is not a proportion** — it is a difference in means of a log-normal variable, so a
symmetric interval on it is visibly wrong to anyone who checks (PRD §8.1,
anti-pattern A6). So: **BCa bootstrap, 10,000 resamples**, both arms resampled
independently, on all three statistics.

ADR-001 keeps numpy and scipy out, so the bias correction, the jackknife
acceleration and the inverse normal CDF are all hand-written — which means they
have to be verified against answers computed somewhere other than this repo. An
interval nobody has checked is worse than no interval, because it looks like
rigour. `tests/test_bootstrap.py` checks `norm_ppf` against published quantiles
and by antisymmetry, the point estimate against plain arithmetic, a zero effect
against spanning zero, and interval width against `1/√n`.

**The one place the bootstrap can be checked on the project's own data** is the
rate, because that is where the closed form is trustworthy. The demo prints both:

```
  BCa rate CI                             +0.58 pp  [ -2.27,  +3.43]
  closed-form Wald rate CI                +0.58 pp  [ -2.26,  +3.42]
  the two agree to within 25% of width   YES
```

Close but not identical — if the correction did nothing it would not be worth
computing.

### The money interval is wider, and asymmetric

Two heavy-tail signatures, both printed and both checked as a build gate:

```
  rate CI width / arm A rate             0.192
  money CI width / arm A Rs-per-event    1.113
  money CI relatively wider              YES
  money CI asymmetry, upper/lower        1.23  (1.00 = symmetric)
```

**Normalised by the control-arm level, not by the point estimate.** The first
version divided each width by its own point estimate and reported "the money
interval is narrower" on the full batch — purely because B−A on the rate was
0.58 pp and a near-zero denominator makes any relative width explode. The
quantity being compared is how precisely each statistic can be *estimated*, so
the scale has to come from something stably estimated: arm A's own level.

The asymmetry is the signature a normal approximation cannot produce at all, being
symmetric by construction. It is therefore the observable difference between
having used a bootstrap and having claimed to.

### Validated against ground truth

PRD §8.2, and the reason the whole design is honest. In production the
counterfactual is unobservable; in simulation both potential outcomes are
computable for **every** event, so the true average treatment effect is an *exact
quantity* rather than an estimate with error of its own. The estimator then has
exactly one source of error left — which arm each event landed in — and that is
what gets tested.

This required a deliberate design choice: every random draw an intervention
depends on is made at **generation** time and stored in `LatentTruth`, so
resolution is a pure function. An oracle that rolled dice at resolution time would
give the truth side its own Monte-Carlo error and turn a proof into a smell test.

`tests/test_estimator_unbiased.py`, four tests answering four different questions:

| Test | Question | Result |
|---|---|---|
| CI covers truth | Does the interval contain the truth on the reported batch? | True ATE **+3.02 pp** lies inside [−2.27, +3.43] |
| Unbiased over 60 seeds | Does the estimate converge to the truth? | mean(estimate − truth) within 3 SE of zero, paired per batch |
| Coverage at nominal rate | Does a 95% interval contain the truth ~95% of the time? | **95.8% over 600 independent batches** (SE 0.9 pp). Test asserts ≥ 86% at 50 seeds |
| Never reads the answer key | Is the agreement circular? | Estimates unchanged when the counterfactual is stripped |

**On the coverage figure.** A first pass measured 92.0% / 90.0% / 91.7% at three
sample sizes and was briefly written up here as a systematic shortfall. It was
not: those runs used 200, 120 and 60 seeds, so their standard errors were 1.5, 2.0
and 2.8 pp and each result sat between 1.2σ and 2.5σ from nominal — individually
marginal, and treating three marginal results as a trend is an inference error
rather than a measurement. Re-measured at 600 seeds: **95.8%, SE 0.9 pp, 0.9σ from
nominal.** The Wald interval on the same batches gives 96.8%, slightly
conservative, which is its known behaviour. The episode is recorded in
`FAILURES.md` because nearly publishing a defect that was not there is the same
class of error as publishing a number that was not measured.

The test keeps a loose floor of 86% at 50 seeds on purpose: at that sample size
the standard error is 3.1 pp, so a tighter assertion would fail on noise — which
is exactly the trap described above, encoded so it cannot be walked into again.

The last one matters most. `sim/outcomes.py` reads latent truth because it stands
in for a webhook that in production reports the real outcome. Everything
downstream — arm samples, bootstrap, contrast — must work from observed outcomes
alone, or the estimator is reading the answer key and its agreement with the truth
proves nothing. The test strips `would_recover_unaided` and asserts every estimate
is byte-identical.

**No latent field reaches the ledger.** ADR-010 quarantines ground truth in its
own table; Day 3 extends the same rule to the ledger, which is the artifact a
reviewer audits and a table Day 4's read-only SQL tool can reach. Checked over the
stored rows rather than over the writer — a check that reads the code it is
checking proves nothing.

---

## 6. What Day 3 actually found, including the unflattering part

**The rules-only policy's incremental effect is real, concentrated, and diluted
into insignificance by the volume it declines to touch.**

| Class | Share | Arm B action | Contribution to lift |
|---|---|---|---|
| TECH_TRANSIENT | 47.9% | `ACT_WAIT` | **0.00 pp — no-op** |
| AUTH_DROPOFF | 24.2% | `ACT_WAIT` | **0.00 pp — no-op** |
| FUNDS | 18.6% | `ACT_RETRY` (+24h) | **+1.62 pp** |
| LIMIT | 1.1% | `ACT_ROUTE` | **+0.50 pp** |
| INSTRUMENT_DEAD | 5.6% | `ACT_MESSAGE` | **+0.20 pp** |
| ELIGIBILITY | 0.3% | `ACT_MESSAGE` | **+0.03 pp** |
| MERCHANT_CONFIG / INTEGRATION_BUG / RISK / ALREADY_PAID | 2.3% | alert / page / escalate / stop | 0.00 pp — no-op |

**72.1% of volume gets `ACT_WAIT`, where the true effect is exactly zero** — the
resolver returns the control outcome unchanged, verified per event rather than
asserted. Those events contribute no signal and their full share of variance, so
the intent-to-treat headline is a real +3.02 pp true effect measured through a
sample where three-quarters of the observations are known-null.

That is why both figures are published. The ITT number answers *does this recover
money across the whole workload* and the honest answer on 6,000 events is *not
distinguishably*. The subgroup number answers *does the intervention work where it
is applied* and the answer is *yes, +9.22 pp, interval excluding zero*.

**Conditioning on the subgroup is legitimate**, and the reason is specific:
`actionable` is a function of `cause_signal` alone, via the reason class and its
default action. It is known at detection, identical for the same event in every
arm (asserted in `tests/test_resolve.py`), and cannot be contaminated by the
outcome. Defining a subgroup on a pre-treatment variable is sound; defining one on
the outcome would not be.

**This is the headroom C − B is measured against, and it is stated now, before any
LLM result exists to be flattered by it.** The table above is a map of where an
LLM planner could beat a lookup table: TECH_TRANSIENT is 48% of volume with a 90%
capability-clearance rate and a silent retry available, and the table answers it
with "wait". Whether an LLM does better is a Day 5 question. That the opportunity
is there is a Day 3 measurement.

### The cut that was considered and rejected

`taxonomy.REASON_CLASS_POLICY["TECH_TRANSIENT"].retry_mode` is `"immediate"` and
its note says "silent retry only" — yet `DEFAULT_ACTION_BY_CLASS` answers it with
`ACT_WAIT`. Changing that one entry would have raised the headline substantially.
It was not changed, because the map is a **published Day 2 artifact** and editing
it after seeing which change would improve the number is the precise mechanism by
which measurement becomes advocacy. It is recorded here as a real tension in the
taxonomy and a Day 5 question, not resolved in the commit that measures it.

One thing *was* changed, and it went the other way: arm B now carries its own
24-hour scheduled-retry delay, taken from the taxonomy's own `retry_mode` rather
than from a new constant. Day 2 measured 18.2% of the raw map's actions coming
back `AMEND` — all G7, an immediate retry on insufficient funds. Arm B proposing
the delay itself makes it the strongest table the taxonomy supports, which makes
C − B a *harder* bar for the LLM. Visible in the demo: G7 now returns `ALLOW`
where it previously returned `AMEND`.

---

## 7. Costs, and the resource that is actually scarce

| Line | Full batch, arm B |
|---|---|
| Actions taken | 1,973 of 2,001 |
| Customer contacts | 88 |
| Total cost | ₹388.20 (₹375 human escalation × 15, ₹13.20 messages × 88) |
| Cost per rupee recovered, gross | 0.006 paise |
| Cost per rupee recovered, incremental | 0.036 paise |
| Recovered per contact, contact-caused | ₹155.83 |
| False-intervention rate | 1 of 88 contacts (1.1%) |

**Both a gross and an incremental cost ratio, because gross flatters it** — most
of the gross denominator is money that was coming back anyway.

**"Recovered per contact" counts only value a contact actually brought in.** The
first version divided the arm's entire recovered value by its contact count and
reported ₹50,166 per contact on the dev batch: nonsense in the flattering
direction, which is the worst kind. Fixed, and the field carries a comment saying
so.

**The false-intervention rate has contacts as its denominator, not events.** Over
events the rate would fall automatically by contacting fewer people, which would
reward doing nothing — and doing nothing is already arm A. It is the number
`T_settle` exists to hold down, and it is computable only because the simulator
knows the counterfactual.

**Merchant alerts and engineer pages are counted as NON-recoveries.** Both are the
correct action for their class, and neither recovers the event in front of it:
telling a merchant their MCC is misconfigured fixes the next thousand payments,
not this one. The value they touch is reported separately as a labelled
externality. Under-claiming a real benefit is the right way round to be wrong.

**Refusals by rule.** Arm B produces **zero** rule refusals, and that is the
designed result rather than an inert gate: the map is built compliant and never
proposes what the envelope refuses, which is what makes it a fair baseline. The
demo prints the per-rule table anyway, populated with the verdicts that *did*
fire and the value each rule governed — same shape, non-empty, and it still
answers which rules decide anything on this workload. The evidence the envelope is
not inert is deliberately elsewhere: `tests/test_redteam_envelope.py`, one
engineered violation per rule R1–R11, all caught and cited. **A refusal count on
compliant input measures the input.**

---

## 8. Threats to validity

Stated plainly, worst first.

1. **The workload is synthetic.** No real Razorpay data was used or available. The
   reason-code *distribution* is weighted to PRD §5.1's published buckets and
   checked against them at runtime, and the self-recovery *shape* is anchored to
   one documented Razorpay behaviour. Everything else is judgement. The organic
   sweep is the defence, and it is a defence against one parameter being wrong,
   not against the model being the wrong shape.

2. **One intervention per event.** Real recovery is a sequence — retry, wait,
   message, escalate. Day 3 has no sequencer, so arm B takes exactly one action.
   This understates what a rules-only policy could do and understates arm C's
   ceiling too.

3. **The world model was written by the same person as the policy.** The
   independent-agreement check in §3 is weak evidence, not strong. A world model
   built by someone who knew which policy would be measured against it is
   vulnerable in a way no internal test can detect.

4. **The ITT headline is underpowered.** +0.58 pp against a 3.7 pp minimum
   detectable effect at 2,001 per arm. The true effect of +3.02 pp is inside the
   interval, and also inside the interval [−2.27, +3.43] is zero. Detecting a 3 pp
   effect at 80% power needs ~3,700 per arm; the batch could be enlarged, and
   deliberately was not, because 6,000 is the figure PRD §8.1 pre-registered from
   the power calculation and choosing N after seeing the result is the thing the
   pre-registration exists to prevent.

5. **`legal_context` is uniformly `service`.** Day 1 ships payment failures only,
   so no event in this batch is a debt collection and R9 — the one **[A]**-graded
   rule — never binds on the demo path. The receivables adapter arrives Day 6.

6. **One of eleven regulatory citations is [A].** Unchanged from Day 2 and still
   the weakest claim in the project. It does not affect the Day 3 number, because
   arm B is refused by nothing.

---

## 9. Reproducing it

```
git clone <repo> && cd pramaan
make install
make demo        # 200-event dev batch, ~40 s, no API key, no network
make demo-full   # 6,000-event batch, ~105 s. The governing figure
make test        # 329 tests
make verify      # test + I8: two demo runs diffed byte for byte
```

Every figure in this document comes from `make demo-full` at seed 42. The run is
deterministic: same seed, byte-identical output, checked by diffing two runs
rather than by eyeballing them. **Zero LLM calls and zero tokens** — three days
in, the headline number cannot be blocked by a rate limit.
