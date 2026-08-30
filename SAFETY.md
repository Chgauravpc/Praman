# SAFETY — the compliance envelope, rule by rule

Every plan the system produces passes through `pramaan/envelope/judge.py` before
anything happens. The envelope is **deterministic, has no clock and no I/O, and
contains zero import edges into `pramaan/llm/`** — its job is to catch the LLM,
so it cannot be the LLM. It returns `ALLOW` / `AMEND` / `REJECT` and **always
names the rule**.

This document is the rule-by-rule reference. It is meant to be read with the
regulation open in another window: every citation names its instrument, and every
citation carries a **grade** that says how far it was actually verified.

## The grading convention — and why one bad number discredits ten good ones

| Grade | Meaning |
|---|---|
| **[A]** | Verified against the **primary instrument** — the operative words read in the RBI/TRAI text itself |
| **[B]** | A **named** primary instrument, but the threshold is taken from **secondary summaries** consistent across sources, **not yet matched to a clause** — treat as unverified at clause level |

A payments judge will spot a stale figure, and one bad number discredits the
others. So this project refuses to grade a rule [A] on secondary evidence. The
grade is stored **in the rule** (`RULE_SOURCES` in `envelope/rules.py`), the code
**refuses at import** to let a rule claim [A] without quoting the operative words,
and the counts below are **derived from that dict**, not typed by hand —
`tests/test_redteam_envelope.py::test_the_published_grade_counts_match_the_prose`
fails if this document and the code disagree. (That test exists because an earlier
draft's prose said "nine of eleven [A]" while the dict graded one — see
`FAILURES.md`, Day 2, severity 5.)

**Of the eleven regulatory rules, exactly one is [A] (R9). The other ten are
[B].** One of the eleven — R6 — cites a **vendor** document rather than a
regulator and is excluded from the regulator-backed count. This is stated plainly
because the honest count is more convincing than an inflated one.

---

## The eleven regulatory rules (R1–R11)

Each row: the rule as enforced, the instrument it cites, its grade, and its
verification status. The rule text is the enforced behaviour in
`pramaan/envelope/rules.py`; the redteam suite carries one engineered violation
per rule and asserts the rule id fires (100% caught, over one constructed case
each — it shows each rule fires and cites itself, not how many *ways* each can be
violated).

| ID | Rule (as enforced) | Instrument | Grade | Verification |
|---|---|---|---|---|
| **R1** | Pre-debit notification must reach the customer **≥24h before every mandate debit**, carrying merchant name, amount, date/time, mandate reference and reason. R1 measures the gap to the **debit**, not to the decision — so the retry scheduler and the notification scheduler must be one component | RBI Digital Payments E-Mandate Framework 2026 — RBI/DPSS/2026-27/396 & RBI/CO.DPSS.POLC.No.S56/02.14.003/2026-27, 21 Apr 2026 | **[B]** | Named instrument; 24h floor from secondary summaries, clause **not** matched — unverified |
| **R2** | First mandate transaction requires AFA. Subsequent debits AFA-exempt to **₹15,000**; to **₹1,00,000** for insurance premiums, mutual-fund subscriptions, credit-card bills | Ibid. | **[B]** | Named instrument; ₹15,000 / ₹1,00,000 thresholds from secondary summaries — unverified at clause level |
| **R3** | The **acquirer** is responsible for compliance by merchants it onboards — so a merchant's badly-behaved recovery agent is *Razorpay's* liability. Every action must be attributable | Ibid. | **[B]** | Named instrument, secondary summary — unverified |
| **R4** | Customer may opt out of one debit or withdraw the mandate entirely; withdrawal requires AFA re-validation | Ibid. | **[B]** | Named instrument, secondary summary — unverified |
| **R5** | Commercial SMS requires DLT registration of entity, header and **content template**; only template-matching messages deliver. Promotional window **09:00–21:00** | TRAI TCCCPR 2018 (quiet period traceable to TCCCPR 2010) | **[B]** | Named instrument; the flat 09:00–21:00 figure could **not** be matched to a primary clause — this was the opposite of what was expected (see below) |
| **R6** | Mandate retry norm: one attempt plus max three retries; `pending` → `halted` when exhausted | **Razorpay** subscription payment-retry documentation | **[B]** · **vendor** | Vendor doc, not a regulator. Keeps its `R6` id for cross-reference but declares `authority="vendor"` and is **excluded** from the regulator-backed count |
| **R7** | FASTag / NCMC auto-replenishment exempt from pre-debit notification. An exemption, so its failure mode is *claiming* it wrongly | RBI 2026 E-Mandate Framework | **[B]** | Named instrument, secondary summary — unverified |
| **R8** | Outbound commercial **voice** restricted to **09:00–21:00**; max **3 unsolicited calls/day** per number per company; DLT + NCPR/DND scrubbing. Penalties reported up to ₹10 lakh | TRAI UCC / TCCCPR | **[B]** | Named instrument, secondary summary — unverified |
| **R9** | **Debt-collection contact permitted only 08:00–19:00.** Contact outside is treated as harassment. Agents must identify themselves, state whom they represent and the purpose | RBI/2022-23/108, `DOR.ORG.REC.65/21.04.158/2022-23`, 12 Aug 2022 — *Outsourcing of Financial Services: Responsibilities of REs employing Recovery Agents* | **[A]** | **Verified against the primary instrument.** Operative words: agents "shall not … call the borrower before 8:00 a.m. and after 7:00 p.m." One reading, one person's — not independently reviewed |
| **R10** | An AI voice call **must disclose at the outset that the caller is automated**, before anything else | TRAI framework / DPDPA consent principles | **[B]** | Named framework; specific disclosure-first obligation from secondary summary — unverified |
| **R11** | Consent must be explicit, informed, specific and **revocable**; revocation honoured immediately and permanently | Digital Personal Data Protection Act | **[B]** | Named Act; the specific consent gradations from secondary summary — unverified |

