# Pramaan — 5-minute demo script

Every figure below is read from `build/dashboard.json` at seed 42, the committed
default. If you re-run `execute-full` before recording, re-read them — do not
recite these from memory if the artifacts have moved.

---

## Before you hit record

**Two terminals and one browser tab.**

```bash
# Terminal 1 — the server. MUST be restarted after any Python change.
cd pramaan && python -m pramaan.report.server
# Terminal 2 — kept empty, for the output-files segment
```

Open `http://127.0.0.1:8000/dashboard/` and hard-reload (Ctrl+Shift+R).

- [ ] `curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8000/api/call/start` returns **200**. A 404 means the server predates the route — restart it.
- [ ] Headline tile reads **+10.43pp**. If it reads +17.49pp you are looking at the old payment-only batch — click `snapshot`.
- [ ] Microphone works, and this origin has **already** been granted mic permission. Do that prompt before recording, not during.
- [ ] `SARVAM_API_KEY` and an LLM key are in `.env`, and you have network.
- [ ] Second tab open on `architecture.html`, ready but not focused.
- [ ] Do **not** show `assets/demo.gif`. It records the older payment-only batch (+17.49pp) and contradicts the numbers you will be saying.

---

## 1 · The problem — 0:00 to 0:36

> Screen: top of the dashboard, headline tile visible.

"A payment fails. Insufficient funds, an expired mandate, a bank outage. Someone
has to decide what to do about it — retry, message, call, or leave it alone.

Here's the trap. Razorpay's own docs say a failed payment is often followed by a
successful one, because the customer just fixes their UPI PIN and retries. So if
you build a recovery agent and count the rupees that come back, you will credit
yourself for money that was always coming back.

Pramaan measures the difference. Not the gross. The difference."

