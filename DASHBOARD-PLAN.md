# Dashboard build plan

Status: **revised 2026-09-04, in build.** Revision 2 replaces a plan whose
expensive half was specified against ledger data that does not exist. The
corrections are in §1; read them before §3, because they are the reason the
estimate fell from 12-15h to roughly 6.

Scope is unchanged in spirit: a **post-ship, additive, read-only** presentation
layer over what `pramaan/` already computes. No redesign, no new reasoning, no
new dataset. Written against the Day 8 SHIP state (586 tests green, headline
frozen). `README.md` defines every number this renders.

---

## 1. What revision 1 got wrong

Revision 1 asserted a shape for the ledger that the committed artifacts do not
have. Each of these was checked against the files in `build/` and
`tests/golden/` rather than against the PRD's intent, and each one invalidated a
milestone:

| Revision 1 claimed | Actually true |
|---|---|
| `ACTION` rows carry the operational counters | **There are zero `ACTION` rows in any committed DB.** `build/pramaan-execute-full.db` — which rev 1 named as the timeline source, "where real ACTION rows with idempotency keys live" — contains 263 `PLAN` rows and nothing else |
| Every ledger row's payload references a `counterparty_id` | `counterparty_id` appears on `DETECT` only. `GATE`/`OUTCOME`/`EXCEPTION` carry `event_id`; `CONVERSE`/`PROMISE` carry `counterparty_id` but no `event_id` |
| `PLAN` rows sit on a customer's timeline | `PLAN` payloads carry neither key — only a `signature` bucket (`reason_class=…\|amount_band=…\|segment=…`) shared across many events. A plan is memoised per signature, so it is **not** a per-customer fact and cannot honestly be drawn as one |
| `PLAN` joins to `DIAGNOSIS` via `llm_call_ids` | No `DIAGNOSIS` rows exist outside the coverage golden (which holds exactly 1), and 177 of 263 `PLAN` rows have `llm_call_ids: []` — the NFR-2 deterministic fallback |
| Five category bars from `source_type` | All 6,000 events in `build/pramaan-full.db` are `source_type="payment"`. `generate_all_types` exists but does not back the committed batch, and §2 forbids re-generating one |
| The headline tiles come off the ledger | **No ledger holds the headline.** `+17.49pp` / `₹7,44,967` are computed in memory by `run_shadow` inside `cli.run_execute`, printed, and discarded |

In `tests/golden/all_kinds.jsonl` — the only artifact containing all twelve
kinds in one chain — every `ACTION`, `PLAN`, `DIAGNOSIS`, `RECEIPT_AUDIT`,
`CANARY` and `RETRACTION` row has `event_id = None`. The brief's mockup wants a
per-customer thread `DETECT → GATE → PLAN → ACTION → CONVERSE/PROMISE →
OUTCOME`. **The ledger is not keyed for that thread**, and re-keying it means
rewriting the frozen, hash-chained, test-pinned layer this plan promises not to
touch. So the per-customer timeline is cut (§5), and what replaces it is the
part that was always the actual differentiator: the *why*.

## 2. Ground rules

The project's credibility rests on invariants I1–I9 and on the envelope /
execute / arm-assignment layers having zero import edges into `pramaan/llm`.
Presentation must not become a second, informal way to touch any of that.

- **Read-only, and enforced.** `snapshot.py` opens SQLite through a
  `file:…?mode=ro` URI. It deliberately does **not** construct a `Ledger`:
  `Ledger.__init__` runs `executescript(SCHEMA)` and commits, which is a write,
  and a read-only claim that only holds by convention is not a claim. It never
  calls `envelope.judge`, `plan.planner`, `execute.razorpay`, or anything under
  `pramaan/llm`, at any time. A test asserts no `pramaan.llm.*` module is in
  `sys.modules` after importing it.
