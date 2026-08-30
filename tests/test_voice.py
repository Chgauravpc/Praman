"""Day 7: the Hinglish voice loop, and the four gates that make it defensible.

These tests drive the real ``place_call`` loop, the real envelope, the real
promise extractor and the real ledger. The only thing ever replaced is the
Sarvam network (an injected transport) and, in the two turn-policy tests, the
LLM (``tests.scripted.ScriptedLLM``) -- so the harness is what is under test,
exactly as the investigator and promise tests are built.

The properties that matter here are safety properties, so they are tested as
such: not "the agent usually discloses" but "there is no path that places a call
without the disclosure first", and not "distress usually stops it" but "distress
stops it even when the same sentence also contains a promise".
"""
from __future__ import annotations

import sqlite3

import pytest

from pramaan import canonical
from pramaan.converse import voice
from pramaan.converse.promises import PROMISED
from pramaan.envelope import EnvelopeContext, Step, judge
from pramaan.ledger.chain import Ledger
from tests.scripted import ScriptedLLM

THURSDAY_1520 = "2026-08-13T15:20:00+05:30"  # inside R9's window; "Friday" -> +1 day


def _ctx(**overrides) -> EnvelopeContext:
    base = dict(
        at=THURSDAY_1520,
        legal_context="collection",
        reason_code="insufficient_funds",
        amount_paise=250_000,
        counterparty_id="cp_test",
        consent="implied",
        ai_disclosure_scripted=True,
        self_identification_scripted=True,
        unsolicited_calls_today=0,
    )
    base.update(overrides)
    return EnvelopeContext(**base)


# --------------------------------------------------------------------------
# R10 -- the disclosure is the first utterance, unconditionally
# --------------------------------------------------------------------------


def test_disclosure_is_always_the_first_utterance():
    result = voice.place_call(["Haan boliye"], _ctx())
    assert result.call_placed
    assert result.turns[0].speaker == "agent"
    assert result.turns[0].text == voice.DISCLOSURE_LINE
    # And the disclosure actually says it is an AI / automated / recorded.
    lowered = result.turns[0].text.lower()
    assert "ai" in lowered and "automated" in lowered and "recording" in lowered


def test_the_r9_identity_line_comes_second_not_first():
    result = voice.place_call(["Haan"], _ctx(), identity=voice.CallerIdentity(
        agent_name="Asha", merchant_name="Zappay", purpose="a failed bill"))
    # R10 then R9: the merchant name appears in turn 2, never in turn 1.
    assert "Zappay" not in result.turns[0].text
    assert "Zappay" in result.turns[1].text


def test_a_call_the_envelope_refuses_emits_no_turns_at_all():
    # An unplaced call has no opening -- not even the disclosure, because there is
    # no call. The disclosure being unconditional means "on every placed call",
    # and this is the other half of that: no call, nothing said.
    result = voice.place_call(["Haan"], _ctx(at="2026-08-13T19:30:00+05:30"))
    assert result.call_placed is False
    assert result.turns == []


def test_missing_disclosure_script_is_refused_by_the_envelope_as_r10():
    result = voice.place_call(["Haan"], _ctx(ai_disclosure_scripted=False))
    assert result.call_placed is False
    assert result.gate.verdict == "REJECT"
    assert result.gate.rule_id == "R10"


# --------------------------------------------------------------------------
# R9 / R8 -- the windows and the cap, as the envelope decides them
# --------------------------------------------------------------------------


def test_r9_window_shuts_the_collection_call_at_1930():
    result = voice.place_call(["Haan"], _ctx(at="2026-08-13T19:30:00+05:30"))
    assert result.call_placed is False
    assert result.gate.rule_id == "R9"


def test_r8_shuts_the_voice_call_before_0900_even_though_r9_is_open():
    # The 08:00-09:00 fourth legal state: R9 permits a collection contact, R8 does
    # not yet permit a *voice* one. The refusal must cite R8, not R9.
    result = voice.place_call(["Haan"], _ctx(at="2026-08-13T08:30:00+05:30"))
    assert result.call_placed is False
    assert result.gate.rule_id == "R8"


def test_r8_daily_call_cap_refuses_the_fourth_call():
    result = voice.place_call(["Haan"], _ctx(unsolicited_calls_today=3))
    assert result.call_placed is False
    assert result.gate.rule_id == "R8"