**The finding worth stating: R9, the one that is [A], is an RBI recovery-conduct
directive, not a payments circular — and the TRAI 09:00–21:00 figure everybody
quotes is the one that could *not* be traced to a primary clause.** Day 1 recorded
R9 as [B] from commentary and flagged it as the weakest of the eleven; it was
checked against the RBI notification on Day 2 and the figure held, so it *rose* to
[A]. The expectation going in was the reverse. See `FAILURES.md`, Day 2.

### Prefixes declare authority

A system that cites a regulator for a rule it invented is worth less than one that
admits which is which. So rule ids carry a prefix: **`R`** regulation (each naming
an instrument), **`G`** a Razorpay decline-reason guardrail (futility, not law —
retrying `card_expired` is not illegal, it is impossible), **`S`** a stopping rule
(product/conduct decisions), **`P`** house policy (a threshold *we* chose — named
so nobody mistakes it for law). Padding the `R` namespace with house rules would
make the compliance story look stronger and be worth less.

---

## The window matrix — `(legal_context × channel × hour)`

Three regulators impose three different clocks, and the most valuable hour of the
day falls outside two of them. `pramaan/envelope/windows.py` encodes this as a
**literal table** meant to be read against the regulation, plus a hand-written
hour-by-hour grid that `_self_check` cross-verifies at import (a typo in either is
caught by the other).

```
      08:00   09:00                    19:00   21:00   22:00        08:00
        │       │                        │       │       │            │
R9  ────├───────┴────────────────────────┤       │       │            │
        │  DEBT COLLECTION 08:00–19:00   ╳       │       │            │
R5/R8   │       ├────────────────────────────────┤       │            │
        │       │  PROMOTIONAL / VOICE 09:00–21:00        │            │
svc     ├───────┴────────────────────────────────┴───────┴────────────┤
        │  SERVICE / TRANSACTIONAL — no statutory time band            │
peak    │       │                        ├───────────────┤            │
        │       │        19:00–22:00 most money at risk, collection SHUT
```

The consequences the envelope encodes:

- **Interval convention is closed at both ends, second precision.** RBI prohibits
  contact "after 7:00 p.m.", so `[08:00:00, 19:00:00]` is permitted and
  `19:00:01` is not. A half-open `[08:00, 19:00)` would refuse a lawful contact at
  exactly 19:00:00; rounding to the hour would permit an unlawful one at 19:59.
  Both are boundary bugs this table exists to make visible.
- **Voice is the intersection R9 ∩ R8 = 09:00–19:00 for collection.** The two are
  kept separate so the envelope can name *which* rule shut the door: refused at
  08:30 it cites **R8** (R9 open, voice not yet), refused at 19:30 it cites **R9**.
- **A silent action (retry, routing change, wait) is not a communication**, so no
  time band reaches it — which is the only reason the 19:00–22:00 failure peak is
  recoverable at all. Most builds gate retries hard and messaging loosely; that is
  exactly backwards on reversibility (see the tiers below).

