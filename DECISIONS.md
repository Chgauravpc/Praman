# Decisions

Architecture decision records. Each one states what was decided, what it costs,
and what would have to be true for it to be wrong.

Two of these — ADR-002 and ADR-003 — are load-bearing enough that changing them
invalidates the entire committed LLM cache. They are recorded here on Day 1 and
not revisited.

---

## ADR-001 — SQLite, and no Docker for the database

**Decided.** One SQLite file. No Postgres, no Docker Compose for the datastore,
no graph database, no vector store.

**Why.** Nothing in this system needs more. The largest batch is 6,000 events;
the heaviest query is a group-by over a few thousand rows. Postgres would add a
service to start, a connection pool to tune, and a reason for `make demo` to fail
on a reviewer's machine. The hash-chained ledger needs ordered append and exact
byte reproduction, and SQLite gives both.

**Cost.** No concurrent writers. Irrelevant: the pipeline is a single process.

**Wrong if.** The event volume grew past a few million rows, or multiple writers
needed the ledger at once.

---

## ADR-002 — Five amount bands, upper-inclusive, boundaries set by RBI **[frozen]**

**Decided.** Exactly five bands, and these five:

| Band | Range | Paise |
|---|---|---|
| 1 | ₹0 – ₹500 | `0 .. 50,000` |
| 2 | ₹500.01 – ₹5,000 | `50,001 .. 500,000` |
| 3 | ₹5,000.01 – ₹15,000 | `500,001 .. 1,500,000` |
| 4 | ₹15,000.01 – ₹1,00,000 | `1,500,001 .. 10,000,000` |
| 5 | above ₹1,00,000 | `10,000,001 ..` |

**Why these boundaries.** ₹15,000 and ₹1,00,000 are RBI's AFA exemption
thresholds under the e-mandate framework (envelope rule R2). Subsequent mandate
debits are AFA-exempt up to ₹15,000, and up to ₹1,00,000 for insurance premiums,
mutual-fund subscriptions and credit-card bills.

So the band is not only a cardinality-reduction device. It carries
compliance-relevant information into the planner signature for free, which means
a plan for band 4 can legitimately differ from a plan for band 3 for a *legally
correct* reason rather than a statistical one.

**Why upper-inclusive.** Band *N* covers `(ceiling[N-1], ceiling[N]]`. RBI's
exemption is "up to ₹15,000" — inclusive. A debit of exactly ₹15,000.00 is
exempt; ₹15,000.01 is not. Lower-inclusive bands would put the exempt boundary
case in the non-exempt band, and the envelope would demand AFA for a payment
that does not need it. One paisa, two different legal treatments, and the band
edge has to fall between them.

**Why five.** Too many bands explodes the signature space. Too few and the
planner cannot distinguish an AFA-exempt debit from one that needs
authentication.

**Cost.** The planner never sees an exact amount. Deliberate — see ADR-004.

**Wrong if.** RBI moved a threshold. That would be a genuine reason to change
the bands, and it would invalidate every cached LLM response, which is the
correct consequence rather than an inconvenience.

---

## ADR-003 — The planner signature is exactly seven fields **[frozen]**

**Decided.**

```python
PLANNER_SIGNATURE_FIELDS = [
    "reason_class",         # 10 values
    "diagnosis_class",      # 11 values (ten classes plus "undiagnosed")
    "amount_band",          #  5 values  (ADR-002)
    "segment",              #  3 values  metro | tier2 | tier3
    "legal_context",        #  3 values  service | collection | promotional
    "channel_eligibility",  #  3 values  which channels are open right now
    "hour_bucket",          #  3 values  business | evening_peak | night
]
```

**Why it is capped.** The LLM plans once per *situation*, not once per event.
Adding a field can multiply the signature space, and the memoisation ratio is
the difference between a token budget of ~800K and one of ~15M.

**Nominal versus observed.** Nominal space is 44,550 combinations. Measured on
the seeded 6,000-event batch: **273 distinct signatures, a memoisation ratio of
22.0×.** The gap is not luck; there are two structural reasons, and both survive
contact with real traffic:

- Most combinations are **unreachable.** `MERCHANT_CONFIG` never reaches the
  planner at all — the envelope handles it. `INSTRUMENT_DEAD` never co-occurs
  with an `issuer_degraded` diagnosis. Contact-forbidden reason classes force
  `channel_eligibility` to a single value.
- Real traffic is **concentrated.** Three reason classes are 70–100% of volume,
  so the top 25 signatures cover 62% of events and the top 100 cover 92%.

**Two honest corrections to the planning estimate.** The build plan predicted
~100–200 observed signatures and ~30×. Measured, it is 273 and 22.0×. Reported
as measured.

**And 22.0× is an upper bound, because two of the seven fields are pinned — not
one.** `diagnosis_class` is constant at `undiagnosed` (no investigator until
Day 4) and `legal_context` is constant at `service` (payment failures only until
the Day 6 receivables adapter). So the effective signature space today is
`10 × 1 × 5 × 3 × 1 × 3 × 3 = 1,350`, not 44,550 — and 273 of those 1,350 are
already occupied, i.e. **20% of the reachable space**. Saturation is closer than
the ratio suggests.

