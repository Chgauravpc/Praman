# LIMITATIONS — stated up front, without apology

Every honest limitation in this build has an argument that makes it a strength.
Every fabrication would have none. So this document is deliberate: a reviewer
should never find a limitation the repo did not name first.

## The transport is stubbed

**SMS and WhatsApp delivery are stubbed, and the voice call runs over local audio,
not a dialed phone line.** No message in this repo is ever presented as having been
sent, and no call is presented as having been dialed.

That is the one-line version the DoD asks for. The reason, and why it shaped the
design rather than merely limiting it:

- **DLT SMS templates and WhatsApp Business API access cannot be provisioned.**
  Both require a registered business entity and a verification process taking days
  to weeks; an individual student cannot legitimately obtain either inside the
  window. So the **constraint is real and enforced** — R5's template gate lives in
  the envelope and `channel` is a first-class field on every action — but the
  **wire itself is absent**: there is no code that sends an SMS or a WhatsApp
  message, which is precisely why none can be presented as sent. (The PRD sketched
  a `converse/channels/base.py` transport seam; it was not built, and this document
  will not claim a seam that does not exist.)
- **Outbound PSTN telephony needs a DLT-registered caller ID**, also unobtainable.
  So the voice channel is a *real* Sarvam STT → LLM turn policy → Sarvam TTS loop
  with real Hinglish and real promise extraction — over local audio, not a dialed
  call. The clip is no less convincing for being honest about the transport, and
  the README and the transcript say so.

A screenshot of a "sent" message that was never sent is the single most
disqualifying thing this project could contain. There is none.

## What was designed but not built (design-only)

Pre-declared on the cut list (BUILD-PLAN §15.2), and each is design-only in the
README, not silently missing:

- **The reflection / playbook-learning loop (§6.9)** — a reflection agent that
  reads batches of completed recoveries and proposes new playbooks, each of which
  must beat the incumbent in the A/B harness before promotion. The *honest*
  version of "self-improving agent" is: generation is cheap and unreliable, so put
  the expensive verification gate in front of it. The gate (the three-arm harness)
  is built; the generator that proposes new arms is **not**. This is the one
  genuine capability cut, and it is the right one — a low promotion rate would have
  been the feature, and there was no window to run enough rounds to show it.
- **Playbook A/B promotion machinery** — follows from the above.

**Almost nothing else on the pre-declared cut list was actually cut.** The plan
permitted dropping the simulated canary, the promise state machine, the
calibration report (Brier + reliability curve), and the guardrail-pricing table if
days slipped; none were. The canary ships (confirming the real incident, with the
refutation path proved separately), the promise machine ships with Laplace-smoothed
reliability and a Brier-scored calibration curve, and the per-rule guardrail table
falls out of the ledger for free. Where the plan and the result differ, the result
did *more*, and `STATE.md` records it day by day.

One thing built but not fully wired: the **promise state machine is not threaded
into batch resolution**. A placed voice call extracts a spoken commitment into a
`Promise` and writes it to the ledger, but no simulated batch event carries a
promise *history* from a prior call, so `EnvelopeContext.promise_state` is `"none"`
for every batch event and S3 never fires on the measurement path. The seam is named
in `STATE.md`, not hidden.

## What is real but synthetic, and graded honestly

- **The event stream is synthetic.** No production Razorpay data was available. The
  reason distribution is drawn from PSP audit figures the source itself grades as
  directional, and it is used for its **shape** rather than its digits. Graded
  **[C]** in `EVALUATION.md` §3.
- **The simulator and the policy were written by the same person, and so was the
  injected incident.** The defence is that ground truth records *what was injected*
  and the test derives the expected decomposition from raw SQL counts — but this is
  weak evidence rather than strong, and no internal test can detect an incident
  shaped to be findable by the tool built alongside it. Stated as a real threat to
  validity, not buried.
- **Ten of the eleven regulatory provisions are graded [B]** — a named primary
  instrument, but the threshold from secondary summaries, not yet matched to a
  clause. **R9 alone is [A]**, and that reading is one person's, unreviewed. The
  grade lives in the rule and the counts are derived-checked against the code, so
  this document and `SAFETY.md` cannot drift from `envelope/rules.py`.
- **R6 sits in the `R` namespace but cites a vendor document** (Razorpay's own
  retry docs), not a regulator. It keeps its id for cross-reference, declares
  `authority="vendor"`, and is excluded from the regulator-backed count.
- **The service/promotional DLT classification is a legal judgment**, not an
  engineering one — the single biggest compliance assumption in the system. It is
  flagged for counsel in `SAFETY.md`; it is not assumed silently.

## Razorpay test mode only

`Config.guard_test_mode()` refuses at load to run against a key that does not begin
`rzp_test_`. One real order and one real payment link were created in TEST mode
(idempotency and the terminal-state guard confirmed against the live API); nothing
that costs money, nothing to clean up.

## Scope of the safety evidence

The redteam suite proves each rule **fires and cites itself** over **one**
engineered violation each — it shows every rule works, not how many *ways* each can
be violated. The organic planner violation rate on the full batch is **0.0%**: the
workload did not organically trip a refusal, which is reported separately from the
redteam catch rate so the two are never conflated.

## Non-technical, and out of scope by nature

In-person availability from September collides with the third year at NMIET
(PRD §17) — a scheduling question for a human, recorded so it is not mistaken for a
technical gap.

---

For the full daily record of what broke and the mechanism added so it could not
recur, see `FAILURES.md`. For what shipped each day, see `STATE.md`.