*(89 spoken words ≈ 36s. Don't rush it — this is the whole thesis.)*

---

## 2 · The architecture — 0:36 to 1:26

> Screen: switch to `architecture.html`. Hover two or three nodes as you talk.

"Events come in — real webhooks in production, a synthetic generator here. The
graph marks which is which in purple, because you should be able to see what's
scaffolding.

Every event is randomly assigned to one of three arms at detection. Arm A is
detected, diagnosed, and never acted on. Arm B is a deterministic lookup table.
Arm C is the LLM planner. All three pass through the same compliance envelope.

*(hover Envelope)* This is the piece I'd point a reviewer at first. It has zero
import edges into the LLM package — it has to catch the model, so it cannot be
the model. Every decision it makes names the rule it made it under.

And everything every stage does lands on a hash-chained ledger."

*(126 spoken words ≈ 50s)*

---

## 3 · The dashboard run — 1:26 to 2:41

> Screen: back to `index.html`.

**With the headline tile on screen:**

"6,000 events. Arm C beat the randomised control by **10.43 points** — interval
**+7.63 to +13.33**, ten thousand bootstrap resamples, excludes zero.

Now look at this one." *(point to the B−A tile)*

"Arm B, the rules-only lookup table, came in at **+1.31 points**, interval
**minus 1.44 to plus 3.96**. That crosses zero. The lookup table did **not**
beat the control. That's a negative result about my own system, on the front
page, because a dashboard that can only show you wins isn't measuring anything."

**Click `demo-dev`. Talk over it — it takes about 15 seconds.**

"These buttons run the actual CLI — the same command a reviewer would type, with
offline mode forced, so nothing here spends a token or opens a socket. That's
real stdout streaming in."

*(when the tiles flash and change)*

"6,000 events became 200. Every tile just re-derived itself from a different
ledger. Nothing on this page is a hard-coded number."

**Click `snapshot` (~5 seconds) to return to the full batch.**

"Back to the full run."

**Scroll to the envelope section:**

"935 actions amended. **1,140 refused outright** — every one citing RBI's
e-mandate pre-debit notification rule. That's the number worth reading twice.
Recovery on mandates is capped by law, not by the agent.

And the planner: **33 of 303** distinct situations were actually written by the
model; the rest fell to the deterministic fallback. I report both, because
counting the calls instead of the usable answers would have let me claim 85."

*(138 spoken words ≈ 55s, plus 20s waiting on the two runs.)*

---

## 4 · The voice agent — 2:41 to 4:07

> Screen: scroll to **Talk to it**.

"Everything so far ran with no API key. This next part can't — a conversation
that hasn't happened yet can't be replayed from a cache.

Watch who speaks first."

**Click `Start call`. Let both agent lines play. Do not talk over them.**

"It introduced itself and disclosed that it's an AI before I said a word. The
regulation doesn't ask for that disclosure to be *somewhere* in the transcript —
it asks for it to be **first**. So opening the call is a separate endpoint from
taking a turn, and the ordering is structural rather than a habit."

**Hold the button and say, in Hinglish:**

> "Haan ji, main Friday tak payment kar dunga."

**Release. Wait for the reply to play.**

"That went out as audio to Sarvam speech-to-text, through the turn-policy model,
and back through Sarvam text-to-speech. And the commitment I just made has been
pulled out into a structured promise — the date it resolved to, and the exact
words it came from."

*(153 spoken words ≈ 61s, plus ~25s of audio you don't talk over.)*

> **If the call fails on camera**, say so plainly: *"That's a live network call
> and it just failed — here's the same loop recorded."* Play
> `assets/voice-demo.mp3` (29s) and add that in the recorded clip the customer
> lines are scripted TTS, while the one you just attempted is genuine
> speech-to-text. **Do not pass the recorded clip off as a live call.**

---

## 5 · The output files — 4:07 to 4:46

> Screen: Terminal 2.

```bash
ls build/
head -2 build/actions.csv
```

"Every run writes its evidence out. `actions.csv` is one row per event — 6,000
of them: what was detected, what was proposed, what the envelope did to it, what
happened. `detected.csv` is the input, before anything judged it.
`dashboard.json` is what the page you just saw renders from — the page computes
nothing of its own.

Underneath all of it is a hash-chained ledger. **19,157 rows**, head hash
**81b81397**. Every row carries the previous row's hash, so if I'd edited one
number to make this demo look better, the chain would break and the verifier
would say so."

*(98 spoken words ≈ 39s)*

---

## 6 · Close — 4:46 to 4:58

"Clone it, run `make demo`, no API key needed. It reproduces byte for byte. And
`FAILURES.md` has every mistake I made building this, including the ones that
changed the numbers."

*(30 spoken words ≈ 12s)*

---

## The clock

Measured, not estimated: 634 spoken words at 150 wpm, plus the dead air you
cannot talk over.

| # | Segment | Speech | Dead air | Ends |
|---|---|---|---|---|
| 1 | Problem | 36s | — | 0:36 |
| 2 | Architecture | 50s | — | 1:26 |
| 3 | Dashboard run | 55s | 20s (`demo-dev` 15s + `snapshot` 5s) | 2:41 |
| 4 | Voice agent | 61s | 25s (opening ~13s, your turn ~5s, reply ~7s) | 4:07 |
| 5 | Output files | 39s | — | 4:46 |
| 6 | Close | 12s | — | 4:58 |

Segment 3 is the one that will run long, because the two runs finish on their
own schedule. If you are behind at 2:45, cut the `snapshot` click and stay on
the dev numbers — say *"and this is the 200-event batch"* — rather than cutting
the B−A null result, which is the most valuable thing in the video.

---

## Things not to say

Each of these is a true statement that a rushed narration turns into a false one.

| Don't say | Say |
|---|---|
| "recovered ₹47 lakh" | "₹47 lakh gross — **₹88,747** of it incremental" |
| "we call customers" | "test-mode adapters; no live payment API is called" |
| "the AI found the incident" | "the investigator diagnosed it; the canary checked it against ground truth" |
| "it knows the counterfactual" | "the *simulator* knows it — which is why a control arm is needed at all" |
| "100% of violations caught" | "100% of **26 engineered** violations, alongside a 19% organic correction rate" |

If asked about the money spread: the rupee figure ranges **₹0.89L to ₹12.82L**
across five seeds, a 14× swing, because order amounts are log-normal. The
percentage-point lift replicates: **+8.38 to +11.04pp**, all ten intervals
excluding zero. Seed 42 is the *lowest* of the five and is still the default.

---

## Figures used above

| Figure | Value | Source |
|---|---|---|
| C−A rate lift | +10.43pp [+7.63, +13.33], excludes zero | `dashboard.json` → `headline.contrasts["C-A"]` |
| C−B rate lift | +9.12pp [+6.32, +12.07], excludes zero | `headline.contrasts["C-B"]` |
| B−A rate lift | +1.31pp [−1.44, +3.96], **does not** exclude zero | `headline.contrasts["B-A"]` |
| Incremental / gross | ₹88,747 / ₹47,54,868 | README headline |
| Envelope | 935 amended, 1,140 rejected | README metric table |
| Planner authorship | 33 of 303 signatures LLM-authored | `plans.counts` |
| Ledger | 19,157 rows, head `81b81397…` | `provenance.ledger_rows`, `ledger_head_hash` |
| Run durations | demo-dev 15s · snapshot 5s | `report/server.py` → `RUNS` |

`demo-full` (310s) and `execute-full` (480s) are **too long to run on camera** —
that is why the script uses `demo-dev` and `snapshot`.