Live today: `reason_class` (10/10), `amount_band` (5/5), `segment` (3/3),
`channel_eligibility` (3/3), `hour_bucket` (3/3). Pinned: `diagnosis_class`
(1/11), `legal_context` (1/3).

**The budget headroom needs recomputing before Day 4.** The number to watch is
planner calls, not the ratio. 273 calls at roughly 800 tokens is about 220K,
comfortably inside budget — but "even if the count doubles" prices in *one*
unpinning. Both fields going live is multiplicative, not additive, and the
plausible range is closer to an order of magnitude than to 2×. If the count
reaches ~2,700 the planner alone is ~2.2M tokens, which does **not** fit. Two
things bound it in practice: most reason classes admit only one plausible
diagnosis class, and `legal_context` is determined by the adapter rather than
drawn freely. Neither is a substitute for measuring it on Day 4, on the dev
batch, before the full run is committed to.

**Cost.** Two events in the same situation get the same plan, even where a human
might distinguish them. That is the trade being made deliberately: an ops lead
does not re-derive policy for every ticket either.

**Wrong if.** The observed signature count approached the nominal space, which
would mean a field had gone high-cardinality. `make demo` prints the count on
every run precisely so that would be visible.

---

## ADR-004 — Prompts are built from canonical banded features only

**Decided.** No prompt may contain an identifier, an exact amount, or a raw
timestamp. Identifiers travel *alongside* the prompt for logging and
reconciliation, never inside it.

**Why.** A high-cardinality value in a prompt makes every call a cache miss and
every signature unique. The hit rate collapses, the memoisation ratio goes 30× →
1×, and the token budget goes 800K → 15M. The system still works — it just
becomes unaffordable and non-reproducible, and it does so silently. This is the
most likely way this build breaks in a way no ordinary test catches.

**How it is enforced**, in three layers rather than by review discipline:

1. **Whitelist construction.** `canonical.validate_features` accepts exactly the
   seven fields, each checked against a closed domain. An eighth key is a hard
   error, so an identifier cannot arrive by being *added*.
2. **A screen on the rendered bytes.** `prompts.assert_no_identifiers` runs on
   every prompt build, in production as well as in tests, and rejects Razorpay-style
   ids, ISO timestamps, currency figures, four-or-more digit runs, hex digests,
   email addresses and Indian mobile numbers. This catches the other route in: a
   value smuggled inside a field, or pasted into a template while editing it.
3. **The invariant test.** `tests/test_prompt_canonical.py` builds prompts for
   two maximally-different events that share a signature and asserts the prompt
   bytes are identical — including one carrying simulator ground truth.

**Cost.** The planner cannot reason about an exact figure. It does not need to:
it needs to know an amount is in band 2, not that it is ₹2,437.

---

## ADR-005 — Three arms, named A/B/C, not treatment/control

**Decided.** `arm ∈ {A, B, C}`. A is control (detect, diagnose, log, do not
act), B is rules-only, C is LLM-planned.

**Why.** Two independent contrasts fall out, and the second is the interesting
one. `B − A` says whether recovery works at all. `C − B` says whether the LLM
earned its place over a lookup table — the one rubric question that almost every
AI submission asserts and none measures.

**Why the naming note.** The PRD's `RiskEvent` sketch writes `arm` as
`treatment | control`. That two-value sketch is superseded: it cannot express
`C − B`. The three-arm design is the frozen one.

**Cost.** A third of events get no intervention, so a third of recoverable
revenue is deliberately left on the table. That is the price of knowing the
number is real.

**Publishable either way.** If `C − B ≈ 0`, the honest finding is that on this
workload a lookup table matches the LLM for intervention *selection*, and the
LLM's value showed up in diagnosis and conversation instead. That is a genuinely
interesting result and stronger than an unfalsifiable claim.

---

## ADR-006 — The LLM cache is committed to the repo

**Decided.** `fixtures/llm_cache/` is version-controlled. `make demo` runs with
every API key unset and makes no network call.

**Why.** A reviewer with a thousand submissions will not provision API keys for
one of them. Being the project that runs on `git clone && make demo` — and still
produces a real, verifiable number — removes the last excuse to skip the repo.
It also makes re-runs free, so iterating on the pipeline does not burn a
free-tier daily allowance.

**The cache key** is `sha256(model + params + full prompt)`, and it doubles as
the `llm_call_id` recorded in the ledger. One identifier does both jobs on
purpose: a reviewer holding a ledger row can look up the exact prompt and the
exact response in the committed repo, which is what makes a claim *checkable*
rather than merely logged.

**Nothing time-varying is stored in a cache file.** No `created_at`, no latency.
The files are committed, so a timestamp would produce a diff on every
regeneration and make the cache unreviewable.

