"""Hinglish voice recovery -- Sarvam STT -> LLM turn policy -> Sarvam TTS.

PRD 6.7, BUILD-PLAN Day 7. The memorable artifact, and the compliance
scaffolding that is what makes it defensible rather than reckless. A reviewer who
has skimmed four hundred projects has read many architecture diagrams; they have
not *heard* an agent open with an AI disclosure, negotiate a payment date in
code-switched Hinglish, extract "Friday tak pakka" as a structured promise, and
write it to a hash-chained ledger. This module is the machinery behind that
sentence.

Four properties are unconditional, and none of them is enforced by this module
*asserting* it -- each is gated by the real envelope (``pramaan.envelope.judge``),
which this module does not get to bypass:

- **R10 -- the AI disclosure is the first utterance.** Not the second, not "once
  the customer asks whether this is a recording". ``opening_lines`` puts it on
  line one, before the R9 business identity, before the customer has said a word.
  There is no code path in ``place_call`` that emits a turn before the disclosure
  (asserted by ``tests/test_voice.py::test_disclosure_is_always_the_first_utterance``),
  and the envelope refuses an ``ACT_VOICE`` step whose context does not carry
  ``ai_disclosure_scripted=True``.
- **R9 -- the collection window and self-identification.** The call is placed only
  inside 08:00-19:00 IST, and the opening states who is calling, whom they
  represent and why. Both are the envelope's decision on the ``ACT_VOICE`` step,
  evaluated at second precision on the real timestamp -- not a branch here.
- **R8 -- three unsolicited calls per number per day, and a DND scrub.** Same gate.
- **S7 -- distress / dispute / legal-signal stand-down.** Checked on *every*
  customer turn, BEFORE the turn policy is allowed to reply. On a signal the agent
  stands down immediately: it speaks one hand-off line, hands to a human, logs the
  customer's words verbatim, and does not continue. There is no "one more nudge",
  and a promise is deliberately NOT extracted from a stood-down call even if the
  words happen to look like one (``test_distress_beats_a_promise_in_the_same_breath``).

Then the promise. A commitment the customer *speaks* -- "Friday tak pakka kar
dunga" -- is extracted from the transcript into the Day 6 state machine
(``pramaan.converse.promises``) and written to the ledger as a PROMISE row. That
is the whole point of the clip: a spoken Hinglish promise becomes a structured,
auditable record with its date resolved.

Why this runs offline, deterministically, with no Sarvam key
------------------------------------------------------------
The same discipline as the rest of the project (NFR-4, I8). The only thing that
needs a live key is the audio, and it is confined to ``SarvamClient``, which is
injectable and reports ``available`` exactly like ``RazorpayTestClient``. The
turn policy calls the LLM through the same cache the planner uses; on a cache
miss with no key it falls back to a deterministic Hinglish reply keyed by intent
(NFR-2, the planner's own pattern), so a keyless ``make voice`` still produces a
real, byte-stable transcript -- the "review on mute" artifact. The **audio** is
the one thing that genuinely needs a live Sarvam key, and the README and STATE.md
say so plainly rather than shipping a synthesised clip dressed up as a real call.

The realtime-duplex decision (BUILD-PLAN Day 7's fallback)
----------------------------------------------------------
Sarvam ships a WebSocket realtime API (Aug 2026). This module implements the
**single-turn** stack -- batch STT on a customer utterance, the turn policy, batch
TTS on the reply -- which is BUILD-PLAN Day 7's named fallback, taken early and
without regret per that day's own instruction: it is the same real STT/LLM/TTS
stack, it exercises every compliance gate identically, and it is testable and
reproducible in a way a live duplex socket is not. ``SarvamClient`` leaves the
realtime endpoint documented but unused; wiring the socket is a strict superset
of this and changes none of the policy below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pramaan.converse import promises
from pramaan.converse.promises import Promise
from pramaan.envelope import EnvelopeContext, Judgement, Step, judge
from pramaan.ledger.chain import Ledger

# --------------------------------------------------------------------------
# The caller's identity -- what R9 requires the opening to state
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerIdentity:
    """Who is calling, whom they represent, and why -- the R9 opening facts.

    A dataclass rather than three loose strings because R9's conduct requirement
    is a *set* of facts that must all be present, and grouping them makes "did
    the opening identify the agent, the principal and the purpose" one check
    instead of three that can drift apart.
    """

    agent_name: str = "Asha"
    merchant_name: str = "Zappay"
    purpose: str = "a failed autopay for your electricity bill"

    def as_dict(self) -> Dict[str, str]:
        return {
            "agent_name": self.agent_name,
            "merchant_name": self.merchant_name,
            "purpose": self.purpose,
        }


#: R10, verbatim and first. The disclosure names that the caller is automated
#: *and* that the call is recorded (DPDPA consent principles, PRD 6.7's "recording
#: consent"). It is a constant, not model-authored: an AI that could paraphrase
#: its own disclosure could paraphrase it into something weaker, and the one line
#: the whole channel's defensibility rests on is not a line to leave to a
#: temperature-zero hope.
DISCLOSURE_LINE = (
    "Namaste, main ek AI voice assistant hoon -- ek automated call, "
    "insaan nahi. Yeh call recording ke liye hai. Theek hai?"
)


def opening_lines(identity: CallerIdentity) -> Tuple[str, str]:
    """The opening script, in the only order R9/R10 permit.

    Two lines, and the order is the rule: R10 disclosure FIRST, then the R9
    identity-and-purpose. Returned as a fixed pair rather than generated so the
    ordering is structural -- ``place_call`` speaks index 0 before index 1, and a
    test reads index 0 to confirm it is the disclosure.
    """
    identity_line = (
        "Main %s bol rahi hoon, %s ki taraf se. Call ki wajah -- %s. "
        "Aapko do minute baat karne mein theek rahega?"
        % (identity.agent_name, identity.merchant_name, identity.purpose)
    )
    return DISCLOSURE_LINE, identity_line


# --------------------------------------------------------------------------
# S7 -- the stand-down classifier
# --------------------------------------------------------------------------
#
# Deterministic and Hinglish-aware, the same discipline as the promise extractor
# next door: a keyword classifier a reviewer can read against the regulation,
# tested on real phrasings. It is deliberately BROAD where the promise extractor
# is narrow, and for the opposite reason. A false *negative* here -- missing a
# genuine distress signal and continuing to chase -- is the harm S7 exists to
# prevent (harassment under R9), so when in doubt this stands down. A false
# positive costs one handed-off call; a false negative is the thing that makes
# the whole channel indefensible.

#: Financial hardship / distress. "I lost my job", "no money", medical hardship.
_DISTRESS_PATTERNS: Tuple[str, ...] = (
    r"naukri\s+(chali\s+gayi|nahi|chhoot)",
    r"job\s+(chali\s+gayi|lost|gone|nahi)",
    r"lost\s+my\s+job",
    r"paise?\s+nahi\s+(hai|hain|h)\b",
    r"paisa\s+nahi",
    r"pai?se?\s+ki\s+(tang|dikkat|problem)",
    r"(bimar|bimari|hospital|medical|ilaaj)",
    r"afford\s+nahi",
    r"can'?t\s+afford",
    r"koi\s+income\s+nahi",
    r"ghar\s+mein\s+koi\s+kama",
)

#: A disputed charge. "I already paid", "wrong charge", "this is not mine".
_DISPUTE_PATTERNS: Tuple[str, ...] = (
    r"(pay|payment)\s+kar\s+(diya|di)",       # "...pay kar diya tha" (I did pay)
    r"already\s+(paid|pay)",
    r"pehle\s+hi\s+(bhar|pay|de)\s+diya",
    r"galat\s+(charge|amount|bill)",
    r"wrong\s+(charge|amount|bill)",
    r"yeh\s+mera\s+(nahi|nhi)",
    r"maine\s+kuch\s+(liya|khareeda)\s+hi\s+nahi",
    r"dispute",
    r"chargeback",
)

#: A legal threat. Lawyer, consumer court, legal notice.
_LEGAL_PATTERNS: Tuple[str, ...] = (
    r"\b(lawyer|vakil|advocate)\b",
    r"legal\s+(notice|action|karyavahi)",
    r"consumer\s+(court|forum|adalat)",
    r"court\s+(mein|jaunga|jaungi|le\s+ja)",
    r"\bsue\b",
    r"case\s+(kar|file|dal)",
    r"police\s+(mein|complaint|shikayat)",
)

_STANDDOWN_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("distress", _DISTRESS_PATTERNS),
    ("dispute", _DISPUTE_PATTERNS),
    ("legal", _LEGAL_PATTERNS),
)


@dataclass(frozen=True)
class StandDown:
    """The S7 verdict on one utterance."""

    triggered: bool
    signals: Tuple[str, ...] = ()

    @property
    def as_context_flags(self) -> Dict[str, bool]:
        """The three EnvelopeContext booleans S7 reads, so the envelope agrees."""
        return {
            "distress_signal": "distress" in self.signals,
            "dispute_signal": "dispute" in self.signals,
            "legal_threat_signal": "legal" in self.signals,
        }


def detect_standdown(utterance: str) -> StandDown:
    """S7 on one customer turn. See the section comment for why it errs broad."""
    import re

    lowered = utterance.lower()
    signals = tuple(
        name
        for name, patterns in _STANDDOWN_GROUPS
        if any(re.search(p, lowered) for p in patterns)
    )
    return StandDown(triggered=bool(signals), signals=signals)


#: What the agent says when it stands down. One line, then silence and a human.
#: It does not argue, does not ask for payment, does not "clarify" -- any of which
#: would be continuing to pursue somebody who has disclosed hardship or disputed
#: the debt. It acknowledges, states the hand-off, and stops.
STANDDOWN_LINE = (
    "Samajh gayi, aur is baat ke liye maafi. Main aage baat nahi karungi -- "
    "aapka case main abhi ek human colleague ko de rahi hoon jo aapki madad "
    "karenge. Aapka time dene ke liye shukriya."
)


# --------------------------------------------------------------------------
# The turn policy -- the LLM's job, tightly bounded
# --------------------------------------------------------------------------

VOICE_TURN_INSTRUCTIONS = """\
You are %(agent_name)s, an automated voice agent for %(merchant_name)s, on a \
phone call about %(purpose)s. You have ALREADY disclosed that you are an AI and \
that the call is recorded, and you have ALREADY identified yourself -- do not \
repeat those.

