# FAILURES

Daily log of what broke and what mechanism was added. Source material for the
application form's final question. Format is fixed (BUILD-PLAN.md §3) so that
Day 8 is selection rather than archaeology.

**Severity convention** (declared here so it stays consistent across days):

| | Meaning |
|---|---|
| 5 | Would have invalidated a published number, silently |
| 4 | Would have invalidated a published number, visibly |
| 3 | Wrong by construction; caught before it reached a number |
| 2 | Correct but misleading, or a planning estimate that did not survive measurement |
| 1 | Friction. Cost time, changed nothing structural |

**Note on the Time field.** These entries were produced in an agent session with
no wall clock, so durations are recorded as the build block rather than invented.
Replace them with real figures if you have them.

---

## Day 1 — 2026-08-26

### [2026-08-26] [severity 3] [layer: eval / assertions]

```
Symptom:   Three separate checks failed on the 200-event dev batch while the
           6,000-event batch passed all three. The demo self-check "all five
           buckets inside their published range" failed; "arms within one event
           of equal thirds" failed at 68/67/65; and the uniformity test failed
           at 13.0% against a 14.5% floor.

Diagnosis: One conceptual error wearing three costumes, and in every case the
           assertion was wrong rather than the system. I was testing a 200-draw
           multinomial sample against *population* parameters. The published
           reason-code ranges describe real-world traffic; a 200-event sample
           has a ~3.4pp standard error on a 0.37 share, so requiring it to land
           inside a 10pp-wide population range is requiring a simulator that is
           not random. The arm check had the same shape: assignment is
           stratified permuted-block over (source_type, amount_band, segment),
           so every stratum with an incomplete final block leaves a residual of
           one or two, and the worst-case imbalance is bounded by the number of
           strata rather than by one. The uniformity test used a fixed multiple
           of the uniform share (10x = 14.5%) against a true value of 22%,
           which a -3.1 sigma draw clears from below while still sitting 13.7
           sigma above uniform.

Mechanism: Split every distribution assertion along the line between a
           configuration property and a sample property, and made that split
           explicit in the code rather than in a comment.
             - declared weights vs published ranges: exact, since they are
               constants
             - observed shares vs declared weights: three standard errors,
               scaled to n
             - published ranges vs observed: full batch only, where the standard
               error is ~0.6pp
             - uniformity: a hypothesis test against the uniform null at five
               standard errors of the null, so the threshold scales with batch
               size instead of being a fixed multiple
             - arm balance: 5% relative to n/3, plus a separate within-stratum
               check, because stratification is the property worth protecting
               and an arbitrary absolute tolerance would have traded it away
           Before touching the sampler I verified _weighted_choice was unbiased
           at N=400,000 (all |z| < 1.3), so the deviation was confirmed as noise
           rather than assumed to be.

Time:      Day 1, blocks D and E
```

### [2026-08-26] [severity 3] [layer: sim / distribution]

```
Symptom:   The 6,000-event batch put the network/connectivity bucket at 9.23%,
           below the 10-15% range the PRD publishes and the README quotes.

Diagnosis: The declared weight was 0.10 -- exactly the published floor. A value
           sitting on a boundary falls below that boundary half the time, so the
           configuration was making a claim the sampler could not reliably
           satisfy. Nothing was wrong with the draw; the declaration was placed
           where it could not hold.

Mechanism: Rebalanced so every declared weight sits at least 0.5pp inside both
           edges of its published range (network 0.10 -> 0.11,
           insufficient_balance 0.19 -> 0.18). Then added
           test_no_declared_weight_sits_on_a_range_boundary, which fails if any
           future weight is placed on an edge. That moves the invariant from
           "the batch happened to match once" to "the configuration cannot
           express a claim the sampler cannot satisfy" -- the failure is now
           unrepresentable rather than checked after the fact.

Time:      Day 1, block D
```

### [2026-08-26] [severity 2] [layer: plan / token budget]

```
Symptom:   Measured memoisation on the 6,000-event batch is 273 distinct
           planner signatures and a 22.0x ratio. BUILD-PLAN §1.5 predicts
           ~100-200 signatures and ~30x.

Diagnosis: The planning estimate assumed more structural unreachability than
           this event mix produces: the dominant reason classes get full
           coverage across band x segment x hour_bucket, so the reachable space
           is larger than the estimate implied. Worse for planning purposes,
           diagnosis_class is currently constant at "undiagnosed" because no
           investigator exists yet -- so the signature count will *grow* on
           Day 4, not shrink toward the prediction.

Mechanism: Reported the measured figure rather than the planned one, in
           DECISIONS.md ADR-003 and in the README. Printed the distinct
           signature count and the ratio in every demo run, so drift shows up on
           the next run instead of at the end of the token budget. And restated
           the budget in the unit that actually governs it: 273 planner calls at
           ~800 tokens is ~220K tokens, which stays inside budget even if Day 4
           doubles the count -- so the decision rests on call count rather than
           on defending a headline ratio.

Time:      Day 1, block E
```

### [2026-08-26] [severity 1] [layer: tooling]

```
Symptom:   Two build-plan instructions could not be executed as written.
           "make demo --dev" fails because GNU make parses a leading "--" as one
           of its own options, and there is no make binary on the Windows
           machine this was built on.

Diagnosis: Not a code problem. The dev batch is the reviewer path, so it should
           have been the default target rather than a flag.

Mechanism: "make demo" is the dev batch; "make demo-full" is the 6,000-event
           run; the CLI still accepts --dev literally. Shipped make.ps1 with the
           same targets, and said plainly in DECISIONS.md ADR-008 which of the
           two was actually exercised here. Shipping a Makefile nobody had run
           seemed worse than shipping both and labelling them.

Time:      Day 1, block A
```

### Not a failure, but worth recording

Thirteen of the 69 reason codes never appear in a 6,000-event batch, all of them
in long-tail classes sampled at a fraction of a percent. That is correct
behaviour for a volume-weighted draw, but it means **Day 2's red-team tests must
construct reason codes directly** rather than rely on the simulator to cover the
taxonomy. Recorded as a consequence in DECISIONS.md ADR-007.

### Candidate for the form answer

The first entry is the strongest of the four so far: it is a single reasoning
error that surfaced in three unrelated places, the fix changed how assertions are
written across the whole project, and the diagnosis required checking that the
sampler was unbiased before concluding the test was wrong. The second is a close
second, because the mechanism made a class of mistake unrepresentable rather than
merely fixing an instance of it.

---

## Day 2 — 2026-08-26

### [2026-08-26] [severity 4] [layer: envelope / rule ordering]

```
Symptom:   The definition-of-done case "a mandate retry with no T-24h pre-debit
           notification is REJECTED citing R1" was rejected citing **G7**. The
           refusal was correct; the citation was not.

Diagnosis: Both rules bind that step. G7 is a house guardrail requiring a
           FUNDS-class retry to be deferred 24h to clear the credit cycle; R1 is
           the RBI pre-debit notification obligation, which is also 24h. The
           evaluator ran the whole taxonomy guardrail block before the
           regulatory block on the principle "futility before legality" -- which
           is right for G1-G5 and G8, and wrong for G7, because G7 is not a
           futility rule at all. It is a *timing* rule, and its floor is a
           number we chose while R1's is a number a regulator chose.

           So the envelope reported a regulatory breach as a scheduling
           preference. The verdict a compliance report would have shown for a
           non-compliant mandate retry was "we would rather wait for the credit
           cycle".

Mechanism: Split reason_map.check_retry into check_retry_futility (G1-G5, G8)
           and check_retry_timing (G7), and moved G7 to *after* the regulatory
           group in judge._collect. The comment explaining why sits on
           check_retry_futility, naming this failure, because the split looks
           like gratuitous decomposition until you know what it is for.

           The general rule this produced, now written at the top of judge.py:
           evaluation order exists to make the citation the most informative
           true statement available, not to make the code shortest. When two
           rules refuse the same step, the one with an instrument behind it wins.

Time:      Day 2, block A/D boundary -- found by running the DoD cases before
           writing the tests, not by a test.
```

### [2026-08-26] [severity 3] [layer: envelope / citation on ALLOW]

```
Symptom:   "A collection contact at 18:55 is ALLOWED" passed, and cited S6 with
           the reason "no cost attributed to this step". On the full batch, every
           ACT_ESCALATE_HUMAN was allowed citing P0, "no time band applies".

Diagnosis: Two instances of one mistake. _binding_allow originally took the last
           rule that reported itself as binding, on the theory that the last one
           evaluated is the tightest. It is not -- the evaluation order is
           arranged by *severity*, and an allow wants *specificity*. Worse, two
           rulings were claiming to bind when they had nothing to weigh: S6 on a
           zero-cost step, and the unrestricted window cell for a silent action.
           Both were technically ALLOW-with-jurisdiction and neither carried any
           information, so they crowded out the rules that did.

           The visible cost: a lawful evening collection message -- the single
           most interesting ALLOW in the system, ten minutes from being unlawful
           -- was recorded in the ledger as permitted because nothing cost
           anything.

Mechanism: Three changes. An explicit ALLOW_CITATION_PRIORITY list, ordered by
           specificity with the window first and R3 last (R3 is true of every
           action, so saying it carries no information). bound=False on S6's
           zero-cost branch. And an informative_when_open flag on Bound, so that
           "R5 was considered and does not reach a service message" still cites
           R5 while "no rule governs a silent action" cites nothing.

           Plus a completeness check that fails at import if a rule can bind
           without appearing in the priority list -- otherwise a rule added later
           is silently uncitable on an ALLOW, which is invisible until somebody
           asks why a permitted action names a less specific rule than the one
           that actually constrained it.

           The principle, which was not obvious before this: **an allow needs a
           citation as much as a refusal does.** "Permitted, and here is the rule
           that was closest to forbidding it" is auditable. "Permitted" is not.

Time:      Day 2, block D -- the 18:55 case surfaced it, the full batch confirmed
           the second instance.
```

