# Pramaan -- Hinglish voice recovery call (transcript)

A single-turn Sarvam STT -> LLM turn policy -> Sarvam TTS call. This transcript is produced with **no API key** (the deterministic turn policy); the audio clip needs a live `SARVAM_API_KEY`. Every line below cleared the same policy envelope the rest of the system runs on.

**Compliance, gated by `pramaan.envelope.judge`, not asserted here:**

- **R10** -- the AI disclosure is the first utterance, unconditionally.
- **R9** -- placed inside 08:00-19:00 IST; the opening states who is
  calling, whom they represent and why.
- **R8** -- within the 3-unsolicited-calls-per-number-per-day cap.
- **S7** -- a distress / dispute / legal signal stands the agent down
  immediately (not exercised in this call; see `tests/test_voice.py`).

Envelope pre-flight on the `ACT_VOICE` step: **ALLOW**, citing **R9**.

| # | Speaker | Line |
|---|---|---|
| 1 | **Agent (AI)** _(scripted)_ | Namaste, main ek AI voice assistant hoon -- ek automated call, insaan nahi. Yeh call recording ke liye hai. Theek hai? |
| 2 | **Agent (AI)** _(scripted)_ | Main Asha bol rahi hoon, Zappay ki taraf se. Call ki wajah -- a failed autopay for your electricity bill. Aapko do minute baat karne mein theek rahega? |
| 3 | Customer | Haan ji boliye, kya baat hai? |
| 4 | **Agent (AI)** | Koi baat nahi. Aap bata sakti hain kab tak payment ho payega? Ek date mil jaaye toh main note kar loon. |
| 5 | Customer | Achha wo bijli ka bill... haan mujhe pata hai payment fail ho gaya tha. |
| 6 | **Agent (AI)** | Ji haan, wahi payment. Tension lene ki zaroorat nahi -- bas bata dijiye kab tak ho payega? |
| 7 | Customer | Abhi thodi cash ki dikkat chal rahi hai, salary aane wali hai. |
| 8 | **Agent (AI)** | Samajh sakti hoon, koi baat nahi. Jab salary aa jaaye, ek date bata dijiye jab tak ho jayega -- main wahi note kar loongi. |
| 9 | Customer | Theek hai, Friday tak pakka kar dunga, pura amount. |
| 10 | **Agent (AI)** | Bilkul theek hai, main note kar leti hoon. Us date tak koi aur call nahi aayegi. Shukriya! |

## Promise extracted from speech

The state machine (`pramaan.converse.promises`) received:

- **state:** `promised`
- **promised date:** `2026-08-14T15:20:00+05:30`
- **channel:** `voice`
- **verbatim:** "Theek hai, Friday tak pakka kar dunga, pura amount."

That commitment is written to the hash-chained ledger as a `PROMISE` row alongside the `CONVERSE` row carrying the verbatim transcript.