**`temperature=0` is used but is explicitly not relied on for reproducibility.**
The cache is the mechanism. Assuming temperature-zero implies determinism is a
common and wrong belief, and this project does not depend on it.

**Cost.** Repo size, and the discipline of reading a cache diff after a prompt
change. That diff is a feature: it is the record of what the change cost.

---

## ADR-007 — Simulated failure reasons are volume-weighted, never uniform

**Decided.** Reason codes are drawn from the five buckets published in the PRD
(bank timeout 37%, wrong PIN 24%, insufficient balance 18%, network 11%, account
blocked 6%), plus a deliberate 4% long tail carrying `LIMIT`,
`MERCHANT_CONFIG`, `INTEGRATION_BUG`, `RISK`, `ALREADY_PAID` and `ELIGIBILITY`.

**Why not uniform.** A uniform draw over the 69 codes would put 1.45% of events
on `bank_technical_error` when reality is 35–45%. The envelope's never-retry
branches would dominate a workload that does not exist, the evaluation would
measure fiction, and the memoisation ratio would be wrong because the signature
distribution would be wrong. Measured on the seeded batch, `bank_technical_error`
is 21.2% of events — 128 standard errors above the uniform null.

**Why the 4% tail exists.** By *volume* most failures are recoverable; by *code
count* 45 of 69 are not. That divergence is the trap the envelope exists to
catch, so a batch containing none of the dangerous classes would test nothing.
4% of 6,000 events is ~240 cases: enough to measure, small enough that all five
published buckets stay inside their stated ranges.

**No declared weight sits on a range boundary.** An earlier draft put `network`
at exactly 0.10, its published floor, and the batch duly came out at 0.0923 —
outside the range the README quotes, for no reason other than that a boundary
value falls below its floor half the time. Every weight now has headroom.

**Cost.** **Eleven of the 69 codes are structurally unreachable** by the
generator — they are in no bucket's code list and in no long-tail class — so they
never appear at any seed or batch size. Seven of those eleven are `AUTH_DROPOFF`
(`incorrect_cvv`, `incorrect_atm_pin`, `card_number_invalid`, `card_type_invalid`,
`incorrect_card_details`, `incorrect_card_expiry_date`,
`incorrect_cardholder_name`) — a *head* class at 24% of volume, not a tail class.
The remaining four are `INSTRUMENT_DEAD`. On top of that, sampling leaves more
codes unseen in any given batch: 15 unseen at seed 42, 11–15 across seeds.

Quote **eleven**: it is the seed-independent figure. Either way the conclusion is
the same and is the point — Day 2's red-team tests must construct reason codes
directly rather than rely on the simulator covering the taxonomy.

The dev batch is much thinner still: at seed 42 it contains **zero**
`ELIGIBILITY` events, and `LIMIT`, `INTEGRATION_BUG` and `MERCHANT_CONFIG` are
2, 2 and 4 events respectively. A whole reason class being absent from the batch
that Days 2–5 are debugged against is a second, independent reason the red-team
suite cannot sample.

---

## ADR-008 — `make demo` is the dev batch, because `make demo --dev` cannot work

**Decided.** `make demo` runs the 200-event dev batch. `make demo-full` runs the
6,000-event batch.

**Why.** The build plan writes the target as `make demo --dev`, but GNU make
parses a leading `--` as one of its own options and errors on an unrecognised
one. No Makefile can accept it. The dev batch is the reviewer path anyway, so it
became the default. The CLI accepts the flag literally, so
`python -m pramaan.cli demo --dev` works exactly as written.

**A Windows shim ships alongside.** `make.ps1` carries the same targets. GNU make
is not installed on the machine this was built on, so the Makefile itself was
never executed here — shipping a Makefile nobody had run seemed worse than
shipping both and saying which one was exercised.

---

## ADR-009 — Event time only. There is no `now()` in the write path

**Decided.** Every ledger row's `ts` is the event's own `detected_at`. The
simulator positions its stream relative to a constant epoch. No component reads
the wall clock.

**Why.** A `datetime.now()` at write time would put the run's date into the row
hash, and NFR-3 — same seed plus same cache produces a byte-identical ledger —
becomes unsatisfiable. `Ledger.append` therefore has no default for `ts` and no
fallback: a caller that does not know its event's time has a bug, and silently
substituting the wall clock would hide it while breaking reproducibility.

**Cost.** Every call site must thread an event time through. That is the
intended pressure.

---

## ADR-010 — Latent ground truth lives in its own table

**Decided.** The simulator's `self_recovers_at` is stored in a separate `latent`
table, not as a column on `events`, and no view joins them.
`EventStore.iter_events()` omits it unless explicitly asked.

**Why.** In production the counterfactual is unobservable; in simulation it is
known. That asymmetry is the whole measurement design — the holdout estimates
the effect the way production would, and ground truth then verifies the
estimator is unbiased. Which makes it the most dangerous object in the codebase:
if it reached a diagnosis, a plan or a prompt, the agent would be reading the
answer key and every number downstream would be fiction.