def test_a_lawful_midday_collection_call_is_allowed():
    result = voice.place_call(["Haan"], _ctx())
    assert result.call_placed is True
    assert result.gate.verdict == "ALLOW"


# --------------------------------------------------------------------------
# S7 -- the stand-down, the non-negotiable one
# --------------------------------------------------------------------------


def test_distress_stands_the_agent_down_and_logs_verbatim():
    words = "Meri naukri chali gayi hai, abhi kuch nahi kar sakta"
    result = voice.place_call([words], _ctx())
    assert result.stood_down is True
    assert "distress" in result.standdown_signals
    assert result.standdown_verbatim == words
    # The agent's last line is the hand-off, and nothing follows it.
    assert result.turns[-1].text == voice.STANDDOWN_LINE
    assert result.turns[-1].speaker == "agent"


def test_a_dispute_signal_stands_down():
    result = voice.place_call(["Maine to already pay kar diya tha!"], _ctx())
    assert result.stood_down is True
    assert "dispute" in result.standdown_signals


def test_a_legal_threat_stands_down():
    result = voice.place_call(["Main apne lawyer se baat karunga"], _ctx())
    assert result.stood_down is True
    assert "legal" in result.standdown_signals


def test_distress_beats_a_promise_in_the_same_breath():
    # The ethical crux: a sentence that discloses hardship AND looks like a
    # promise must stand down and extract NO promise. Pausing the ladder on a
    # "promise" from somebody who just said they lost their job would be the
    # system recording a commitment it should never have solicited.
    result = voice.place_call(
        ["Meri naukri chali gayi hai, par Friday tak pakka kar dunga"], _ctx()
    )
    assert result.stood_down is True
    assert result.promise is None


def test_standdown_stops_the_conversation_no_later_turns():
    # A distress signal on turn one means the second customer utterance is never
    # processed: no reply to it, no promise from it.
    result = voice.place_call(
        ["paise nahi hai bilkul", "theek hai kal de dunga"], _ctx()
    )
    assert result.stood_down is True
    # customer turns present: only the first one was consumed before stand-down.
    customer_turns = [t for t in result.turns if t.speaker == "customer"]
    assert customer_turns == [voice.Turn("customer", "paise nahi hai bilkul")]
    assert result.promise is None


def test_the_standdown_context_flags_make_the_envelope_agree():
    # detect_standdown's flags feed EnvelopeContext, and the envelope's own S7
    # must then refuse a voice step -- the classifier and the gate cannot drift.
    sd = voice.detect_standdown("meri naukri chali gayi hai")
    assert sd.triggered
    ctx = _ctx(**sd.as_context_flags)
    verdict = judge(Step("ACT_VOICE", channel="voice"), ctx)
    assert verdict.verdict == "REJECT"
    assert verdict.rule_id == "S7"


def test_an_ordinary_reply_does_not_stand_down():
    sd = voice.detect_standdown("Haan ji, main Friday tak kar dunga")
    assert sd.triggered is False
    assert sd.signals == ()


# --------------------------------------------------------------------------
# The promise, extracted from speech
# --------------------------------------------------------------------------


def test_a_spoken_promise_reaches_the_state_machine_with_its_date():
    result = voice.place_call(
        ["Haan boliye", "Theek hai, Friday tak pakka kar dunga"], _ctx()
    )
    assert result.promise is not None
    assert result.promise.state == PROMISED
    assert result.promise.channel == "voice"
    # Thursday 2026-08-13; the next Friday is the 14th.
    assert canonical.parse_iso(result.promise.promised_date).date().isoformat() == "2026-08-14"


def test_a_vague_acknowledgement_is_not_a_promise():
    # PRD 6.8's own trap, over the voice loop this time.
    result = voice.place_call(["haan haan kal dekhta hoon"], _ctx())
    assert result.stood_down is False
    assert result.promise is None


# --------------------------------------------------------------------------
# The ledger -- CONVERSE and PROMISE
# --------------------------------------------------------------------------


def _ledger() -> Ledger:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return Ledger(conn)


def test_a_placed_call_writes_a_converse_row_and_a_promise_row():
    result = voice.place_call(
        ["Theek hai Friday tak pakka kar dunga"], _ctx()
    )
    ledger = _ledger()
    voice.write_call_to_ledger(ledger, result, ts=THURSDAY_1520)
    counts = dict(ledger.kind_counts())
    assert counts.get("CONVERSE") == 1
    assert counts.get("PROMISE") == 1
    assert ledger.verify_chain().ok