### [2026-08-26] [severity 4] [layer: ledger / demo probe]

```
Symptom:   The demo printed "tamper probe FAILED -- a mutated row went
           undetected" immediately after adding GATE rows to the ledger.

Diagnosis: Not a hash-chain failure. _tamper_probe corrupts one row of a
           throwaway copy and confirms verify_chain notices. It selected "the
           middle row" and edited an amount_at_risk_paise field in it. That
           worked while every row was a DETECT row. With DETECT and GATE
           interleaved the middle row is a GATE row, which carries no amount, so
           the SQL replace() matched nothing, no row was modified, the chain
           verified correctly -- and the probe reported that as a
           tamper-evidence failure.

           Two failures in one, and the second is the dangerous one. The probe
           had become inert, and it had no way to tell the difference between
           "the chain missed a mutation" and "there was no mutation". Had the
           false negative gone the other way -- had it printed "detected" while
           mutating nothing -- the demo would have been making a false
           tamper-evidence claim on screen to every reviewer, and nothing would
           have flagged it.

Mechanism: The probe now selects from rows that actually contain the field,
           reads the payload before and after the edit, and reports
           "inconclusive" as a distinct outcome if the row did not change.
           Detection is only reported once a real mutation is confirmed.

           And a test, tests/test_ledger_chain.py::
           test_the_demo_tamper_probe_actually_tampers, which builds a mixed-kind
           ledger -- the exact condition that broke it -- and asserts both that
           the outcome is "detected" and that it is never the inconclusive
           branch. The general lesson: a demo that prints a probe is making a
           claim on screen, so the probe needs a test of its own. An assertion
           that can silently stop asserting is worse than no assertion, because
           it also occupies the space where somebody would have written a real
           one.

Time:      Day 2, block D -- first full demo run after GATE landed.
```

### [2026-08-26] [severity 2] [layer: research / regulatory grading]

```
Symptom:   Day 1 recorded R1-R11 as [B] -- secondary summaries -- and named R9
           (debt collection 08:00-19:00) as the weakest, blocking Day 2. R9 was
           checked first. It verified: RBI/2022-23/108,
           DOR.ORG.REC.65/21.04.158/2022-23, 12 August 2022, which prohibits
           "calling the borrower before 8:00 a.m. and after 7:00 p.m. for
           recovery of overdue loans". The figure held and the grade is now [A].

           R5/R8 (promotional and voice, 09:00-21:00) did **not** verify. Every
           source found is secondary. The 21:00-09:00 quiet period appears to
           trace to TCCCPR **2010** and be carried through the 2018 regulation's
           preference schedule rather than existing as a flat prohibition in the
           2018 text. Consistent across sources, and not matched to a clause.

Diagnosis: The interesting part is which rule turned out to be weak. Day 1
           predicted R9 and was wrong: R9 is the one with a circular number, a
           date and a quotable sentence, while the TRAI figure everybody in the
           industry quotes is the one that could not be traced to a primary
           instrument in a reasonable search. The number that felt safe because
           it is universally repeated was the less verifiable of the two.

Mechanism: Recorded the grade *in the rule*, not in a footnote:
           rules.RULE_SOURCES carries an instrument and a grade per rule, and
           _self_check refuses at import to let a rule claim [A] without quoting
           the operative words. So an upgrade requires reading the text.

           **That mechanism was not sufficient, and the fifth entry below is
           what happened next.** The published count is one of eleven, not two.

           Also declined to fix the 08:00-09:00 hour_bucket gap by adding a
           bucket, which was Day 1's proposed remedy. The gap is real and the
           remedy was aimed at the wrong layer: hour_bucket is a cache key, and
           the fix is that the *gate* reads timestamps instead. F7 untouched,
           cache unchurned, and a test pins the case where the coarse feature and
           the gate disagree. Recorded as ADR-013.

Time:      Day 2, before any envelope code -- the STATE.md next-action item.
```

### [2026-08-27] [severity 5] [layer: docs / compliance claim]

```
Symptom:   rules.py's module docstring said "two of eleven are [A]-verified (R9,
           and the R2 thresholds via the framework reference)". RULE_SOURCES, in
           the same file sixty lines below, grades R9 [A] and every other rule
           [B] -- one of eleven. README.md said "nine of the eleven ... still
           graded [B]"; it is ten. STATE.md repeated the wrong figure twice and
           FAILURES.md once. All of it was committed to the public repo.

Diagnosis: R2 cannot be [A] under this project's own rules. Its instrument is
           "Ibid.", inheriting R1's [B] source, and _self_check refuses an [A]
           grade without quoted operative words -- forcing R2 to "A" fails the
           import. So the data structure was right and the prose beside it was
           wrong, in the direction of overstatement, in a project whose stated
           principle is that admitting a summary beats claiming a reading.

           The mechanism built on Day 2 to prevent exactly this drift guards
           RULE_SOURCES. It has nothing to say about a sentence in the docstring
           above RULE_SOURCES, or in the README, or in STATE.md. The guard was
           real and its scope was one data structure; the claim a reader meets is
           prose.

           Severity 5 rather than 4: it would have invalidated a published
           number silently, it was the number carrying the compliance argument,
           and it was wrong in the direction that flatters the project. A
           reviewer who checked the one thing the project invites them to check
           -- count the grades -- would have found the headline overstated by
           100%.

Mechanism: GRADE_A_COUNT and GRADE_B_COUNT are derived from RULE_SOURCES at
           import and never written by hand. A new test,
           test_the_published_grade_counts_match_the_prose, parses "<n> of
           eleven ... [A]" and the [B] counterpart out of rules.py's docstring,
           README.md and STATE.md, and fails if any of them disagrees with the
           dict. It failed on first run against the README, which is the
           evidence it works.

           The general lesson, and it generalises past this project: **a guard on
           a data structure does not protect the sentence next to it, and the
           sentence is what ships.** Any number stated in prose that a reviewer
           could check should be derived from the thing it describes, or tested
           against it. Care is not a mechanism.

Time:      Day 2, verification pass -- found by an auditing session, not by the
           building session. The building session wrote both the guard and the
           sentence the guard could not see, which is the argument for HANDOFF
           section 8 step 7 in one line.
```

### [2026-08-27] [severity 3] [layer: tests / tautology]

```
Symptom:   test_canonical_delegates_to_the_envelope_without_changing_any_value
           asserted canonical.channel_eligibility(x) ==
           windows.channel_eligibility_for_bucket(x). The first function's entire
           body is a call to the second, so the assertion cannot fail. Mutating
           the shared implementation left the test green.

Diagnosis: The underlying claim -- that the Day 1 -> Day 2 handover changed no
           value and therefore invalidated no cached signature -- is true. It was
           verified independently by reimplementing Day 1's function from git
           commit b085fdd and diffing all 90 combinations, and the golden ledger
           head hash is unmoved.

           But it was being pinned by *inheritance*: one unrelated Day 1
           behaviour test happened to cover one of the 90 cells, and the golden
           ledger covered none of the disagreements, because the dev batch is
           entirely legal_context="service". So the property held and the test
           named for it contributed nothing.

           This is the failure mode that makes a suite worse than honest
           ignorance -- the same shape as the Day 2 tamper probe that had
           silently stopped tampering. A test that cannot fail occupies the space
           where somebody would otherwise have written one that can.

Mechanism: Replaced the self-comparison with DAY1_CHANNEL_ELIGIBILITY: all 90
           values, generated by running Day 1's function in a git worktree at
           b085fdd, never hand-edited. The test now compares today's behaviour
           against values recorded *before* the refactor, which is what the
           sentence claims. Added test_the_delegation_test_can_actually_fail,
           which asserts a specific cell differs from the plausible wrong answer
           -- so a future collapse back into tautology trips something.

Time:      Day 2, verification pass.
```

### [2026-08-27] [severity 2] [layer: reported figures]

```
Symptom:   Three counted figures in Day 2's reporting did not reproduce.

           (a) "226 tests, +76 over Day 1's 150". Day 1 as committed (b085fdd)
               collects 141: 140 passed, 1 skipped. With the Day 1 follow-up
               commit (3a8cefd) it is 146 collected, 145 passed. The "150" came
               from Day 1's own STATE.md and was already wrong; Day 2 propagated
               it without measuring. The real delta is +81 passing over 145.

           (b) "Thirteen of the 69 reason codes never appear in a 6,000-event
               batch." At the project seed (42) it is fifteen. The figure was
               inherited from Day 1's FAILURES.md and re-stated in
               test_redteam_envelope.py's docstring and VERIFY.md without being
               re-measured.

               Worth recording precisely, because it is *not* seed-invariant:
               measured across seeds 42/1/7/99/2026 the count is 15/12/11/11/16.
               So the honest statement is "fifteen at seed 42, and never fewer
               than eleven across the seeds tried" -- not a constant.

           (c) The suite reported "0 skipped" today against Day 1's "1 skipped".
               The skip is a conditional inside a ledger tamper case ("value is
               already what it would be set to"); adding GATE rows changed the
               ledger content so the case now has something to mutate. Benign,
               and it means the Day 1 figure and the Day 2 figure were not
               counting the same population.

Diagnosis: All three are inherited numbers restated without re-measurement. None
           changes any argument -- (b) in fact strengthens the case for
           constructing red-team inputs rather than sampling them -- but a
           project whose headline claim is "we report what actually happened"
           cannot carry counted figures it has not counted.

Mechanism: Re-measured all three and corrected them at every site. The test
           baseline is now stated with the commit it was measured at, and the
           unseen-code figure with the seed, because both are only meaningful
           with that qualifier attached.

Time:      Day 2, verification pass.
```