The investigator gets a read-only SQL tool belt and writes its own queries, so
the schema itself has to make reading the answer key hard. A separate table does
that: `SELECT * FROM events` — the natural thing for an agent to write — cannot
return it. `tests/test_prompt_canonical.py` additionally asserts that an event
carrying ground truth produces byte-identical prompt bytes to one without it.

---

## ADR-011 — The ledger enum contains only kinds the system actually emits

**Decided.** `LEDGER_KINDS` is `("DETECT",)` today. Kinds are added on the day
their writer lands, not in advance.

**Why.** A ledger whose schema advertises events the system never writes reads as
aspirational. A shorter enum where every value appears in the golden file is more
convincing than a longer one. `Ledger.append` rejects an unknown kind, so this is
enforced rather than merely intended.

**Consequence.** Run provenance — seed, batch size, epoch — goes in a `runs`
table instead of a synthetic ledger header row, which keeps the ledger a pure
stream of domain events.

---

## ADR-012 — The rule-ID namespace has four prefixes, not one

**Decided.** Every constraint the envelope applies carries an id whose prefix
declares its authority: `R` regulation, `G` Razorpay decline-reason guardrail,
`S` stopping rule, `P` house policy. `NO_RULE_BINDS` is `P0`.

**Why.** One numbering scheme would be tidier and would misrepresent the
system. Retrying `card_expired` is futile, not illegal. Refusing a promotional
email at 03:00 is good manners, not a TRAI offence. A voice-call floor of ₹500 is
a number we chose. Filing all of those under `R` would make the compliance story
look broader and be worth less: the first time a reviewer checked one citation
and found a preference, they would stop trusting the other ten.

**Consequence.** A refusal is answerable to "which rule, and where does it say
that" — and for the `P` rules the honest answer is "nowhere, we decided it",
which is now visible rather than disguised. The demo prints the legend.

**Wrong if.** A regulator's requirement were mis-filed as a `P`, which would
under-state a legal obligation. That is why `rules.RULE_SOURCES` requires an
instrument for every `R` and `_self_check` fails at import without one.

---

## ADR-013 — The gate reads timestamps at second precision, never `hour_bucket`

**Decided.** Every window rule in `pramaan/envelope/` takes an ISO-8601
timestamp and works in seconds since IST midnight. The three-value `hour_bucket`
is a **cache key** and is never a legal input. Windows are **closed intervals**
on both ends.

**Why.** This resolves the Day 1 blocker that looked like a frozen-field problem.
`hour_bucket` provably cannot represent the 08:00–09:00 state — R9 open, R5/R8
shut — so a gate built on it permits a voice call at 08:30 that R8 forbids. The
fix is not a fourth bucket (which would churn every cached signature and would
still be coarse). The fix is that **the gate does not use buckets**. F7 is
untouched, the cache key is unchanged, and the disagreement between the two is
pinned by a test.

On closed intervals: RBI prohibits contact "before 8:00 a.m. and after 7:00 p.m.",
so 08:00:00 and 19:00:00 are both permitted and 19:00:01 is not. A half-open
`[08:00, 19:00)` refuses a lawful contact; hour-rounding permits an unlawful one
at 19:59. Both are a boundary asserted rather than read.

**Cost.** The coarse feature is knowingly more permissive than the gate in three
combinations. Safe in exactly one direction — nothing acts on a cache key —
and asserted as such in `test_envelope_matrix.py`.

**Wrong if.** Anything downstream ever gated on `channel_eligibility` instead of
calling `judge`. That would be a compliance breach, not a bug, which is why the
docstring on `canonical.channel_eligibility` says so in the first line.

---

## ADR-014 — The window matrix is two hand-written tables that check each other

**Decided.** `windows.py` holds `LEGAL_WINDOWS` — 18 cells of
`(legal_context × channel) → bounds` — and `HOURLY_GRID`, the same 18 cells drawn
as 24-character strings, one character per IST hour. Both are written by hand
from the regulation, and `_self_check` asserts at import that they agree at every
hour of every cell.

**Why.** The artifact under review is the *correspondence between the table and
the regulation*, and that cannot be eyeballed against control flow. The
redundancy is the feature: a typo in either representation is caught at import by
the other, and a reviewer gets a picture of the day rather than a list of
boundary constants.

**Cost.** Two places to edit when a window changes. Deliberate — the import-time
check means forgetting one is loud.

---

## ADR-015 — `reason_map.py` projects `taxonomy.py`; it does not re-transcribe it

**Decided.** The 69 codes stay in `pramaan/taxonomy.py`, which the simulator also
imports. `pramaan/envelope/reason_map.py` holds the eight guardrails G1–G8 and
asserts at import that its projection still covers all ten classes.

**Why.** The build instruction asked for Appendix A transcribed into
`reason_map.py`, and doing that literally would have produced a second copy of a
69-row table. Two copies is how the eval ends up measuring a workload the
envelope does not police — the exact failure the single-source-of-truth comment
in `taxonomy.py` was written to prevent. The envelope's real job here is the
other half: turning the table into named, citable guardrails.