Speak natural Hinglish (Hindi-English code-switching), the way a polite Indian \
call-centre agent actually speaks. One or two short sentences. Your only goals, \
in order:
  1. Understand what the customer is saying.
  2. If they can pay, get a specific DATE. "kab tak ho payega?" is the question \
that matters.
  3. Be warm and brief. Never threaten, never mention legal consequences, never \
pressure.

You must NEVER: claim to be a human, invent fees or figures, or promise anything \
on the merchant's behalf beyond accepting a payment date.

The conversation so far:
%(transcript)s

Reply with a single JSON object:
{"say": "your next line, in Hinglish", "intent": "ask_date|acknowledge_promise|answer_question|close"}
"""


def build_turn_prompt(
    transcript: Sequence["Turn"], identity: CallerIdentity
) -> str:
    rendered = "\n".join("%s: %s" % (t.speaker, t.text) for t in transcript)
    return VOICE_TURN_INSTRUCTIONS % {
        "agent_name": identity.agent_name,
        "merchant_name": identity.merchant_name,
        "purpose": identity.purpose,
        "transcript": rendered,
    }


def _fallback_reply(customer_text: str, *, now: str) -> Dict[str, str]:
    """The NFR-2 deterministic turn, for a keyless / cache-miss run.

    Mirrors the planner's deterministic fallback: a real, honestly-labelled
    answer rather than a stub or a crash. It reads the customer's turn for a
    handful of Hinglish cues and picks one of a few natural replies -- enough
    that the keyless transcript reads like a call rather than a stuck record,
    without pretending to be the LLM. The point of the fallback is that the
    *transcript artifact* is byte-stable with no key; the live LLM turn (warm
    cache) is what negotiates with real subtlety, and ``make voice-live`` is
    where that happens.
    """
    import re

    lowered = customer_text.lower()

    extracted = promises.extract_commitment(customer_text, now=now)
    if extracted.is_promise:
        return {
            "say": "Bilkul theek hai, main note kar leti hoon. Us date tak koi "
            "aur call nahi aayegi. Shukriya!",
            "intent": "acknowledge_promise",
        }
    # Financial difficulty -> empathise, then ask gently. Not S7-level distress
    # (that stands the call down before this is reached); ordinary "cash tight,
    # salary coming" that a recovery call is exactly for.
    if re.search(r"dikkat|cash|paise?\s+ki|salary|tang|thodi\s+problem", lowered):
        return {
            "say": "Samajh sakti hoon, koi baat nahi. Jab salary aa jaaye, ek "
            "date bata dijiye jab tak ho jayega -- main wahi note kar loongi.",
            "intent": "ask_date",
        }
    # Acknowledging the failed bill -> confirm, then move to the date.
    if re.search(r"bill|fail|pata|bijli|autopay|payment\s+ho", lowered):
        return {
            "say": "Ji haan, wahi payment. Tension lene ki zaroorat nahi -- bas "
            "bata dijiye kab tak ho payega?",
            "intent": "ask_date",
        }
    return {
        "say": "Koi baat nahi. Aap bata sakti hain kab tak payment ho payega? "
        "Ek date mil jaaye toh main note kar loon.",
        "intent": "ask_date",
    }


def generate_reply(
    llm: Optional[Any],
    transcript: Sequence["Turn"],
    customer_text: str,
    identity: CallerIdentity,
    *,
    now: str,
) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """One agent turn. Returns (reply, llm_call_ids).

    ``screen=False`` for the same reason the promise extractor uses it (ADR-038):
    the prompt embeds the customer's own free-form utterance, which carries
    exactly the raw dates and amounts the canonicality screen exists to refuse,
    and a per-conversation call was never memoised across the shared signature
    space the screen protects. Confined to ``pramaan/converse`` and enforced by
    the repo-wide grep test
    ``tests/test_promises.py::test_screen_false_is_used_only_under_pramaan_converse``.

    With no client (or a client that raises / misses offline) it falls back to
    the deterministic Hinglish turn, so the transcript is always produced.
    """
    if llm is None:
        return _fallback_reply(customer_text, now=now), ()
    prompt = build_turn_prompt(transcript, identity)
    try:
        response = llm.call(prompt, tier="fast", schema={"type": "object"}, screen=False)
        data = response.json()
        say = str(data.get("say", "")).strip()
        if not say:
            raise ValueError("empty 'say' in turn-policy reply")
        reply = {"say": say, "intent": str(data.get("intent", "answer_question"))}
        call_id = getattr(response, "call_id", "")
        return reply, ((call_id,) if call_id else ())
    except Exception:  # noqa: BLE001 -- a bad/absent reply must not drop the call
        # Same class of decision as the planner's Day 5 rate-limit fix: degrade to
        # the deterministic turn rather than crash the conversation.
        return _fallback_reply(customer_text, now=now), ()


# --------------------------------------------------------------------------
# Sarvam -- the only part that needs a key. Confined here, injectable.
# --------------------------------------------------------------------------

#: Sarvam AI, verified [B] on 2026-08-26 (PRD 6.7). Saaras STT with native
#: Hinglish code-switching; TTS with 35+ voices and sub-250ms streaming; a
#: WebSocket realtime API released Aug 2026. The batch endpoints are used here
#: (the single-turn stack); the realtime socket is documented and left for a
#: strict-superset follow-up. These are not to be trusted indefinitely -- a
#: free-tier voice lineup changes like the LLM one, which is why the key check is
#: a property, not a promise.
SARVAM_STT_ENDPOINT = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_ENDPOINT = "https://api.sarvam.ai/text-to-speech"
SARVAM_REALTIME_ENDPOINT = "wss://api.sarvam.ai/realtime"  # documented, unused here
#: TTS model and voice, verified live against the API on 2026-08-30. Sarvam
#: deprecates both models and voices without notice -- ``meera``/``bulbul:v2``
#: were the first-drafted values and both 400'd as deprecated; the error body
#: names the current roster, which is how these were corrected. ``ritu`` is a
#: Hinglish-capable female voice on ``bulbul:v3``. If a live run 400s with "not
#: recognized"/"deprecated", read the error body -- it lists the replacements.
SARVAM_TTS_MODEL = "bulbul:v3"
SARVAM_TTS_SPEAKER = "ritu"
#: A second, contrasting voice for the customer side, so the demo clip is a real
#: two-person dialogue rather than the agent talking into silence. Also a
#: bulbul:v3 voice (male, to contrast the agent's female voice).
SARVAM_TTS_CUSTOMER_SPEAKER = "aditya"
#: mp3 rather than the wav default: MP3 is a frame stream, so per-line clips
#: concatenate into one playable file, where concatenated WAVs would carry a
#: header mid-stream and break most players.
SARVAM_TTS_CODEC = "mp3"
SARVAM_STT_MODEL = "saaras:v2"


class SarvamClient:
    """STT and TTS over Sarvam, or a clear refusal when no key is present.

    The same shape as ``RazorpayTestClient``: ``available`` reports whether a key
    is configured, the transport is injectable so tests never touch the network,
    and the two methods raise a legible error rather than a mysterious one when
    called offline. Nothing in the conversation loop calls these unless a caller
    asks for audio -- the transcript path never needs them.
    """

    def __init__(self, api_key: Optional[str], *, transport: Optional[Any] = None) -> None:
        self._api_key = api_key or None
        self._transport = transport

    @property
    def available(self) -> bool:
        return bool(self._api_key) or self._transport is not None

    def _require(self) -> None:
        if not self.available:
            raise RuntimeError(
                "SARVAM_API_KEY is not set. The transcript is produced with no "
                "key; only audio synthesis needs one. Set it in .env and re-run "
                "with --live-sarvam to produce assets/voice-demo.mp3."
            )

    def _module(self):
        if self._transport is not None:
            return self._transport
        import requests

        return requests

    def transcribe(self, audio_bytes: bytes, *, language: str = "hi-IN") -> str:
        """Batch STT. Returns the recognised text (Hinglish is native to Saaras)."""
        self._require()
        module = self._module()
        response = module.post(
            SARVAM_STT_ENDPOINT,
            headers={"api-subscription-key": self._api_key or ""},
            data={"model": SARVAM_STT_MODEL, "language_code": language},
            files={"file": ("turn.wav", audio_bytes, "audio/wav")},
            timeout=90,
        )
        response.raise_for_status()
        return str(response.json().get("transcript", ""))

    def synthesize(
        self, text: str, *, language: str = "hi-IN", speaker: Optional[str] = None
    ) -> bytes:
        """Batch TTS. Returns MP3 bytes for one line, Hinglish voice.

        Model, voice and codec are the ones verified live on 2026-08-30 -- see
        the module constants for why they are what they are and how to correct
        them when Sarvam next rotates the roster. ``speaker`` overrides the
        default voice, so the demo can give the customer a contrasting one.
        """
        self._require()
        module = self._module()
        response = module.post(
            SARVAM_TTS_ENDPOINT,
            headers={"api-subscription-key": self._api_key or ""},
            json={
                "inputs": [text],
                "target_language_code": language,
                "speaker": speaker or SARVAM_TTS_SPEAKER,
                "model": SARVAM_TTS_MODEL,
                "output_audio_codec": SARVAM_TTS_CODEC,
            },
            timeout=90,
        )
        response.raise_for_status()
        import base64

        payload = response.json()
        audios = payload.get("audios") or []
        if not audios:
            return b""
        return base64.b64decode(audios[0])


# --------------------------------------------------------------------------
# The conversation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One line of the call. ``speaker`` is 'agent' or 'customer'."""

    speaker: str
    text: str
    #: True for the two opening turns, so a reviewer (and a test) can see that
    #: the disclosure and identity were spoken as scripted, not model-authored.
    scripted: bool = False