### Not a failure, but worth recording

The envelope refuses **nothing** on the deterministic reason-class map: 6,000
events, 4,905 ALLOW, 1,095 AMEND, 0 REJECT. That is the correct result and it
took a moment to be sure of. The map answers MERCHANT_CONFIG with
ACT_ALERT_MERCHANT and TECH_TRANSIENT with ACT_WAIT, so it never proposes the
things the envelope refuses -- which is exactly what makes it a fair arm B rather
than a strawman, and it means the C-B contrast on Day 5 is measured against a
compliant baseline.

The risk is that a zero on screen reads as an inert gate, so the demo now says
so in prose and points at the red-team suite. **The organic violation rate and
the injected catch rate are different metrics and must never be reported as one
number:** the first measures the planner and will not be zero on Day 5; the
second measures this component and is 100% across one engineered violation per
rule R1-R11.

### Candidate for the form answer

The grade-count drift (severity 5) is now the strongest of the ten recorded, and
it displaces the rule-ordering entry. Reasons: it is the only one that reached the
public repo, it overstated a compliance claim by 100% in the direction that
flatters the project, and the fix names a lesson that generalises past this
codebase -- *a guard on a data structure does not protect the sentence next to
it, and the sentence is what ships.* It also demonstrates the value of the
separate verification session, because the building session wrote both the guard
and the sentence the guard could not see.

The rule-ordering failure (severity 4) is the strongest of the build-time
entries. It is a single reasoning error -- "futility before legality" over-
applied to a rule that was not about futility -- whose symptom was not a wrong
answer but a *wrong citation*, which is the failure mode that matters most for a
component whose entire product is the citation. The fix changed how evaluation
order is reasoned about across the whole envelope, and it produced a stated
principle rather than a patched branch. The tamper-probe entry is a close second
for a different reason: the bug was an assertion that had silently stopped
asserting, which is the class of failure that makes a test suite worse than
honest ignorance.

---

## Day 3 — 2026-08-27

The gate. Five defects, and the two worth reading are the fourth and fifth:
both were metrics that produced a *flattering* wrong answer with nothing raising.

### [2026-08-27] [severity 4] [layer: eval / interval comparison]

```
Symptom:   The definition-of-done requires the money confidence interval to be
           visibly wider than the rate interval -- heavy tails guarantee it, so
           if it is not, the bootstrap is wrong. The check reported
           "money CI relatively wider: NO" on the 6,000-event batch, which by
           its own stated logic meant the bootstrap was broken.

           It was not. The check was.

Diagnosis: Rate and money are in different units, so raw widths cannot be
           compared and something has to normalise them. The first version
           divided each interval's width by *its own point estimate*. On the full
           batch the incremental rate is +0.58pp, so its relative width came out
           at 9.86 while money's was 5.32 -- and the check duly declared the
           money interval narrower.

           The error is conceptual rather than arithmetic. The quantity being
           compared is how precisely each statistic can be *estimated*. Dividing
           by a noisy near-zero point estimate measures the estimate, not the
           precision, and it explodes exactly when the effect is small -- i.e.
           precisely in the case the project actually reports.

Mechanism: Normalise by the **control arm's own level** instead. Arm A's recovery
           rate and arm A's rupees-per-event are both stably estimated and each
           sets the natural scale for its own metric. Now: rate 0.192, money
           1.113 -- the money interval is 5.8x relatively wider, which is the
           expected direction and magnitude.

           Added a second, independent heavy-tail signature that cannot be faked:
           interval **asymmetry** (upper half-width / lower half-width), printed
           and 1.23 on the full batch. A normal approximation is symmetric by
           construction, so an asymmetry away from 1.0 is direct visible evidence
           the interval was read off the resample distribution rather than
           computed from a standard error.

           Severity 4 rather than 3: the check is a build gate that prints its
           verdict on screen, so it would have shipped a run announcing its own
           bootstrap was wrong.

Time:      Day 3, block C.
```

### [2026-08-27] [severity 5] [layer: eval / cost metric]

```
Symptom:   "recovered per contact: Rs 50,166.62" on the dev batch, against a
           total of two messages sent and a per-message cost of 15 paise.

Diagnosis: The metric was `recovered_paise / contacts` -- the arm's **entire**
           recovered value, organic recovery and silent retries included, divided
           by the number of messages it happened to send. With two contacts in a
           67-event arm it attributed roughly half the arm's recovery to each
           message.

           PRD 10.3 argues that rupees-recovered-per-contact is the objective the
           system should be held to, ahead of cost per rupee of compute. So this
           was not a decorative number: it was the headline figure for the
           project's own stated objective, and it was wrong by roughly three
           orders of magnitude in the flattering direction.

Mechanism: Added `contact_attributed_paise` -- value recovered where the *cause*
           was a contact, which the resolver already records because attribution
           goes to whichever recovery path fired first. The corrected full-batch
           figure is Rs 155.83 per contact, against PRD 10.3's central-scenario
           estimate of roughly Rs 100. The field carries a comment recording that
           it existed as a bug first.

           Severity 5, and it is the highest of the day: nothing would have
           raised, the number was on screen in the standard output, and it was
           wrong in the direction that flatters. That combination is the exact
           failure mode this project's whole measurement layer exists to prevent,
           found inside the measurement layer.

Time:      Day 3, block C.
```

### [2026-08-27] [severity 3] [layer: eval / sensitivity table]

```
Symptom:   The organic-recovery sensitivity table -- which BUILD-PLAN Day 3
           requires in the standard output specifically to answer "your simulator
           is made up" -- printed four non-monotone rows:

               15% -> +1.96pp   30% -> +0.53pp   50% -> +2.15pp   70% -> +0.92pp

           As printed it looked like a bug and proved nothing.

Diagnosis: Each row was a fresh arm-based *estimate*, carrying its own sampling
           noise. At 2,000 events per arm each interval spans about 5pp, which is
           wider than the entire range the underlying quantity moves across. The
           table was showing noise where it was supposed to show a trend.

           The underlying estimand is clean and monotone, and it always was:
           +3.25pp -> +3.08pp -> +2.85pp -> +2.33pp across the same sweep. A 28%
           decline, and non-proportional, which is exactly the point the table
           exists to make -- under a per-action uplift model it would be flat.

Mechanism: Print both columns. The **estimand**, computed exactly from both
           potential outcomes with no sampling error, answers "how much does this
           assumption matter". The **estimate with its interval** answers "could
           production tell these scenarios apart", and the honest answer is no.

           Printing only the estimand would overstate what the experiment can
           resolve; printing only the estimate hides the trend inside the noise.
           The distinction between an estimand and an estimate turned out to be
           the difference between a table that argues something and a table that
           looks broken.

Time:      Day 3, block C2.
```

### [2026-08-27] [severity 3] [layer: docs / inherited claim]

```
Symptom:   A test asserting the arm split is "near-exact at every prefix of the
           stream" failed: spread 31 across arms at a 300-event prefix of the
           3,000-event batch, against a claimed bound of one event per stratum.

Diagnosis: The claim came from Day 1's `ArmAssigner` docstring and is false for
           the data as emitted. Permuted blocks do bound the within-stratum
           spread at one -- in the order arms are **assigned**. But `generate`
           sorts events by `detected_at` before returning them, so a time-ordered
           prefix is effectively a random subset of the assignment order and
           inherits no block guarantee at all.

           Measured both ways at a 300-event prefix: spread 2 in assignment
           order, 31 in time order.

           No published figure depended on it, because nothing in the project
           analyses a truncated batch -- the full-batch balance (2003/2001/1996)
           and the within-stratum bound are both real and both still hold. The
           docstring was simply asserting something stronger than the code
           delivers, and it had been carried unchallenged for two days.

Mechanism: Corrected the docstring to state the property that holds and to name
           the one that does not, with the measured figures. Split the test in
           two: one asserting the real bound in assignment order, and one
           asserting the honest property for the emitted order -- that
           time-ordered imbalance *converges* even though it is not bounded,
           because if it did not, assignment would correlate with detection time
           and the experiment would be confounded rather than merely
           mis-documented.

Time:      Day 3, test pass.
```

### [2026-08-27] [severity 2] [layer: eval / power arithmetic]

```
Symptom:   The power self-check refused to import: PRD 8.1 publishes 8,241 events
           per arm to detect a 2pp lift; the implemented formula returns 8,242.

Diagnosis: The exact value is 8,241.32. The PRD rounded; a sample size is a floor
           requirement, so the correct operation is a ceiling. Two of the three
           published rows (330, 1,319) are unaffected because their exact values
           round and ceiling identically.

Mechanism: Kept the ceiling, which is the correct arithmetic, and pinned all
           three rows in `arms._self_check` with a comment recording the
           disagreement and why the code wins. Recorded here rather than silently
           adjusting either side.

           Severity 2 and the magnitude is one event out of eight thousand. It is
           logged because the alternative -- quietly changing the formula to match
           a published number -- is the same move that produced the severity-5
           compliance-count error at the end of Day 2, and the habit matters more
           than this instance.

Time:      Day 3, block A.
```