**Consequence.** `reason_map._self_check` fails at import if a class is added to
the taxonomy with no guardrail, or if a class is claimed by two retry guardrails
(which would make the cited rule depend on evaluation order).

---

## ADR-016 — AMEND is implemented, and re-judged; a deferral is not an amendment

**Decided.** Two amendments exist: an immediate retry on FUNDS/LIMIT becomes a
24-hour retry (G7), and a voice call that the window or the value floor refuses
becomes a message (R8 / P2). Every amendment is **re-judged from the top**, once,
and is only offered if it comes back `ALLOW`.

**Why.** BUILD-PLAN Day 5 permits a binary envelope. Both amendable cases are
*arithmetic* rather than judgment, and both are common planner errors, so
refusing them outright discards a plan that was substantively right. The
re-judgement is what stops AMEND from being a hole: the envelope never proposes
something it would not have allowed.

**And the line that is not crossed.** A collection contact at 19:05 is `REJECT`
citing R9 — not "AMEND: send it at 08:00". The judgement carries
`reopens_in_seconds` so a caller can queue it, and the queued step is judged
again when it fires. Turning "not now" into "later" inside the envelope would
mean the envelope had approved an action at a time it never evaluated.

**Not amendable, on purpose.** A missing AI disclosure (R10) or a missing DLT
template (R5). The envelope would have to author a script or invent a registered
template, and refusing sends the defect back to the planner, which is the only
place it can be fixed.

---

## ADR-017 — DND is enforced by R5 and R8, not by S4

**Decided.** PRD 6.6 lists DND under stopping rule S4. It is implemented in R5
(messages) and R8 (voice) instead. S4 keeps consent withdrawn (DPDPA) and mandate
withdrawn.

**Why.** ADR-012's discipline applied. The NCPR/DND register is TRAI's
instrument, so a refusal that rests on it should name the TRAI rule. With DND in
both places, a DND-suppressed call would cite S4 or R8 depending on evaluation
order, and the compliance report would under-count R8.

**Consequence.** A withdrawn mandate is R4's business for *debits* — AFA
re-validation can make a further debit permissible — and S4's for *contact*,
because nothing makes further contact permissible. Both are tested.

---

## ADR-018 — R3 is operationalised as attributability

**Decided.** R3 states a responsibility rather than a threshold, so its evaluator
asks one question: can this action be attributed to a named merchant? An
unattributable action is refused, including `ACT_WAIT`.

**Why.** R3 is the rule a Razorpay engineer cares about: Razorpay is the
acquirer, so a merchant's badly-behaved recovery agent is *Razorpay's* liability.
An action the acquirer cannot attribute is an action it cannot answer for, and an
audit trail with an unattributed row in it is an audit trail with a hole. Leaving
R3 as commentary would have left the one rule that makes the submission relevant
to the reader with no test behind it.

**Cost.** Every action needs a merchant id. Correct: that is what the rule says.

---

## ADR-019 — The collection window is per-channel, which diverges from PRD §7

**Decided.** For `legal_context = collection`: non-voice channels are bound by R9
at **08:00–19:00**; voice is bound by R9 ∩ R8 at **09:00–19:00**.

**Why.** PRD §7 states the binding collection window as the intersection
09:00–19:00 for everything. That is right for voice and too strict for messaging.
R5's band governs *promotional* traffic; a debt-collection SMS is not promotional,
so R5 does not reach it and R9 alone binds — which opens the 08:00–09:00 hour for
collection messaging. The PRD sentence is the headline case; this is the
per-channel reading.

**Consequence.** One extra hour of lawful collection messaging per day, and the
08:00–09:00 state becomes the sharpest test of the whole design: a collection SMS
is permitted at 08:30 and a collection call is not, citing R8.

**Wrong if.** A collection message were held to be promotional under the DLT
category it is registered in. That is the classification question PRD §7 says to
flag for legal review, and it is flagged in `windows.py`.

---

## ADR-020 — GATE joins the ledger on Day 2, with a writer

**Decided.** `LEDGER_KINDS` becomes `("DETECT", "GATE")`. The demo judges every
event's deterministic default action through the envelope and writes a GATE row
per event.

**Why.** ADR-011's rule is that a kind is added on the day something writes it,
not the day it is planned. GATE now has a writer, and adding it does two things
worth more than the twenty lines it cost: the envelope stops being a component
with tests and becomes a component with a *number*, and `rule_fired` /
`decision` become populated columns — so "how often did R9 refuse an evening
collection attempt?" is a `GROUP BY` rather than a log grep.

**Consequence.** The dev-batch ledger is 400 rows, not 200, and the golden file
was regenerated. `test_the_exported_ledger_matches_the_golden_file` now
reproduces the demo's whole write path rather than only `ingest` — it had to, or
it would have compared against a file it could never generate.