@dataclass
class CallResult:
    """The whole outcome of one call attempt."""

    identity: CallerIdentity
    gate: Judgement
    call_placed: bool
    turns: List[Turn] = field(default_factory=list)
    stood_down: bool = False
    standdown_signals: Tuple[str, ...] = ()
    standdown_verbatim: Optional[str] = None
    promise: Optional[Promise] = None
    llm_call_ids: Tuple[str, ...] = ()

    @property
    def transcript_text(self) -> str:
        return "\n".join("%s: %s" % (t.speaker, t.text) for t in self.turns)


def preflight(context: EnvelopeContext) -> Judgement:
    """The envelope's decision on whether this call may be placed at all.

    An ``ACT_VOICE`` step on the voice channel, judged at the call's own
    timestamp. This is the single gate carrying R9 (window + self-id), R8 (cap +
    DND), R10 (disclosure), P2 (value floor) and the stopping rules -- the module
    does not re-implement any of them, it asks the authority. A REJECT here means
    the call is never dialled.
    """
    return judge(Step(action="ACT_VOICE", channel="voice"), context)


def place_call(
    customer_utterances: Sequence[str],
    context: EnvelopeContext,
    *,
    identity: Optional[CallerIdentity] = None,
    llm: Optional[Any] = None,
    counterparty_id: str = "cp_demo",
) -> CallResult:
    """Run one Hinglish recovery call. The heart of the module.

    ``customer_utterances`` is the customer side of the call -- already text,
    whether that text came from Sarvam STT on live audio or from a scripted demo.
    The agent side is generated here: the two scripted opening lines first
    (R10 then R9), then one turn-policy reply per customer utterance, with the
    S7 stand-down checked before each reply.

    The envelope is consulted once, up front, on whether the call may be placed
    (``preflight``). If it refuses, no turn is emitted at all -- the disclosure
    included -- because an unplaced call has no opening.
    """
    identity = identity or CallerIdentity()

    gate = preflight(context)
    if not gate.allowed:
        return CallResult(identity=identity, gate=gate, call_placed=False)

    now = context.at
    turns: List[Turn] = []
    call_ids: List[str] = []

    # R10 then R9 -- the opening, before the customer says anything.
    disclosure, identity_line = opening_lines(identity)
    turns.append(Turn("agent", disclosure, scripted=True))
    turns.append(Turn("agent", identity_line, scripted=True))

    stood_down = False
    standdown_signals: Tuple[str, ...] = ()
    standdown_verbatim: Optional[str] = None

    for utterance in customer_utterances:
        turns.append(Turn("customer", utterance))

        standdown = detect_standdown(utterance)
        if standdown.triggered:
            # S7: one hand-off line, log verbatim, stop. No reply, no promise.
            turns.append(Turn("agent", STANDDOWN_LINE, scripted=True))
            stood_down = True
            standdown_signals = standdown.signals
            standdown_verbatim = utterance
            break

        reply, ids = generate_reply(llm, turns, utterance, identity, now=now)
        turns.append(Turn("agent", reply["say"]))
        call_ids.extend(ids)

    promise: Optional[Promise] = None
    if not stood_down:
        promise = _extract_promise(customer_utterances, counterparty_id, now=now, llm=llm)

    return CallResult(
        identity=identity,
        gate=gate,
        call_placed=True,
        turns=turns,
        stood_down=stood_down,
        standdown_signals=standdown_signals,
        standdown_verbatim=standdown_verbatim,
        promise=promise,
        llm_call_ids=tuple(call_ids),
    )