- **One producer per number.** `eval/metrics.py` opens by warning that if two
  layers can each derive the headline, they can disagree, "and then there are
  two truths and no audit trail". The dashboard therefore **never recomputes**
  a contrast. `run_execute` serialises the `BatchMetrics` it already built
  (§3), and the snapshot reads that file. Every other tile is a *count of rows
  the ledger recorded*, which is reading, not deriving.
- **No fabricated data.** Every tile and every field traces to a real row or a
  real dataclass field. Revision 1's seeded fake-name table for anonymised
  counterparties is **cut entirely** — inventing "Acme Corp" in a project whose
  name means *proof* costs a paragraph of self-justification and buys a reviewer
  the chance to screenshot invented data. Anonymised IDs render as themselves.
- **Every number carries its population.** `Contrast.actionable_only` exists
  precisely "so that a caller cannot print a subgroup figure as though it were
  the headline". The raw ledger C−A over all 6,000 outcomes is +4.41pp; the
  headline is +17.49pp on the pre-specified actioned subset. **A tile that shows
  one and implies the other is the exact category error README warns about.**
  Arm, batch, and population are part of the tile, not a footnote.
- **No new architecture, no new dependencies, no new data.** No server, no
  framework, no build step, no LLM call, no re-run of `sim.generate`, no
  regenerated seed. `make demo`'s keyless promise (I9) stays intact.
- **Nothing writes back.** No editing from the UI, no approving or overriding an
  agent action — that would reintroduce the informal second path into the
  envelope that this section rules out.

## 3. Architecture

```
cli.run_execute ──▶ build/execute-<batch>-metrics.json ─┐
  (already computes BatchMetrics; now also serialises)  │
build/pramaan-full.db  (DETECT/GATE/OUTCOME/EXCEPTION) ─┼──▶ pramaan/report/snapshot.py ──▶ build/dashboard.json ──▶ dashboard/index.html
tests/golden/all_kinds.jsonl  (the why exemplar)       ─┘     (new, offline, read-only, no LLM)      (new, static)        (new, static, no build step)
```

**`pramaan/report/snapshot.py`** (new). Pure Python, no LLM edge. Three jobs:

1. `metrics_to_dict(BatchMetrics) -> dict` — a faithful serialiser of the
   object `run_shadow` already returns. Computes nothing. `cli.run_execute`
   calls `write_metrics()` after it prints, so the printed number and the
   rendered number are the same object serialised twice, and cannot drift.
2. `read_ledger(path)` — read-only connection, one pass over `ledger` ordered
   by `seq`, joining `GATE`/`OUTCOME`/`EXCEPTION` to their `DETECT` row on
   `event_id` (the one join the data actually supports).
3. `build_snapshot(...) -> dict` → `build/dashboard.json`, fixed shape (§4),
   deterministic given fixed inputs — same discipline as I8, and tested as
   byte-identical across two runs.

**`dashboard/`** — `index.html`, `app.js`, `style.css`. Plain HTML/CSS/JS, no
framework, no build step, no charting library. This is a demo-day artifact, and
a build step is one more thing that can fail keyless reproduction.

**`make dashboard`** — runs the snapshot, prints the output path, and states
plainly that Chrome will block `fetch()` over `file://` so a static server is
needed (`python -m http.server` from `dashboard/`). Documented exactly, so it is
not a surprise on demo day.

## 4. Data contract: `build/dashboard.json`

`"schema_version": 1` at top level so the frontend fails loudly on drift rather
than rendering blanks silently.

### 4.1 `headline` — from the serialised `BatchMetrics`, never recomputed

Each contrast (`B-A`, `B-A actioned`, `C-B`, `C-A`) carries `point`, `low`,
`high`, `method`, `resamples`, `confidence`, `excludes_zero`, `n_treatment`,
`n_control`, and **`actionable_only`**. The UI renders `actionable_only` as
visible population text on the tile. `fallback_reason` is carried through: an
interval labelled BCa that silently fell back to percentile would be a lie about
the method, and the method is the part being trusted.

### 4.2 `ledger` — counts of what was actually recorded