**On the zero-rejection result.** The gate refuses nothing on the deterministic
map, and that is the expected outcome rather than a broken gate: the reason-class
map is *built* compliant, which is what makes it a fair arm B instead of a
strawman. The evidence that the envelope is not inert is
`test_redteam_envelope.py`.

---

## ADR-021 — Any count a reviewer could check is derived, and the prose is tested

**Decided.** Counts that describe the code are computed from the code, never
typed. `rules.GRADE_A_COUNT` / `GRADE_B_COUNT` are derived from `RULE_SOURCES`;
`envelope/registry.py` derives the full rule-id list from the five modules that
own the rules. Prose that states one of those counts — in a docstring, in
`README.md`, in `STATE.md` — is parsed and checked against the derived value by
`test_the_published_grade_counts_match_the_prose`.

**Why.** Because the alternative was tried and failed twice in one day.

Day 2 built a guard so a rule could not claim an [A] grade without quoting the
operative words. The guard works. It protects `RULE_SOURCES`. It has nothing to
say about a sentence sixty lines above `RULE_SOURCES` claiming a different count
— and that sentence claimed **two of eleven [A]-verified** while the dict graded
**one**. The README said nine of eleven were [B]; it was ten. Both shipped to the
public repo.

Then the fix for that included a demo line reading `rule ids implemented 25`,
typed by hand. It is 30.

**The generalisation, which is the actual decision.** A guard on a data
structure does not protect the sentence next to it, and the sentence is what
ships. Care is not a mechanism. So: derive the number, or test the prose against
the number, and prefer both.

**Cost.** A regex-based prose test is slightly brittle — it looks for
"`<n>` of eleven … [A]" and the [B] counterpart. Brittle in the safe direction:
it fails loudly on a rewording rather than silently missing a wrong number, and
a false failure costs a minute while a false pass costs the compliance claim.

**Wrong if.** The counts stopped appearing in prose at all, which would be worse
— a reader meets the sentence, not the dict.

---

## ADR-022 — Grade and authority are separate axes, and R6 keeps its id

**Decided.** `RuleSource` carries `grade` (how well the source was read),
`authority` (`regulator` | `vendor` — who wrote it), and `read_on` (required for
an [A], recording when and from where). R6 declares `authority="vendor"` and is
excluded from the regulator-backed count. It keeps the id `R6`.

**Why.** ADR-012 says never cite an `R` id for something a regulator did not
say. R6 — one attempt plus three retries — is real and load-bearing, and its
source is Razorpay's own subscription documentation. Under the convention that
is a `G`-flavoured source sitting on an `R` id, and it is the one place in the
eleven where the namespace and the source disagree.

Renaming it to `G9` would satisfy the convention and break every cross-reference:
PRD §6.5 numbers it R6, and so does everything downstream. Declaring the
discrepancy costs one field and leaves the id stable, so a reader who checks the
one suspicious rule finds it already labelled. `_self_check` fails if a *second*
vendor-sourced rule appears — two would mean the boundary needs restating rather
than extending.

**And on `read_on`.** An [A] grade now has to say how it was read. R9's says
"2026-08-27, rbi.org.in notification page, single unreviewed reader" — which is
better than a secondary summary and is *not* a second pair of eyes on a gazette
copy. "Verified" with no provenance is an unfalsifiable claim, and this file
should not carry one.

---

## ADR-023 — A test may not compare a function to its own delegate

**Decided.** Where a refactor claims to preserve behaviour, the test pins the
behaviour against values captured **before** the refactor, in a frozen literal
table. `DAY1_CHANNEL_ELIGIBILITY` holds all 90 values of Day 1's
`channel_eligibility`, generated by running that function in a git worktree at
commit `b085fdd`.

**Why.** The test originally named for this claim asserted
`canonical.channel_eligibility(x) == windows.channel_eligibility_for_bucket(x)`.
The first function's whole body is a call to the second, so the assertion cannot
fail; mutating the shared implementation left it green. The property it claimed
was true — verified independently — but it was being held up by one unrelated
Day 1 behaviour test that happened to cover one of the 90 cells. The golden
ledger covered none of the disagreements, because the dev batch is entirely
`legal_context="service"`.