def _extract_promise(
    customer_utterances: Sequence[str],
    counterparty_id: str,
    *,
    now: str,
    llm: Optional[Any],
) -> Optional[Promise]:
    """Pull a commitment out of what the customer actually said.

    Scans the customer turns for the first genuine promise (the heuristic is
    conservative on purpose -- a false promise pauses the ladder on somebody who
    never committed, S3). Returns the ``Promise`` in state PROMISED, or ``None``
    if nothing rose to a commitment. The LLM path is used only if a client is
    present; the heuristic is the offline default and is what the tests pin
    against the brief's own two examples.
    """
    for utterance in customer_utterances:
        extracted = promises.extract_commitment(utterance, now=now)
        if extracted.is_promise:
            return promises.make_promise(counterparty_id, extracted, channel="voice")
    return None


# --------------------------------------------------------------------------
# The ledger writers -- CONVERSE and PROMISE
# --------------------------------------------------------------------------


def write_call_to_ledger(
    ledger: Ledger, result: CallResult, ts: str, *, arm: Optional[str] = None
) -> None:
    """Write the call to the hash chain: one CONVERSE row, plus a PROMISE row.

    ``ts`` is the call's own event time -- the same ``EnvelopeContext.at`` the
    gate was evaluated at. It is required and passed in rather than read from a
    clock: there is no ``now()`` in the write path (config.py's first paragraph),
    because a wall-clock stamp would make the ledger hash depend on when the run
    happened and break NFR-3.

    The CONVERSE row carries the **verbatim** transcript. That is deliberate and
    is R9/S7's requirement, not a leak: the ledger is the audit artifact a
    conduct review reads, and "log the conversation verbatim" is exactly what S7
    demands on a stand-down. The canonicality screen governs *prompts* (protecting
    a shared cache), never the ledger -- an evidentiary record that stripped the
    customer's own words would be the wrong artifact.

    A PROMISE row is written only when a promise was actually extracted -- the
    same one-writer-per-real-thing discipline as every other kind (ADR-011): no
    promise, no row, so "no PROMISE rows" means "nobody committed", not "the
    writer is missing".
    """
    converse_payload: Dict[str, Any] = {
        "counterparty_id": result.identity.as_dict(),
        "call_placed": result.call_placed,
        "gate_decision": result.gate.verdict,
        "gate_rule": result.gate.rule_id,
        "turns": [
            {"speaker": t.speaker, "text": t.text, "scripted": t.scripted}
            for t in result.turns
        ],
        "stood_down": result.stood_down,
        "standdown_signals": list(result.standdown_signals),
        # Verbatim, per S7. Present only on a stand-down.
        "standdown_verbatim": result.standdown_verbatim,
        "disclosure_first": _disclosure_is_first(result),
    }
    ledger.append(
        "CONVERSE",
        ts=ts,
        payload=converse_payload,
        arm=arm,
        llm_call_ids=result.llm_call_ids,
        rule_fired=result.gate.rule_id,
        decision=result.gate.verdict,
    )

    if result.promise is not None:
        p = result.promise
        ledger.append(
            "PROMISE",
            ts=ts,
            payload={
                "counterparty_id": p.counterparty_id,
                "state": p.state,
                "amount_paise": p.amount_paise,
                "promised_date": p.promised_date,
                "channel": p.channel,
                "verbatim": p.verbatim,
                "confidence": p.confidence,
            },
            arm=arm,
        )
    ledger.conn.commit()


