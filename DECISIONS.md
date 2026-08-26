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