### [2026-08-27] [severity 2] [layer: eval / bootstrap resample count]

```
Symptom:   The 6,000-event batch -- the one whose figure gets published -- ran its
           headline bootstrap at 2,000 resamples, while the 200-event dev smoke
           test ran at 10,000. PRD 8.1 specifies 10,000.

Diagnosis: A single conditional written backwards. The intent was "use the
           reduced count where the number does not need to be precise", and it
           was applied to exactly the wrong batch.

Mechanism: 10,000 for the headline contrasts on every batch. The reduced count is
           now used only for the two sweeps, where the point is the direction of
           movement across rows rather than a third significant figure, and it is
           labelled on screen with the count actually used. Full-batch runtime
           went from 63s to about 105s, which is acceptable for something run once
           a day.

Time:      Day 3, block D.
```

### Not a failure, but worth recording

**A test written to a threshold instead of to a property.** An early version of
the bootstrap asymmetry test asserted `|upper/lower - 1| > 0.05` on log-normal
data at 1,000 observations per arm, and measured 0.955 -- failing by 0.005. The
temptation was to relax the threshold. The actual reason is the central limit
theorem: the statistic is a *difference of two means*, so its sampling
distribution converges to normal as n grows however skewed the amounts are, and
asserting asymmetry at n=1,000 is asserting something theory says should be
absent.

Replaced with two tests: one asserting pronounced asymmetry at a sample size and
tail weight where the skew genuinely survives into the contrast, and one asserting
that asymmetry **decays as n grows** — which is the stronger claim, because it
tests the mechanism rather than a number, and it distinguishes real skew from an
implementation artefact that merely looks like skew.

**The headline number is not significant, and that is the pre-registered
outcome.** Intent-to-treat B−A is +0.58pp [−2.27, +3.43] on 6,000 events. PRD 8.1
pre-registered the kill condition, so the honest reading was fixed in advance
rather than negotiated after seeing the interval. The diagnosis is specific and
printed: arm B answers 72.1% of volume with `ACT_WAIT`, where the true effect is
exactly zero, so a real +3.02pp effect is measured through a sample in which
three-quarters of the observations are known-null. On the pre-specified actioned
subset the same experiment gives +9.22pp [+4.96, +13.46].

**A change that was available and was not made.** `REASON_CLASS_POLICY` marks
TECH_TRANSIENT as `retry_mode="immediate"` with the note "silent retry only", yet
`DEFAULT_ACTION_BY_CLASS` answers it with `ACT_WAIT`. TECH_TRANSIENT is 47.9% of
volume with a 90% capability-clearance rate, so changing that one entry would have
raised the headline substantially. It was not changed: the map is a published Day 2
artifact, and editing it after seeing which edit would improve the number is
precisely how measurement becomes advocacy. Recorded in EVALUATION.md §6 as a real
tension in the taxonomy and a Day 5 question.

### [2026-08-27] [severity 3] [layer: verification / statistical inference]

```
Symptom:   Nearly shipped STATE.md with a "Blocked" item reporting that the
           project's bootstrap intervals under-cover -- "measured BCa coverage is
           90-92%, not 95%, consistent across sample sizes, so it is systematic
           rather than noise". The claim was false.

Diagnosis: Three coverage runs at 200, 120 and 60 seeds returned 92.0%, 90.0% and
           91.7%. Their standard errors are 1.5, 2.0 and 2.8 pp respectively, so
           each result sat between 1.2 and 2.5 sigma from nominal -- individually
           marginal, none decisive.

           The error was treating three marginal estimates as confirmation of
           each other. They are not independent evidence of a trend; they are
           three noisy measurements of the same quantity, and the right response
           to three noisy measurements is one precise measurement rather than a
           conclusion.

           A second run happened to make this obvious: BCa, percentile and Wald
           all returned *identical* coverage (95.3%) on a different seed range.
           Three different interval methods cannot share a defect, so the
           variation was clearly the seed set rather than the estimator.

           Re-measured at 600 seeds: 95.8%, SE 0.9 pp, 0.9 sigma from nominal.
           Wald on the same batches gives 96.8% -- slightly conservative, which is
           its documented behaviour. The bootstrap is correctly calibrated.

Mechanism: Corrected the claim in STATE.md and EVALUATION.md before commit, with
           the reasoning left in place rather than deleted, so the investigation
           is carried forward instead of the conclusion alone.

           The existing coverage test keeps a deliberately loose floor (86% at 50
           seeds, where SE is 3.1 pp) and now says in its docstring why a tighter
           assertion would be wrong. A test asserting 95% at 50 seeds would fail
           roughly a third of the time on noise -- the same trap, encoded.

           Severity 3 rather than 2: the claim would have reached a reviewer as a
           self-reported defect in the component whose whole job is to be
           trustworthy, and the correct reading of the evidence was available
           from the standard errors, which were sitting in the harness output.

Time:      Day 3, verification pass.
```

### Candidate for the form answer

The severity-5 contact-attribution bug is the strongest entry in the log so far,
and it is stronger than Day 2's grade-count drift for one reason: it was found
**inside the measurement layer**, by the discipline that layer exists to enforce.

"Rupees recovered per customer contact" is the objective PRD 10.3 argues the whole
system should be held to. The implementation divided the arm's entire recovered
value — organic recovery included — by the number of messages sent, and printed
Rs 50,166 per contact. Nothing raised. The number was on screen in the standard
output, it was wrong by three orders of magnitude, and it was wrong in the
direction that flatters.

What caught it was not a test. It was reading the output and noticing that two
messages at 15 paise could not plausibly have recovered a lakh of rupees. Which is
the argument for printing cost breakdowns next to ratios, and against trusting a
metric because it has a plausible name — a metric named after the right objective
is not the same thing as a metric that measures it.

---

## Day 4 — 2026-08-28

Six defects. The first is the worst thing found in the project so far and it was
not found by a test: the two model IDs the project had carried for three days had
been switched off twelve days earlier. Three of the remaining five are the same
shape as Day 3's worst — a check that looked like it worked and did not.

---

## Day 5 — 2026-08-29

### [2026-08-29] [severity 3] [layer: plan / parsing]

```
Symptom:   A test asserting that a dropped, unreadable step does not sink the
           steps around it (`test_a_partially_malformed_step_list_keeps_the_
           valid_steps`) failed: the surviving step came back with
           `step_index=1`, not `0`, even though it was the only step in the
           plan.

Diagnosis: `_parse_plan` enumerated the model's raw step list and passed the
           enumeration index straight to `_coerce_step` — including the index
           of the step that was about to be dropped. So a plan whose first
           step failed coercion produced a surviving plan with a hole at
           position zero, and a plan with several bad steps scattered through
           good ones would come back with a non-dense, possibly confusing
           index sequence.

Mechanism: Nothing downstream actually reads `step_index` for control flow —
           `arm_step` takes `plan.steps[0]` by list position, not by the
           field's value, and `RecoveryPlan`'s own ordering validator only
           requires ascending, not dense-from-zero. So this was not a
           correctness bug in anything that shipped; it is caught here,
           before a step-index gap could ever reach a printed report or a
           PLAN ledger row and read as a hole in the plan rather than a
           dropped, invalid step.

Fix:       `_parse_plan` now assigns `_coerce_step`'s index from the count of
           steps that have already survived, not from the loop position over
           the raw list. A plan with N valid steps always numbers them
           0..N-1, regardless of how many invalid steps were interleaved.

Time:      Day 5, block A.
```

### [2026-08-29] [severity 3] [layer: plan / validate]

```
Symptom:   A test for `PlanJudgement.first_allowed_index` on a plan whose one
           step gets AMENDed (an immediate FUNDS retry, deferred by G7) failed:
           the property returned `None` — "nothing would execute" — on a step
           the envelope had, in fact, corrected and would run.

Diagnosis: `first_allowed_index` was written to check `judgement.allowed`,
           which is `True` only for a clean ALLOW (`envelope/context.py`).
           AMEND is a separate verdict, and `Judgement.allowed` does not (and
           should not) include it — the bug was in `plan/validate.py`
           assuming a name meant more than it does, not in the envelope. The
           docstring already said "first non-terminal ALLOW/AMEND step"; the
           code did not match its own docstring.

Mechanism: The general shape is worth naming: a boolean-sounding property
           (`allowed`) invites being read as "would go ahead", when its actual
           contract is narrower ("the envelope did not have to touch this").
           A caller composing a new query on top of an existing property has
           to re-read what it actually promises, not what its name suggests.

Fix:       `first_allowed_index` now checks `verdict in (ALLOW, AMEND)`
           directly, with a comment explaining why `.allowed` is the wrong
           tool for this specific question.

Time:      Day 5, block B.
```

### Not a failure, but worth recording

**A CI that correctly excludes zero, on a contrast that is exactly zero.** The
first full-batch run of `execute --full` printed C−B as +3.83pp with a 95% CI of
[+0.93, +6.64] — a result that reads, at a glance, like the LLM beating the
lookup table. There is no LLM in this run: with no API key, every arm-C plan is
the NFR-2 deterministic fallback, which is *byte-identical* to arm B's own
policy for every event (checked directly — zero mismatches across the full
6,000-event batch, and confirmed again via `potential_outcomes(events, "B",
"C")`, whose exact ATE is 0.00pp on every one of rate, value_share and
money_per_event). So the true effect is exactly zero and the estimated
contrast, on this one random split of events into arms, landed outside its own
95% interval's usual coverage of zero — which is not a bug, it is what "95%
confidence" is a claim about: roughly one split in twenty will do exactly this.