Per arm, from `OUTCOME` rows joined to `DETECT`: `n`, `recovered`,
`at_risk_paise`, `recovered_paise`, `contacted`, and the histogram of
`OUTCOME.action` (`ACT_WAIT`, `ACT_RETRY`, `ACT_MESSAGE`, `ACT_ESCALATE_HUMAN`,
…). Escalations come from that histogram plus the `EXCEPTION` rows — **not**
from `ACTION` rows, which do not exist. `GATE` verdicts (4,905 ALLOW / 1,095
AMEND / 0 REJECT on the full batch) come from `GATE.decision`.

### 4.3 `by_category`

Grouped on `DETECT.source_type`. On the committed batch this is **one group,
`payment`, and the UI must say so** rather than drawing four empty bars. The
renderer is written for N groups so a future multi-type batch needs no code
change; it is honest about N=1 today.

### 4.4 `why` — the exemplar, from `tests/golden/all_kinds.jsonl`

The brief's "why did the agent do this?" panel, rendered from real rows in the
one chain that contains every kind. A fixed, hand-picked set: one `GATE`
(`rule_fired`, `decision`, `reason`), one `PLAN` step (`rationale`,
`stop_conditions`, `expected_value_paise`, `cost_paise`), one `DIAGNOSIS`
(`confidence`, `falsifiable_by`, `diagnosis_class`) and its `RECEIPT_AUDIT`
(`claims_in`, `claims_surviving`, `receipt_coverage`, `may_plan_action`), one
`ACTION` (`action_type`, `idempotency_key`, `idempotent_replay_was_noop`).

These are **not** joined into a single customer's story, because they cannot
honestly be joined (§1). They are presented as what they are: the audit record
of one run, each panel labelled with its ledger `seq` and `kind`. The
`DIAGNOSIS` → `RECEIPT_AUDIT` pairing is the one worth showing on camera — it
is the only place a reviewer sees what the model claimed next to what survived
a deterministic check of it.

## 5. Milestones

1. **`metrics_to_dict` + `write_metrics`, wired into `run_execute`.** One
   additive call after the existing print. Changes no computation and no printed
   number. Test: the serialised point estimate equals
   `metrics.contrasts["C-A"].intervals["rate"].point` exactly. *~1h.*
2. **`snapshot.py`: `headline` + `ledger` + `by_category`.** Read-only
   connection, the `event_id` join, §4.1–4.3. Tests: byte-identical across two
   runs; the no-`pramaan.llm`-import assertion; paise figures cross-checked
   against a direct SQL aggregate. *~2h.*
3. **Overview page.** Tile row + hand-rolled CSS bars + the gate/action
   histograms, reading `build/dashboard.json`, with population labels per §2.
   *~1.5h.*
4. **`why` exemplar in snapshot + the panel UI.** §4.4. No dropdown, no drawer
   state, no cross-row joins — panels rendered inline. *~1h.*
5. **`make dashboard` + a `## Try it` paragraph in README**, matching its
   existing style. *~0.5h.*

**Total: roughly 6 hours.** Stop after any milestone and what exists is honest
and demoable.

## 6. Open questions

- **Pre- or post-submission?** `STATE.md` says the remaining work is push,
  record video, submit form. The video is what the rubric requires; a dashboard
  that competes with it is the wrong trade. Six hours that makes the video
  better is not. If the form is unsubmitted, milestone 3 is the natural stop.
- **Video inclusion?** If this lands before recording, a 15–20s pan over the
  Overview and the `DIAGNOSIS`/`RECEIPT_AUDIT` pair is a strong addition to
  `VIDEO-SCRIPT.md`. Flag once milestone 4 is done; do not block on it.

## 7. Explicitly not doing

Per-customer timelines (§1: the ledger is not keyed for them). Fabricated
display names. Live/streaming updates, websockets, polling. Auth, multi-user,
deployment. Any write path from the UI. Any re-run of `sim.generate`, any new
seed, any new synthetic dataset.