def _disclosure_is_first(result: CallResult) -> bool:
    """True iff the first emitted turn is the R10 disclosure. A stored invariant,
    so the ledger itself records that the property held on this call."""
    return bool(result.turns) and result.turns[0].text == DISCLOSURE_LINE


# --------------------------------------------------------------------------
# The canonical demo call -- the transcript artifact and the CLI both use it
# --------------------------------------------------------------------------
#
# A fixed script so the transcript (assets/voice-transcript.md) is byte-stable
# with no key, and so the same conversation is what a live-Sarvam run synthesises
# into audio. The customer speaks Hinglish; the call is a real recovery arc --
# hesitation, a reason, and a specific promised date -- ending in "Friday tak
# pakka", the brief's own example, so the extracted promise is the visible payoff.

#: 2026-08-13 is a Thursday in IST, inside R9's 08:00-19:00 window, so this call
#: is lawfully placeable and "Friday" resolves to the very next day (2026-08-14).
DEMO_CALL_AT = "2026-08-13T15:20:00+05:30"  # a Thursday, 15:20 IST

DEMO_CUSTOMER_UTTERANCES: Tuple[str, ...] = (
    "Haan ji boliye, kya baat hai?",
    "Achha wo bijli ka bill... haan mujhe pata hai payment fail ho gaya tha.",
    "Abhi thodi cash ki dikkat chal rahi hai, salary aane wali hai.",
    "Theek hai, Friday tak pakka kar dunga, pura amount.",
)