Left unlabelled, this is a false discovery waiting to be quoted. The fix is not
statistical, it is presentational: `pramaan.execute.runner` now computes and
prints the *exact* C−B effect (via `potential_outcomes`/`true_effect`, the same
machinery Day 3 built to validate the B−A estimator) directly beneath the
*estimated* C−B contrast, so a reviewer sees both numbers together rather than
being handed a CI with no way to tell "this is signal" from "this is noise
around a null that has not been given anything to detect yet." The same check
is now a standing test
(`test_arms.py::test_arm_c_true_effect_against_b_is_zero_with_no_llm`), so the
day an API key makes the true effect genuinely non-zero, that test's failure is
the record of the day arm C started actually deciding something.

**The Day 3 "no LLM import" boundary test had to be narrowed, not deleted.**
Wiring arm C put a real, necessary import edge from `pramaan.eval.arms` into
`pramaan.llm.client` — arm C *is* the LLM-planned arm, so there is no version
of wiring it that avoids this. `test_the_eval_layer_has_no_import_edge_into_
the_llm_package` failed, correctly, the first time the full suite ran against
the new code. Recorded as ADR-036 rather than here, because the fix was a
reviewed, deliberate scope change to an existing invariant (one named, lazy,
tested-as-confined exception) and not a defect in anything — but it is the
kind of test failure worth being honest about surfacing rather than quietly
loosening the assertion until it passed.

### [2026-08-29] [severity 3] [layer: plan / llm client]

```
Symptom:   The first live planning pass (dev batch, real Groq + OpenRouter
           keys, the human supplied them mid-session) crashed with an
           unhandled RuntimeError after ~71 signatures: "every provider for
           tier fast failed; last error: openrouter returned 3 consecutive
           429s; failing over." The whole batch run died; nothing after the
           crash was resolved, and the command exited non-zero with a
           traceback instead of a report.

Diagnosis: `Planner._build` caught exactly one exception type from
           `client.call()` -- `CacheMiss`, the pure offline case (no key, no
           cache). `llm.client._call_live` raises a plain `RuntimeError` once
           every candidate provider has failed over from consecutive 429s,
           and nothing caught that at the planner layer. A free-tier rate
           wall is not a hypothetical; it is the first thing BUILD-PLAN's own
           arithmetic section warns will happen, and it happened on the
           first live run this project ever made.

Mechanism: NFR-2 says a cache MISS must never block the first action.
           A rate-limit wall reached *after* a network attempt is the same
           situation with a different cause -- no LLM answer is available for
           this signature right now -- and the fix had only handled the
           "before any attempt" half of that statement.

Fix:       `Planner._build` now also catches any other exception from
           `client.call()`, degrades to `default_plan_for` exactly as a
           `CacheMiss` does, and records it under a new, separate counter
           (`PlannerStats.provider_failures`) so the two causes are
           distinguishable in the printed report rather than conflated.
           `tests/test_planner_memoisation.py::
           test_a_provider_failure_after_a_live_attempt_also_falls_back`
           pins the behaviour with a client that always raises.

           Re-run after the fix: the same dev batch completed end to end,
           gracefully falling back for 8 of 80 signatures that hit the wall,
           with a clean exit and a full report. The full 6,000-event batch
           then completed the same way, falling back for 197 of 273 --
           the wall was still up, and the run finished anyway.

Time:      Day 5 evening, first live run.
```

### [2026-08-29] [severity 2] [layer: plan / prompt-schema mismatch]

```
Symptom:   Of 71 live responses cached in the first run, only 1 parsed into a
           valid plan. Inspecting the raw responses: 19 carried well-formed
           JSON with a `steps` array and a legal `action`, but 18 of those 19
           used a `channel` value outside the closed vocabulary --
           "silent", "internal", "merchant", "engineer", and one plain
           case mismatch, "SMS". (The other 52 of 71 never reached JSON at
           all -- see the "not a failure" note below; a separate cause.)

Diagnosis: `PLANNER_INSTRUCTIONS` (`pramaan/llm/prompts.py`, written Day 1)
           never enumerates the `channel` field's allowed values anywhere --
           the JSON example shows `"channel": "..."` as a bare placeholder.
           For an action that never reaches a customer (`ACT_WAIT`,
           `ACT_RETRY`, `ACT_ALERT_MERCHANT`, `ACT_PAGE_ENGINEER`,
           `ACT_STOP`), the model reasonably describes *why* there is no
           channel rather than writing the schema's literal "none" -- it was
           never told "none" was the word to use.

Mechanism: `_coerce_step` treated `channel` as load-bearing for every action
           uniformly, so a synonym on a non-contact action sank the whole
           step even though the envelope never reads `channel` for a
           non-contact action in the first place -- the value was
           functionally irrelevant and rejected anyway.

Fix:       `_coerce_step` now only validates `channel` strictly for actions
           in `CONTACT_ACTIONS` (`ACT_MESSAGE`, `ACT_VOICE`,
           `ACT_CONCESSION`); every other action's channel is forced to
           `"none"` regardless of what the model wrote, and a small,
           named alias table (`_CHANNEL_ALIASES`) normalises the two
           observed near-miss spellings that DO occur on a contact action
           ("whatsapp business", case variants). This is a parsing fix, not
           a prompt change, and deliberately so: it did not invalidate the
           71 already-cached, already-paid-for responses. Re-parsing them
           after the fix raised `llm_built` from 1 to 19 with zero new
           network calls.

           The prompt gap that caused the model to reach for a synonym in
           the first place is still open -- `PLANNER_INSTRUCTIONS` still does
           not enumerate the channel vocabulary. Left for a future prompt
           iteration rather than fixed today, because changing the prompt
           text invalidates every entry in the committed cache (the key is
           `sha256(model + params + prompt)`), and the free-tier budget this
           session had already been through one rate-limit wall.

Time:      Day 5 evening, same live run.
```

### Not a failure, but worth recording