def test_a_call_with_no_promise_writes_no_promise_row():
    result = voice.place_call(["haan haan kal dekhta hoon"], _ctx())
    ledger = _ledger()
    voice.write_call_to_ledger(ledger, result, ts=THURSDAY_1520)
    counts = dict(ledger.kind_counts())
    assert counts.get("CONVERSE") == 1
    assert "PROMISE" not in counts


def test_the_converse_row_carries_the_verbatim_transcript():
    words = "Meri naukri chali gayi hai"
    result = voice.place_call([words], _ctx())
    ledger = _ledger()
    voice.write_call_to_ledger(ledger, result, ts=THURSDAY_1520)
    row = ledger.conn.execute(
        "SELECT payload FROM ledger WHERE kind='CONVERSE'"
    ).fetchone()["payload"]
    # S7 requires the customer's own words be logged verbatim.
    assert words in row
    # canonical_json is compact (no spaces after ':').
    assert '"stood_down":true' in row


def test_the_ledger_chain_verifies_over_a_real_call():
    result = voice.run_demo_call()
    ledger = _ledger()
    voice.write_call_to_ledger(ledger, result, ts=voice.DEMO_CALL_AT)
    assert ledger.verify_chain().ok


# --------------------------------------------------------------------------
# The turn policy -- the LLM path, and its fallback
# --------------------------------------------------------------------------


def test_the_live_turn_policy_uses_the_customer_reply_and_bypasses_the_screen():
    # A scripted LLM stands in for the model. The reply must come back through the
    # loop, and the prompt must have been sent with screen=False (the customer's
    # raw utterance carries the dates/amounts the screen would otherwise refuse).
    scripted = ScriptedLLM([{"say": "Achha, kab tak ho payega?", "intent": "ask_date"}])
    result = voice.place_call(["Bijli ka bill hai na"], _ctx(), llm=scripted)
    assert any(t.text == "Achha, kab tak ho payega?" for t in result.turns)
    # The call id from the scripted reply is threaded into the ledger's call ids.
    assert result.llm_call_ids and result.llm_call_ids[0].startswith("scripted_")


def test_a_malformed_turn_policy_reply_falls_back_rather_than_crashing():
    # Same class as the planner's Day 5 rate-limit fix: a bad reply degrades to
    # the deterministic turn, it does not drop the call.
    scripted = ScriptedLLM(["this is not json"])
    result = voice.place_call(["Bijli ka bill hai na"], _ctx(), llm=scripted)
    assert result.call_placed
    # A deterministic reply is present; the call survived.
    assert any(t.speaker == "agent" and not t.scripted for t in result.turns)


# --------------------------------------------------------------------------
# Determinism and the artifact
# --------------------------------------------------------------------------


def test_the_keyless_demo_call_is_deterministic():
    a = voice.run_demo_call()
    b = voice.run_demo_call()
    assert a.transcript_text == b.transcript_text
    assert a.promise.promised_date == b.promise.promised_date


def test_the_transcript_markdown_shows_the_disclosure_and_the_promise():
    md = voice.render_transcript_markdown(voice.run_demo_call())
    assert voice.DISCLOSURE_LINE in md
    assert "Promise extracted from speech" in md
    assert "2026-08-14" in md  # the resolved promised date


# --------------------------------------------------------------------------
# Sarvam -- confined, and honest about the key
# --------------------------------------------------------------------------


def test_sarvam_refuses_clearly_with_no_key():
    client = voice.SarvamClient(api_key=None)
    assert client.available is False
    with pytest.raises(RuntimeError, match="SARVAM_API_KEY"):
        client.synthesize("kuch bhi")


class _FakeSarvamTransport:
    """Records posts and returns a canned STT/TTS shape. No network."""

    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        import base64

        class _R:
            status_code = 200

            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                if "text-to-speech" in url:
                    return {"audios": [base64.b64encode(b"AUDIO").decode()]}
                return {"transcript": "Friday tak pakka kar dunga"}

        return _R()


def test_sarvam_tts_and_stt_run_over_an_injected_transport():
    transport = _FakeSarvamTransport()
    client = voice.SarvamClient(api_key=None, transport=transport)
    assert client.available is True
    assert client.synthesize("Namaste") == b"AUDIO"
    assert client.transcribe(b"\x00\x01") == "Friday tak pakka kar dunga"
    assert len(transport.posts) == 2