### The DLT service/promotional classification — flagged for legal review

The single biggest compliance assumption in the system is that a **payment-retry
link is a *service / transactional* message** carrying no statutory time band,
rather than a *promotional* one. This is a **legal judgment, not an engineering
one**, and it is what buys access to the evening failure peak. The matrix encodes
the service classification because that is the design under test; it is **stated,
not buried**, and the correct operational posture (per PRD §7) is: do not assume
it, register the template in the correct DLT category, and have counsel review it.
Getting it wrong in the permissive direction risks a TRAI penalty (reported up to
₹10 lakh) against the merchant — and under R3 that is the acquirer's liability.

---

## Reversibility tiers T0–T4 (`envelope/tiers.py`)

"Bounded" without reversibility is just a counter. Each action sits in exactly one
tier, and the tier decides which gates it must clear.

| Tier | Actions | Gate | Cost of getting it wrong |
|---|---|---|---|
| **T0** silently reversible | routing change, `ACT_WAIT` | auto | nothing — undo it and nobody knew |
| **T1** reversible, visible | charge retry | auto within caps + idempotency + R1/R2/R6 | a refund, possibly a fee |
| **T2** irreversible | any message | legal window + category + budget + consent | a person read something they should not have |
| **T3** irreversible, high-touch | voice call | T2 + R8/R9/R10 + a ₹500 value floor (P2) | a person was telephoned — possibly harassed |
| **T4** irreversible, costly | discount, waiver, human escalation | human approval above ₹5,000 (P3) | money given away, or a human's hour spent |

**A charge retry is refundable; a message is not, and a phone call really is not.**

---

## The seven stopping rules S1–S7 (`envelope/stopping.py`)

Each has a number, a numeric trigger, and a unit test. Three end the thread (S1,
S4, S7 — terminal); four are economic/courtesy limits (S2, S3, S5, S6). The order
is chosen so the citation is the most informative true statement available.

| # | Rule | Trigger |
|---|---|---|
| **S1** | **Already paid** — the single most important rule | `order_already_paid`, or a Smart Collect virtual-account credit match. Terminates instantly |
| **S2** | Contact budget | Per-counterparty cap over a rolling window; plus R8's hard 3 calls/day ceiling |
| **S3** | Promise honoured | A recorded promise pauses all contact until its date, then re-evaluates once |
| **S4** | Consent withdrawn / mandate withdrawn | Permanent stop, never re-attempted (R4, R11) |
| **S5** | Merchant circuit breaker | Complaint/unsubscribe rate crosses a threshold → halt the campaign, before the DLT header is blocked (which would end the merchant's ability to message *anyone*) |
| **S6** | Diminishing returns | Expected value of the next step below its cost → stop. Prevents the infinite polite nudge |
| **S7** | **Distress / dispute / legal signal** | Hardship, a disputed charge, or a legal threat → the agent stands down immediately, hands to a human, and logs the words **verbatim**. Non-negotiable under R9 |

S7 is the one that matters ethically and the one a judge will notice is missing
from everyone else's build. An agent that keeps chasing someone who has said "I
have lost my job" is not a product, it is a liability — and under R9's conduct
rules it is harassment. In this system a distress signal on a voice turn stops the
call **even when the same sentence also contains a promise**, and extracts no
promise from it (`tests/test_voice.py::test_distress_beats_a_promise_in_the_same_breath`).

---

## What the envelope does *not* claim

- **DND/NCPR** is enforced by **R5** (messages) and **R8** (voice), not by S4 —
  the register is TRAI's instrument, so a refusal citing it names the TRAI rule.
- The redteam suite proves each rule **fires and cites itself** over **one**
  constructed violation each. It is not a measure of how many ways each rule can
  be broken.
- The organic planner violation rate on the full 6,000-event batch is **0.0%** —
  the workload did not organically trigger a refusal. That is reported *separately*
  from the redteam catch rate on purpose; conflating "the envelope is potent" with
  "the traffic triggered it" is the easiest way to make a compliance claim that
  sounds strong and proves nothing (PRD §8).

For the code: `envelope/rules.py` (R1–R11 with `RULE_SOURCES` and grades),
`windows.py` (the matrix), `reason_map.py` (G1–G8), `tiers.py` (T0–T4),
`stopping.py` (S1–S7), `judge.py` (the evaluation order and why it is the
interesting part).