**One of the two failover models is unsuitable for this prompt's token
budget, and that is a fact about the model, not a bug.** 52 of the first
run's 71 responses never produced parseable JSON at all -- the raw text is a
long, well-formed chain-of-thought preamble ("Here's a thinking process: 1.
Analyze...") that consumes the entire 900-token completion budget and is cut
off before any answer follows. All 52 came from
`nvidia/nemotron-3.5-lightning:free` on OpenRouter, the "fast" tier's
failover; the 19 that produced JSON all came from Groq's
`openai/gpt-oss-20b`, the primary. Groq was rate-limited early and often
during this session (both live runs), which is why the failover carried most
of the traffic and why the parse-failure rate looks as bad as it does --
it is really a report on one specific model's fit for a tight,
structured-output budget, not on the planner's prompt or parser. Worth
revisiting on a future day: either raise `max_tokens` for this one model, add
an explicit "no chain-of-thought, JSON only" instruction, or pick a
non-reasoning free model as the OpenRouter failover.

**A CI that correctly excludes zero, on a contrast that was, for one run,
exactly zero -- and then, after the fixes above, was not.** The first
full-batch run (before the two fixes) printed C−B as +3.83pp on an exact,
verified-zero true effect; recorded above under "Day 5's own version of
measured, not assumed" in STATE.md, and the mechanism -- printing the exact
true effect alongside the estimate -- is what caught it rather than a human
noticing. After the crash fix and the parsing fix, the *same* full batch's
true C−B effect is **+13.08pp**, no longer zero, because the planner now has
24 genuinely LLM-authored plans in its signature cache. Both readings were
correct at the time they were taken; the second is the one worth
publishing, and it exists only because the machinery built for the first
case (`potential_outcomes`/`true_effect` at the plan layer) kept working
after the underlying facts changed.

### [2026-08-28] [severity 5] [layer: llm / model identifiers]

```
Symptom:   `pramaan/llm/client.py` named `llama-3.3-70b-versatile` (strong tier)
           and `llama-3.1-8b-instant` (fast tier). Both had been **shut down by
           Groq on 2026-08-16**, announced on 2026-06-17. Today is 2026-08-28, so
           the project's only two configured models had been dead for twelve days.

           Nothing had noticed, for the reason that made it possible: Days 1-3
           consumed zero tokens by design, so the first call that would have
           failed was the first call ever made -- scheduled for today.

Diagnosis: The IDs were carried as an open item ("Model IDs are unverified.
           Nothing has hit either API") in STATE.md from Day 1, with a comment in
           the source reading "verify these against the live free-tier lineup".

           That is the defect. A comment instructing a future reader to verify
           something is not a verification, and an open item that says "this
           becomes blocking on Day 4" is a note to a person, not a check. The
           project had a *reminder* where it needed a *test*, and the reminder
           worked exactly as well as reminders do -- it was read on the correct
           day, twelve days after the fact it described stopped being true.

           Worth being precise about the counterfactual, because it is
           uncomfortable: had the first live call been attempted on Day 3 evening
           to warm the cache, the run would have failed on a 404 against a
           retired model, and the natural reading at 11pm would have been "the
           key is wrong" or "the client is broken" -- neither of which is where
           the problem was.

Mechanism: Two changes, and only the second one is durable.

           1. Updated to the replacements Groq's own deprecation notice names:
              `openai/gpt-oss-120b` for the strong tier, `openai/gpt-oss-20b` for
              the fast tier. Refreshed the OpenRouter failovers from its live
              free-models collection on the same date -- and note that *both*
              previous OpenRouter entries were also gone from that roster, along
              with gpt-oss entirely, so the failover path was independently
              broken.

           2. Added `python -m pramaan.cli models` (`make models`), which prints
              every configured ID and, when a key is present, asks each provider's
              `GET /models` what it actually serves and reports each ID as PRESENT
              or MISSING. It makes no completion call and consumes no tokens, so
              there is no reason not to run it.

           The second is the point. A free-tier lineup changes without notice, so
           the correct artefact is not a corrected constant -- that has an expiry
           date too -- but a command that re-derives the answer. `MODELS` now also
           records `MODELS_VERIFIED_ON` and the two source URLs, so the next
           reader re-checks rather than re-researches.

           Severity 5: the whole day's deliverable ran through a code path whose
           configured models did not exist, and the failure was scheduled to land
           in the highest-pressure hour of the project.

Time:      Day 4, before block B. Found by checking a carried open item against
           the provider's documentation rather than against the code.
```

### [2026-08-28] [severity 4] [layer: prompts / canonicality screen]

```
Symptom:   `get_reason_taxonomy` -- the tool whose stated purpose (PRD 6.2) is to
           ground the agent in real reason-code semantics -- could not render its
           output into a prompt. `assert_no_identifiers` refused it:

               tool result (get_reason_taxonomy) contains a razorpay-style
               identifier: 'order_already'

Diagnosis: The Day 1 screen matched `\b(?:pay|order|sub|...|run)_[A-Za-z0-9]{3,}`,
           i.e. any token starting with an identifier prefix and an underscore.
           Three of the 69 Razorpay reason codes are shaped exactly that way --
           `order_already_paid`, `order_amount_mismatch`,
           `order_payment_method_mismatch` -- as is this project's own future
           tool name `run_canary`.

           Those are closed-domain enum values, not identifiers. Refusing them is
           a false positive, and an expensive one: `cause_signal` is a column in
           the agent's own projection, so *any* query grouping by reason code
           renders codes into prompt bytes. The screen was refusing the domain
           vocabulary the investigator exists to reason about.

           Nothing caught it for three days because nothing had yet put a reason
           code in a prompt. Day 1 built the screen, Day 1 built the taxonomy, and
           the two never met until an agent needed both.

Mechanism: Discriminate on identifier *shape* rather than on the prefix alone. A
           Razorpay id is base-62 random (`pay_29QQoUBi66xm2f`) and therefore
           carries a digit or a capital; a reason code is lowercase words joined by
           underscores and carries neither. Added a second pattern as a backstop
           for the all-lowercase id -- a real id is one long unbroken run of
           alphanumerics, and the longest single word after an id prefix anywhere
           in the taxonomy is seven characters, so a twelve-character run is a wide
           margin.

           Loosening a guard requires proving the guard still holds, so two tests
           landed with the change in `tests/test_prompt_canonical.py`: every one of
           ten real identifier shapes is still refused (including the
           all-lowercase case), and every one of all 69 taxonomy codes is now
           promptable.

           Severity 4 rather than 3 because of what the tempting fix would have
           been. The obvious move at the point of discovery is to relax the screen
           *for tool output* -- and PRD 9.1 is explicit that a weakened
           canonicality guarantee is the failure mode no test catches: the system
           keeps working, every call becomes a cache miss, and the budget goes from
           ~800K to ~15M silently. The screen stayed byte-for-byte strict at the
           chokepoint; only its definition of "identifier" got more accurate.
```

### [2026-08-28] [severity 4] [layer: investigate / detector baseline]

```
Symptom:   The detector was run against a deliberately injected two-day incident
           (days 3-4) and returned a **one-day** window, `(3,)`. It found the
           incident and got its extent wrong.

Diagnosis: Each day was compared against the three days immediately preceding it.
           On day 4, one of those three is day 3 -- the incident's own first day --
           so the baseline rises, the measured elevation falls below the 3pp
           threshold, and day 4 is not flagged.

           The consequence generalises: a multi-day degradation is *always*
           detected as its first day only. And nothing raises. The detector returns
           a window, the investigation runs happily, the decomposition is computed
           over part of the episode, and every figure the diagnosis quotes is
           quietly wrong about a real incident. On the injected case the blended
           rise came out at +9.4pp over one day against a true +7.3pp over two --
           an *over*-statement, which is the flattering direction.

Mechanism: The baseline must be the last CLEAN stretch, not simply the preceding
           days. It now freezes at the stretch that was quiet before the run
           started and stays frozen for as long as consecutive days keep flagging.
           The detector then returns `(3, 4)`, exactly the injected window, and
           `tests/test_investigator_loop.py` asserts equality against the
           incident spec rather than mere non-emptiness.

           Only findable because the ground truth existed. A detector run against
           real data returns a window and there is nothing to compare it to --
           which is the argument for building the injected incident with recorded
           truth before building the thing that looks for it.

Time:      Day 4, block B, first end-to-end run.
```

### [2026-08-28] [severity 4] [layer: investigate / receipt auditor tolerance]

```
Symptom:   The auditor check that verifies a claim's numbers appear in its cited
           evidence cleared a fabricated claim: "tier3's failure rate rose 41.0pp"
           against evidence recording 41.7%.

Diagnosis: Two separate problems, found one behind the other.

           The first was calibration. The tolerance was 2% *relative*, which at
           the 41.7 level is +/-0.83 percentage points of slack -- so a claim a
           third of a point wide of the truth verified. A single relative
           tolerance cannot serve both scales the same quantity appears on: it is
           0.417 in a result payload and 41.7% in a claim, and any absolute floor
           generous enough for the first is five percentage points on the second.

           Fixed by normalising both sides to one scale -- percentage points --
           before comparing, then applying one tolerance (0.15pp absolute, 1%
           relative). The tools render rates to one decimal, so a claim quoting
           what it was shown lands inside 0.15, and the relative term takes over
           for counts where quoting 1,238 as 1,240 is accuracy rather than
           invention.

           The second problem is that the test *still* passed after the fix, and
           the reason is more interesting than the first: **41.0 really is in that
           result** -- it is tier2's window rate. The claim attached a genuine
           number to the wrong entity, and a check that asks "is this figure in
           the evidence" cannot catch that.

Mechanism: The tolerance is fixed and the second limit is documented rather than
           papered over, because binding a number to its grammatical subject is
           not a mechanical operation -- it would require a language model inside
           the verifier that exists to verify a language model.

           `test_number_present_but_attached_to_the_wrong_entity_is_a_known_limit`
           asserts the *limit*, with a failure message telling whoever implements
           subject binding to update the claim in `receipts.py` and delete the
           test. A documented hole that a test keeps honest beats a docstring
           nobody re-reads.

           Severity 4: this is the check that guards the only fabrication mode
           which actually moves money -- a planner reads a magnitude, not a
           citation -- and it was admitting fabrications while reporting a clean
           audit.

Time:      Day 4, block D.
```

### [2026-08-28] [severity 3] [layer: sim / incident sizing]

```
Symptom:   The first sizing of the injected incident -- 180 extra failures and 900
           extra attempts onto the 200-event dev batch -- drove tier2's in-window
           failure rate to a clamped **1.000**. Every tier2 payment failing, for
           two days.

Diagnosis: 200 events over ~6 days across 3 segments is ~11 failures per
           (day, segment) slice. Any injection large enough to clear sampling
           noise on a slice that thin is larger than the slice, and the numbers
           were chosen by feel rather than computed from the counts.

           The consequence is worse than implausibility. A rate pinned at 1.000 by
           a clamp cannot represent the injected effect, so the decomposition had
           nothing to attribute and the incident was **invisible to the very tool
           built to find it** -- while the traffic table still returned a
           well-formed rate for every slice. Nothing raised.

           This is the same arithmetic Day 3 already recorded from the other end:
           minimum detectable effect 22.2pp at 67 events per arm. The dev batch is
           too small for a *realistic* incident to be detectable, and that is a
           property of the batch, not of the detector.

Mechanism: Sized backwards from the actual per-slice counts instead: 38 extra
           failures puts tier2 at +11pp, and 300 extra attempts moves tier3 from
           9.3% to 26.6% of attempts. On a 1,200-event batch over 8 days that
           yields a +7.3pp blended rise splitting into +3.8pp rate and +3.5pp mix,
           with no slice exceeding a 46% failure rate.

           The batch grew from 200 to 1,200 events and the token cost did not
           change at all, which is the point worth keeping: the investigator's cost
           is one session per *incident*, and SQL over 1,200 rows is free. BUILD-PLAN
           Day 4's "run the 200-event dev batch only" is a constraint on incidents
           (4-6, not 40), not on rows.

           `test_incident.py` now asserts no failure rate exceeds 1.0 and that
           attempts >= failures on every row.

Time:      Day 4, block A.
```

### [2026-08-28] [severity 2] [layer: tests / injected clock]

```
Symptom:   The test for the 5-second SQL timeout reported the query as having
           succeeded, and a first attempt at fixing it produced
           `RuntimeError: generator raised StopIteration` from inside SQLite's
           progress handler.

Diagnosis: Two small things, both about the seam rather than the logic.

           `ToolBelt.call` reads the injected clock once for its own elapsed-time
           accounting *before* `query_sql` reads it to set the deadline. A clock
           returning a large value on its second read therefore puts the deadline
           ahead of every subsequent read, and nothing ever aborts.

           And the first fix used a finite iterator. The progress handler fires an
           unpredictable number of times, so it ran off the end, and a
           StopIteration raised inside a C callback surfaces as an unrelated
           RuntimeError several frames away.

Mechanism: A strictly increasing unbounded callable. Both traps are written into
           the test's docstring, because the next person to inject a clock into
           this code will hit the first one and the error message points nowhere
           near the cause.

           Kept at severity 2 -- it never affected production behaviour -- but
           recorded because a timeout test that silently passes for the wrong
           reason is a timeout that is not tested, and the 5s ceiling is the only
           thing standing between a model-written cross join and a stalled run.

Time:      Day 4, block D.
```

### Not a failure, but worth recording

**The event store could not compute a failure rate, and three tools depended on
one.** `events` holds failures only; there is no `attempts` anywhere in the Day
1-3 schema. So "failure rate" was not a computable quantity, which means
`compare_baseline`, `decompose` and the detector would all have been facades over
data unable to support them. A segment's failure *count* rising tells you nothing:
it rises when that segment sends more traffic.

Not a defect, because nothing had claimed otherwise — Days 1-3 never needed a
rate. But it is the kind of gap that only appears when something tries to use the
data, and had it been noticed a day later it would have been noticed as
"decompose returns strange numbers" rather than as a missing table. Attempts are
now *derived* from observed failures against a per-segment baseline anchored to
PRD 6.3's published ranges, so the denominator cannot drift free of the numerator
it has to stay consistent with.

**The stream had no incident in it at all.** `generate()` draws every event
i.i.d. — reason code, segment and hour independent of each other and of calendar
time. Correct for Days 1-3, whose job was to measure an estimator against a known
null. But it means an investigator run against it could only ever correctly
conclude "nothing is happening", and the Day 4 definition-of-done item "it
correctly identifies the injected degradation" had no injected degradation to
identify. Building the world before the thing that looks at it also produced the
detector-baseline defect above, which is the strongest argument for that ordering.

**Three of the six tools were held back deliberately, then all six shipped.**
BUILD-PLAN Day 4 says to ship three if behind by lunch. All six landed, and the
one that nearly justified the cut was `get_reason_taxonomy` — not because it was
hard, but because it was the tool that surfaced the canonicality defect. Cutting
it would have shipped a screen that silently refuses reason codes into Day 5,
where `cause_signal` reaches the planner prompt.

### Candidate for the form answer

The dead model IDs are the strongest entry in this log, and they are stronger than
Day 3's contact-attribution bug for a reason that has nothing to do with severity
arithmetic.

Day 3's worst defect was a wrong number. This one was a wrong *belief about the
world*, held for three days, recorded in the project's own state file as an open
item, with a comment in the source instructing a future reader to check it. Every
mechanism for catching it existed and none of them was a check. The item said
"this becomes blocking on Day 4" and it was correct — it just did not say that the
thing it described had already been false for twelve days.

The fix that matters is not the corrected constant. It is `make models`, which
asks the provider what it serves and costs nothing to run. The general lesson is
narrow and worth stating plainly: **a fact with an expiry date should be stored as
a query, not as a constant with a reminder attached.** Everything in this project
that is a constant — the reason taxonomy, the amount bands, the regulatory windows
— is a constant because it is stable. Model availability on a free tier is not,
and it was being stored as though it were.

## Day 6 — 2026-08-29

### [2026-08-29] [severity 4] [layer: ledger / tests]

```
Symptom:   The full suite went green after Day 6's changes, then one test broke
           on a second full run: `test_ledger_rejects_an_unknown_kind` failed
           with "DID NOT RAISE <ValueError>".

Diagnosis: The test's own example of "a kind the ledger must refuse" was the
           literal string "CANARY" — a reasonable choice on Day 5, when no
           such kind existed. Day 6 gave CANARY a real writer
           (`pramaan.investigate.canary.write_canary_result`) and added it to
           `LEDGER_KINDS`, so the exact string the test used to prove
           rejection became, in the same commit, a string the ledger must
           accept. The test was correct when written and wrong the moment its
           chosen example stopped being hypothetical.

Mechanism: `LEDGER_KINDS`'s own docstring already states the rule this
           violates in spirit — "a kind joins the enum on the day its writer
           lands" — but nothing had stated the *dual* of that rule for a test
           asserting the negative: an example used to prove a kind is
           *rejected* has to be a string that can never legitimately become a
           kind, not merely a string that is not one yet. "CANARY" satisfied
           the second reading and not the first.

Fix:       Changed the test's example to `"NOT_A_REAL_KIND"`, with a comment
           naming exactly this incident so the next reader does not repeat
           the choice that broke here. No change to `LEDGER_KINDS` or its
           writer was needed — the test's fixture was the only thing wrong.

Time:      Day 6, wrap-up. Caught by the suite's own second full run before
           commit, not by inspection — the mechanism working as intended.
```

### Not a failure, but worth recording

**Day 2's envelope had already anticipated Day 6's mandate/subscription
adapters, four days before either existed.** `rules.MANDATE_SOURCE_TYPES` was
written as `{"mandate", "subscription"}` on Day 2, when only the payment
adapter existed and neither `mandate` nor `subscription` events could be
constructed at all. Writing `tests/test_sense_adapters.py`'s subscription
test turned up the fact directly: an un-notified subscription retry is
refused citing R1 with zero code changes required today. Not a defect in
either direction — R1 genuinely does need to reach subscription-style
recurring debits, and whoever wrote it on Day 2 evidently reasoned from the
regulation rather than from what the codebase could exercise yet. Recorded
because it is a real, checkable instance of building the general rule before
the specific case that needs it, which the project has otherwise mostly
gotten by luck rather than by that kind of foresight.

**The canary confirmed on the first real run, and that was worth pausing on
before writing it down as a result.** A protocol built to catch a wrong
diagnosis that confirms the one real diagnosis available to test it against
is consistent with two different explanations: the protocol works, or the
protocol is too permissive to ever refute anything. The second was checked,
not assumed — `tests/test_canary.py` proves the REFUTED path fires on a
`decompose` result built to disagree with ground truth, using the identical
comparison the real run went through. So the real run's CONFIRMED verdict is
evidence the diagnosis was right, not evidence the check cannot fail.

---

## Day 7 — 2026-08-30

### [2026-08-30] [severity 2] [layer: reported figures / cache reproduction]

```
Symptom:   Block D re-ran the full batch offline to freeze the numbers and got a
           DIFFERENT headline from the one Day 6's STATE.md published. The true
           C-B effect read +14.07pp, not +13.08pp; incremental recovery read
           Rs 7,44,967.63, not Rs 4,44,951.35. Same code, same seed, no live
           call -- and a number that a reviewer would have caught as a
           discrepancy against the committed STATE.md.

Diagnosis: Not a bug, and the mechanism is exactly the one Block D exists to
           surface. Day 6's +13.08pp was computed from the Day 5-evening LIVE
           run, which resolved 24 of 273 planner signatures from a real LLM reply
           before hitting the rate wall; the rest fell back to the NFR-2 default.
           Between then and now, more fast-tier planner replies were cached (as
           the Day 5-evening runs continued) and committed on Day 6. The offline
           replay probes every cached candidate, including via the failover-peek
           path, so it now resolves 36 of 273 signatures -- twelve more real LLM
           plans than the live run captured. Twelve more genuinely-LLM-authored
           signatures is a larger, truer C-B. The Day 6 STATE.md headline was
           stale relative to the cache that was committed alongside it.

Mechanism: Took the offline, keyless figure as the frozen one, because it is the
           reproducible one: two runs with .env moved aside and all five keys
           unset (has_any_llm_key: False) produce byte-identical output, and that
           is the number a reviewer can reproduce with `git clone && make`. STATE.md
           now carries all three readings (pre-key 0.00 exact, Day 6 live +13.08,
           Day 7 frozen +14.07) with the signature-count mechanism that explains
           the movement, rather than quietly replacing one number with another.
           Severity 2 -- the figure was never wrong, but a published number moved
           between days and the honest fix is to show why, not to hide the seam.
```

### [2026-08-30] [severity 1] [layer: converse / voice, python version]

```
Symptom:   `python -m pramaan.cli voice` crashed writing the transcript:
           `TypeError: write_text() got an unexpected keyword argument 'newline'`.

Diagnosis: `Path.write_text(newline=...)` was added in Python 3.10; this
           environment is 3.9. The LF-forcing that the ledger's own
           `export_jsonl` does with `open(..., newline="\n")` cannot be spelled
           on `Path.write_text` here.

Mechanism: Used the same `open(path, "w", encoding="utf-8", newline="\n")` form
           the ledger already uses, so the committed transcript is LF on every
           platform (the .gitattributes byte-exactness rule). Friction only, but
           it is the kind of version-specific API assumption that a golden-file
           project cannot leave in.
```

### [2026-08-30] [severity 1] [layer: converse / S7 classifier coverage]

```
Symptom:   The dispute stand-down test failed: "Maine to already pay kar diya
           tha!" was not detected as a dispute signal, so S7 did not fire.

Diagnosis: The dispute regex required "maine" immediately adjacent to "pay",
           but the real Hinglish phrasing puts "to already" between them
           ("maine to already pay kar diya"). A classifier that only matches the
           textbook word order misses exactly the natural utterances it exists
           for -- and an S7 false negative is the harmful direction (continuing
           to chase somebody who says they already paid).

Mechanism: Broadened the dispute patterns to match the commitment core
           ("(pay|payment) kar (diya|di)") and "already (paid|pay)" regardless of
           intervening words. Caught by the test before it shipped; the reason it
           is recorded at all is that S7's errors are asymmetric, so its coverage
           gaps are worth a line even when a test caught them.
```

### [2026-08-30] [severity 1] [layer: cli / voice, use-after-close]

```
Symptom:   `python -m pramaan.cli voice` printed the whole report and then exited
           1 with no RESULT checks shown -- an exception after "RESULT" printed.

Diagnosis: The RESULT checks read `ledger.kind_counts()`, but the SQLite
           connection had already been closed in the LEDGER section (right after
           export_jsonl). A closed-database read raised, after the header line
           had printed, so the failure looked like a silent non-zero exit rather
           than an obvious crash.

Mechanism: Snapshot `kind_counts = dict(ledger.kind_counts())` before
           `conn.close()` and had the RESULT block read the snapshot. Friction
           only, and caught the first time the command was run end to end -- but
           an exit-1 that still prints a clean-looking report is exactly the kind
           of thing a reviewer running `make voice` would trip over, so it is
           worth the connection-lifetime discipline the rest of the CLI already
           keeps.
```

### [2026-08-30] [severity 2] [layer: converse / Sarvam TTS, stale API IDs]

```
Symptom:   With the Sarvam key finally supplied, the first live `make voice-live`
           crashed: `400 Client Error: Bad Request` from the text-to-speech
           endpoint. No audio was produced.

Diagnosis: The TTS request carried two IDs that were correct when the client was
           first written from memory and wrong by the time it ran: speaker
           `meera` and model `bulbul:v2` had both been deprecated. Sarvam's 400
           body says so explicitly and lists the current roster -- exactly the
           same class of failure as the Groq model IDs that had been dead for
           twelve days on Day 4. A free-tier voice lineup rotates like a
           free-tier LLM lineup, and IDs written from memory are IDs with an
           expiry date.

Mechanism: Read the error body rather than guessing: it named `bulbul:v3` as the
           replacement model and, on the next 400, the v3-compatible speakers.
           Corrected to model `bulbul:v3`, agent voice `ritu`, and while there
           switched the codec to mp3 (`output_audio_codec`) so the per-line clips
           concatenate into one playable file instead of a WAV with a header
           mid-stream. All three constants now carry a comment saying they were
           verified live on 2026-08-30 and how to re-derive them from the API's
           own error body. Severity 2, not 1: this is the second time in the
           project an ID written from memory turned out to be dead, and the
           lesson (verify against the provider, do not trust memory) is one the
           `models` command already exists to enforce for the LLMs -- voice now
           needs the same reflex.
```

### [2026-08-30] [severity 1] [layer: converse / artifact reproducibility]

```
Symptom:   After the live LLM turn policy ran, the committed transcript and the
           mp3 risked drifting: the transcript is meant to reproduce keyless, but
           the live LLM turns (nicer Hinglish) did not replay offline, so `make
           voice` would have overwritten the committed transcript on every run.

Diagnosis: A per-conversation LLM reply is keyed by the whole transcript-so-far,
           and one unparseable turn cascades the rest to the deterministic
           fallback -- so offline replay of the committed cache did not reproduce
           the live transcript. Committing the live version would have made `make
           voice` produce a dirty tree, and left the mp3 (live words) and the
           transcript (which command wrote it?) able to disagree.

Mechanism: Made the committed artifact use the DETERMINISTIC turn policy for both
           the transcript and the audio -- the same reason `make demo` is
           deterministic. `--live-sarvam` now controls the audio only; the words
           are the reproducible ones, and the mp3 speaks exactly them, so the two
           can never drift and `make voice` produces no diff. The live LLM turn
           policy stays a real, tested path (generate_reply with a client,
           verified live this session) -- it is simply not the committed artifact.
```

### Not a failure, but the day's real constraint

**The demo GIF was rendered from real output, not screen-captured -- and that
is the honest version, not a shortcut.** There is no screen recorder in this
session, so the GIF was produced by running the actual `make execute-full`
(offline, keyless), capturing its real stdout, and drawing those exact bytes as
scrolling-terminal frames (Pillow + Consolas). Deterministic input -> a
deterministic GIF, which is *more* reproducible than a screen recording, not
less: nothing is typed by hand, nothing is edited, the frames are the program's
own output. The distinction worth stating is that this is not the same as
faking: the tempting fake would have been mocking up output that looks
impressive; instead the pixels are a faithful render of a real run's bytes,
landing on the real headline (Rs 7,44,967.63 incremental, C-A/C-B both excluding
zero) and `ALL CHECKS PASS`. The generator uses Pillow, a build-time tool kept
out of the public repo so the "SQLite + stdlib only" runtime claim stays true.

**The voice audio clip was the one genuine provisioning gap -- and it closed
mid-session when the human supplied the Sarvam key.** While it was unset, faking
it (a robotic offline-TTS clip dressed up as the memorable artifact) was rejected
on the same principle the canary and the incident were held to: this project does
not stage its evidence. So the transcript shipped regardless, and the audio was
one command away the moment a key existed -- which is exactly what happened. The
key arrived, `make voice-live` produced a real two-voice Sarvam clip (after the
stale-ID fix above), and the DoD item closed for real rather than on a promise.
An absent-but-reproducible artifact was the right way round to be incomplete; a
convincing fake would not have been.

**The realtime-duplex fallback was taken at the start of the day, not at the
deadline.** BUILD-PLAN Day 7 sets a 14:00 hard stop for the duplex-vs-single-turn
decision precisely so the clip does not die at midnight. With no way to run or
verify a live Sarvam WebSocket in this environment, the single-turn stack was
chosen up front: it is the same real STT/LLM/TTS pipeline, it clears every
compliance gate identically, and it is testable and reproducible in a way a live
socket is not. Recorded because taking the fallback early and without regret is
the instruction, and doing so is a decision worth showing rather than a
capitulation worth hiding.


---

## Day 8 — 2026-08-30 (ship)

### [2026-08-30] [severity 3] [layer: cli / investigate ledger write]

```
Symptom:   Building the all-kinds coverage golden (Day 8 DoD: every LEDGER_KINDS
           value must appear in a golden file) crashed writing a RECEIPT_AUDIT
           row: `ValueError: unknown decision 'UNSUPPORTED'`.

Diagnosis: `cli.run_investigate` wrote the RECEIPT_AUDIT row with
           `decision=session.audit.status`. The audit status is SUPPORTED /
           UNSUPPORTED, but the ledger validates the `decision` column as one of
           ALLOW / AMEND / REJECT (it is the GATE verdict column). So a real
           audit could never be written -- the receipt auditor's verdict is not a
           gate decision. It stayed hidden for four days because the live
           investigate run never completed a write (it hit the rate-limit wall,
           Days 5-7), so this line was never reached end to end. The status was
           also already in the payload via `as_ledger_payload()`, so passing it
           to `decision` was redundant as well as wrong.

Mechanism: Removed the `decision=` argument from the RECEIPT_AUDIT write; the
           status lives in the payload where it belongs. The new
           `tests/test_ledger_kind_coverage.py` now drives EVERY real writer into
           one chain and asserts the emitted kind-set equals the enum, so a write
           path that crashes -- or a kind with no working writer -- is a test
           failure rather than a latent surprise. This is the mechanism the
           enum-growth test (one kind at a time) could not provide: it proved
           each kind COULD be appended, not that the production writer actually
           succeeds. A design change, not a typo: the fix says the auditor's
           verdict and a gate decision are different columns, and the coverage
           test makes "every advertised kind is really emitted" enforceable.
```

### [2026-09-02] [severity 4] [layer: claims / the voice STT leg]

```
Symptom:   The README, STATE and the transcript all said the voice work is a
           "Sarvam STT -> LLM turn policy -> Sarvam TTS" stack and that a promise
           was "extracted from SPEECH". An audit showed the shipped clip
           (assets/voice-demo.mp3) is TTS reading a scripted transcript:
           SarvamClient.transcribe() is never called in the demo/voice pipeline
           (grep: only in its own def and one fake-transport unit test). No
           customer speech is transcribed, and nothing transcribed drives a turn
           -- exactly the "TTS reading a script is not a voice agent" bar the
           Day-7 prompt warned about.

Diagnosis: Two real, separable facts got merged into one overstated claim. (1)
           The STT *code* exists and, checked live on 2026-09-02, actually works:
           Sarvam saarika:v2.5 transcribes the synthesised Hinglish audio
           correctly. (2) It is not *wired* into the pipeline, for a concrete
           reason found while auditing: STT returns Devanagari, where the
           deterministic promise/S7 extractors are romanized-Hinglish regex, so
           driving the loop from the transcript needs the LLM extraction path --
           which detects the promise from Devanagari but miscomputes the weekday
           ("Friday" -> the wrong day). And STT needs a key + network, so an
           STT-driven transcript could not be the keyless-reproducible committed
           artifact. So the pipeline was built to run on the typed
           DEMO_CUSTOMER_UTTERANCES, and the prose never caught up to that.

Mechanism: Corrected the claim everywhere rather than the code: "from speech" ->
           "from the customer's utterance"; "Sarvam STT -> ... -> TTS" clip ->
           "Sarvam TTS of a scripted call, STT implemented and verified but not
           run in it"; the transcript header and the coverage row reworded; a
           dedicated section added to LIMITATIONS.md stating the STT leg is
           verified-but-unwired and why, and what a genuine STT-driven clip would
           take. This is a claim fix, not a fabrication (the audio is real, the
           script honest), but the "genuine STT leg" bar is not met and the repo
           now says so. Severity 4: a panel probing the memory-hook artifact
           would have caught the gap, and one overstated differentiator
           discredits the honest ones -- which is the whole thesis of the
           project. The lesson is the same discipline applied to a claim instead
           of a number: state exactly what runs, not what the stack could do.
```