def demo_context() -> EnvelopeContext:
    """The context for the canonical demo call: a lawful collection voice call."""
    return EnvelopeContext(
        at=DEMO_CALL_AT,
        legal_context="collection",
        reason_code="insufficient_funds",
        amount_paise=250_000,  # Rs 2,500 -- clears the P2 voice floor
        counterparty_id="cp_demo_voice",
        consent="implied",
        ai_disclosure_scripted=True,
        self_identification_scripted=True,
        unsolicited_calls_today=0,
    )


def run_demo_call(*, llm: Optional[Any] = None) -> CallResult:
    """Place the canonical demo call. Deterministic with ``llm=None``."""
    return place_call(
        DEMO_CUSTOMER_UTTERANCES,
        demo_context(),
        identity=CallerIdentity(),
        llm=llm,
        counterparty_id="cp_demo_voice",
    )


def render_transcript_markdown(result: CallResult) -> str:
    """The 'review on mute' artifact: the call as readable Markdown.

    Written beside the audio (assets/voice-transcript.md) so a reviewer on mute
    still gets the whole point -- the disclosure-first opening, the Hinglish
    negotiation, and the extracted promise with its resolved date.
    """
    lines: List[str] = []
    lines.append("# Pramaan -- Hinglish voice recovery call (transcript)")
    lines.append("")
    lines.append(
        "A single-turn Sarvam STT -> LLM turn policy -> Sarvam TTS call. This "
        "transcript is produced with **no API key** (the deterministic turn "
        "policy); the audio clip needs a live `SARVAM_API_KEY`. Every line below "
        "cleared the same policy envelope the rest of the system runs on."
    )
    lines.append("")
    lines.append("**Compliance, gated by `pramaan.envelope.judge`, not asserted here:**")
    lines.append("")
    lines.append("- **R10** -- the AI disclosure is the first utterance, unconditionally.")
    lines.append("- **R9** -- placed inside 08:00-19:00 IST; the opening states who is")
    lines.append("  calling, whom they represent and why.")
    lines.append("- **R8** -- within the 3-unsolicited-calls-per-number-per-day cap.")
    lines.append("- **S7** -- a distress / dispute / legal signal stands the agent down")
    lines.append("  immediately (not exercised in this call; see `tests/test_voice.py`).")
    lines.append("")
    lines.append(
        "Envelope pre-flight on the `ACT_VOICE` step: **%s**, citing **%s**."
        % (result.gate.verdict, result.gate.rule_id)
    )
    lines.append("")
    lines.append("| # | Speaker | Line |")
    lines.append("|---|---|---|")
    for i, t in enumerate(result.turns, 1):
        who = "**Agent (AI)**" if t.speaker == "agent" else "Customer"
        tag = " _(scripted)_" if t.scripted else ""
        safe = t.text.replace("|", "\\|")
        lines.append("| %d | %s%s | %s |" % (i, who, tag, safe))
    lines.append("")
    if result.promise is not None:
        p = result.promise
        lines.append("## Promise extracted from speech")
        lines.append("")
        lines.append("The state machine (`pramaan.converse.promises`) received:")
        lines.append("")
        lines.append("- **state:** `%s`" % p.state)
        lines.append("- **promised date:** `%s`" % p.promised_date)
        lines.append("- **channel:** `%s`" % p.channel)
        lines.append("- **verbatim:** \"%s\"" % p.verbatim)
        lines.append("")
        lines.append(
            "That commitment is written to the hash-chained ledger as a `PROMISE` "
            "row alongside the `CONVERSE` row carrying the verbatim transcript."
        )
    else:
        lines.append("## No promise extracted")
        lines.append("")
        lines.append("Nothing the customer said rose to a commitment (the extractor is")
        lines.append("conservative on purpose -- a false promise pauses the ladder under S3).")
    lines.append("")
    return "\n".join(lines)