**This is the same shape as the tamper probe (ADR-020's consequence note):** an
assertion that had silently stopped asserting. Both are worse than no assertion,
because they occupy the space where somebody would have written a real one.

**Consequence.** `test_the_delegation_test_can_actually_fail` asserts a specific
cell differs from the plausible wrong answer, so a collapse back into tautology
trips something. And the general rule: if a test would pass after deleting the
code it tests, it is documentation with a `def` in front of it.

---

## ADR-024 — An intervention's effect is derived from a world model, never declared as an uplift

**Date:** 2026-08-27 (Day 3)
**Status:** accepted

### Context

Arm B needs outcomes. The cheap way to get them is one number per action:

```python
P(recover | ACT_RETRY, FUNDS) = organic_rate + 0.12
```

Two hours of work, and it produces a headline number.

### Decision

Do not do that. Model **capability** (when the blocking condition stops blocking)
and **intent** (whether the customer still wants to pay) as separate latents, and
let the uplift fall out of how each intervention interacts with them.

A merchant-initiated retry supplies intent and needs capability, so it converts
*capability without intent*. A message supplies a reminder and needs capability
unless the block **was** the customer not having acted yet, so it converts *intent
that never got round to it*.

### Why

The uplift table is worthless in a specific and fatal way: **the headline
incremental number then *is* the 0.12, restated.** A reviewer asking "where does
0.12 come from" has no answer.

Worse, it silently destroys the one defence the project has against "your
simulator is made up". BUILD-PLAN Day 3 requires an organic-recovery sensitivity
sweep in the standard output. Under an uplift table that sweep moves the baseline
and leaves the uplift untouched, so **every row prints the same incremental
figure** and the table proves nothing at all. Under a capability/intent model,
raising organic recovery genuinely eats the headroom an intervention has to work
in, and the measured estimand falls +3.25pp → +2.33pp across 15%→70% — a 28%
decline, non-proportional, which is a real answer to a real objection.

The parameters are still judgement (**[C]**). But they are claims **about the
world** rather than about the size of the effect, which is a different kind of
assumption and a defensible one. And the effect being derived means it can be
wrong in ways the author did not choose.

### Consequences

- `LatentTruth` gains five fields; the quarantined table gains five columns.
- All of them are drawn at **generation**, so resolution is a pure function and
  both potential outcomes are exactly computable — which is what makes
  `tests/test_estimator_unbiased.py` a proof rather than a smell test (ADR-025).
- Draws come from a per-event RNG keyed on `sha256(seed | event_id)`, never from
  the generator's shared stream. Taking one extra draw there would shift every
  subsequent event's reason code and amount, moving the Day 1 golden ledger, the
  22.0× memoisation ratio and the 4,905/1,095 envelope tally — for nothing.
  Verified: all three unchanged.
- One independent check: the world model and the Day 1 taxonomy were written
  separately and agree. `AUTH_DROPOFF.retry_needs_customer` is True because a
  merchant charge cannot supply a PIN, and the taxonomy independently answers
  AUTH_DROPOFF with `ACT_WAIT`. Weak evidence, better than none.

---

## ADR-025 — Every random draw happens at generation, so resolution is pure

**Date:** 2026-08-27 (Day 3)
**Status:** accepted

### Context

Resolution asks "did a rail switch clear this block?" and "did the customer answer
the message?". The obvious implementation draws for those at resolution time.

### Decision

No RNG anywhere in `sim/outcomes.py`. Every draw an intervention depends on —
`route_would_succeed`, `message_response_lag_seconds`,
`voice_response_lag_seconds` — is made at generation and stored on the event.
Resolution is a pure function of `(latents, action, delay, window)`.

### Why

PRD §8.2's whole design is that the simulator's known counterfactual verifies the
estimator is unbiased. That requires **both** potential outcomes for every event —
what it does under arm A *and* under arm B — which requires resolving the same
event twice and getting a well-defined answer each time.

An oracle that rolled dice at resolution time would give the *truth* side its own
Monte-Carlo error. The comparison would become "a noisy estimate against a noisy
truth", and the unbiasedness test would degrade from a proof into a smell test.

It also makes the oracle trivially testable and the whole resolver deterministic,
which invariant I8 requires anyway.

### Consequences

- `resolve_one` takes `arm` as a parameter rather than reading `event.arm`, so the
  same function produces both potential outcomes. The live path always passes
  `event.arm`; only `potential_outcomes` passes anything else.
- `potential_outcomes` computes the answer key. Nothing on the agent path may call
  it, nothing writes its output to the ledger, and its only caller is the test.
- The oracle is injectable, which is the seam that makes this productionisable:
  in production the outcome arrives from a `payment.captured` webhook.
  `tests/test_resolve.py` swaps it to demonstrate the boundary is real.

---

## ADR-026 — The observation window is a measurement; T_settle is a policy. They are named apart

**Date:** 2026-08-27 (Day 3)
**Status:** accepted

### Context

PRD §5.1 and §8.1 both say "settle window", and they mean different things. §5.1
means how long the policy waits before acting, so it does not pay to message
someone mid-retry. §8.1 means how long after detection a recovery still counts.

### Decision

Two names, two constants, two modules' worth of separation:

- `OBSERVATION_WINDOW_SECONDS = 72h` — a **measurement** choice, a parameter of
  `resolve_batch`, identical across every arm.
- `SETTLE_DELAY_SECONDS[reason_class]` — a **policy** choice, part of what an arm
  does, and one of the things arm C could plausibly beat arm B on.

### Why

Conflating them is how a measurement window quietly becomes a treatment. If the
window were a property of an arm, arm A could be censored differently from arm B
and B−A would pick up a difference that has nothing to do with recovery. Keeping
the window out of the arm makes that mistake structurally unavailable.

72h is not free either: it must exceed the 24h scheduled-retry delay, or the
measurement censors the treatment before it fires and reports "scheduled retries
do not work" when what happened is that nobody waited for one. Asserted at import,
because the two constants live in different modules and could drift.

And because the trade-off is genuine in both directions — short windows censor
the treatment, long ones let organic recovery swallow the effect — PRD §5.1 asks
for a curve rather than a defended constant. `make demo` prints one.

---

## ADR-027 — An unwired arm reports no number, and the refusal is enforced

**Date:** 2026-08-27 (Day 3)
**Status:** accepted

### Context

Arm C exists from Day 3 and does nothing until Day 5. It holds 1,996 of 6,000
events. Those events have outcomes.

### Decision

`ArmPolicy.wired` is False for arm C, and `metrics.contrast` **raises** if asked
for any contrast involving it. The demo prints arm C's `n` and a dash for every
figure.

### Why

An unwired arm takes no action, so its outcomes are identical to arm A's by
construction. A table printing `C: 29.7%` next to `B: 30.2%` would read as *the
LLM is no better than the table* — which is not a finding, it is an artefact of
the LLM not existing yet. And it is the exact finding Day 5 exists to establish or
refute, so publishing it early with the wrong cause attached would be worse than
publishing nothing.

Enforced rather than documented, because a comment saying "do not print arm C" is
one refactor away from being ignored.

### Consequences

- Building the slot on Day 3 costs ten minutes; retrofitting a third arm on Day 5
  would mean re-running and re-reporting everything.
- More importantly it fixes arm C's measurement **before** arm B's result is
  known, which is what stops the Day 5 comparison being designed around a number
  already in hand.
- `arms._self_check` asserts exactly one arm acts today and names it. If arm C is
  wired without that check moving in the same commit, the import fails — an arm
  that starts acting is a decision somebody should have made explicitly.

---

## ADR-028 — Interval widths are normalised by the control-arm level, not by the point estimate

**Date:** 2026-08-27 (Day 3)
**Status:** accepted, after the first version was wrong

### Context

The Day 3 definition of done requires the money confidence interval to be visibly
wider than the rate interval. Heavy tails guarantee it, so if it is not, the
bootstrap is wrong. The two statistics are in different units, so something has to
normalise them.

### Decision

Divide each interval's width by the **control arm's own level** for that metric —
arm A's recovery rate, arm A's rupees-per-event.

### Why

The first version divided each width by its own point estimate, and reported "the
money interval is narrower" on the full batch. Not because the bootstrap was
broken, but because the incremental rate is +0.58pp: a near-zero denominator makes
any relative width explode.

The error is conceptual. The quantity being compared is how precisely each
statistic can be **estimated**. Dividing by a noisy near-zero estimate measures
the estimate rather than the precision — and it fails exactly when the effect is
small, which is the case the project actually reports. Arm A's levels are stably
estimated and each sets the natural scale for its own metric.

### Consequences

- Full batch: rate 0.192, money 1.113. The money interval is 5.8× relatively
  wider, which is the expected direction and magnitude.
- A second, independent heavy-tail signature is printed alongside: interval
  **asymmetry**, upper half-width over lower. A normal approximation is symmetric
  by construction, so an asymmetry away from 1.0 is direct visible evidence the
  interval was read off the resample distribution rather than computed from a
  standard error. 1.23 on the full batch.
- Recorded in FAILURES.md as severity 4: the check prints its verdict on screen,
  so it would have shipped a run announcing its own bootstrap was broken.

---

## ADR-029 — A sensitivity table prints the estimand and the estimate, not one of them

**Date:** 2026-08-27 (Day 3)
**Status:** accepted, after the first version was uninformative

### Context

BUILD-PLAN Day 3 requires the organic-recovery sweep in the **standard output**,
specifically so that "your simulator is made up" becomes a question already
answered on screen.

### Decision

Print two columns per scenario: the **exact effect** from both potential outcomes,
and the **arm-based estimate with its confidence interval**.

### Why

The first version printed only the estimate, and the four rows came out
non-monotone: +1.96 / +0.53 / +2.15 / +0.92. It looked like a bug and proved
nothing. Each row carries its own sampling noise, and at 2,000 events per arm each
interval spans about 5pp — wider than the entire range the underlying quantity
moves across. The table was showing noise where it was meant to show a trend.

The estimand was clean all along: +3.25 → +3.08 → +2.85 → +2.33pp. That is the
answer to the question the sweep asks — *how much does this assumption matter* —
and it has no sampling error because both potential outcomes are known.

But printing only the estimand would overstate what the experiment can resolve. So
both, labelled: the estimand answers "how much does the assumption matter", the
estimate answers "could production tell these scenarios apart", and the honest
answer to the second is no.

### Consequences

The distinction between an **estimand** and an **estimate** turned out to be the
difference between a table that argues something and a table that looks broken.
The same split is now used in the demo's estimator-validation section, where the
true ATE (+3.02pp) is printed next to the estimate (+0.58pp [−2.27, +3.43]) with
"covered" against each — which is the whole of PRD §8.2 on one screen.

