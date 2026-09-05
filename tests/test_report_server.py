"""The demo server runs a fixed table of commands, and nothing else.

``pramaan/report/server.py`` starts subprocesses on behalf of a web page, which
is a thing worth being careful about, so its module docstring makes four
promises: the command set is a fixed table rather than a parameter, the socket
binds to loopback only, every run is forced offline, and a second concurrent run
is refused because two writers on one SQLite ledger is a corrupted ledger.

Those were four claims with no test behind them -- exactly the pattern this
project spends its time finding elsewhere. These are the tests.

Nothing here starts a server or a subprocess. The command table, the run-state
machine and the log classifier are all pure, and testing them directly is both
faster and stricter than driving them through HTTP: a socket test would prove
one request was refused, where reading the table proves *no* request can reach a
shell.
"""
from __future__ import annotations

import inspect
import sys

import pytest

from pramaan.report import server as S


# --------------------------------------------------------------------------
# The command table
# --------------------------------------------------------------------------


def test_every_run_is_an_argv_list_never_a_shell_string():
    """A string would mean ``shell=True`` somewhere, eventually.

    The whole safety argument rests on the command never being assembled from
    text, so this asserts the shape rather than trusting the call site.
    """
    for run_id, spec in S.RUNS.items():
        assert isinstance(spec["argv"], list), run_id
        assert all(isinstance(part, str) for part in spec["argv"]), run_id
        assert spec["argv"][0] == sys.executable, (
            "%s must run this interpreter, not whatever `python` resolves to" % run_id
        )


def test_no_run_is_assembled_from_request_input():
    """The handler looks an id up in ``RUNS`` and never interpolates it.

    Read the source of ``do_POST`` and assert the id is used as a dict key and
    a dict membership test only. A future edit that formats the id into a
    command -- the classic remote-code hole in a local demo server -- fails
    here rather than in the wild.
    """
    source = inspect.getsource(S.Handler.do_POST)
    assert "run_id in RUNS" in source or "run_id not in RUNS" in source
    for forbidden in ("shell=True", "os.system", "%s" % "{run_id}", "+ run_id"):
        assert forbidden not in source, "do_POST builds a command from input: %r" % forbidden


def test_the_subprocess_call_never_uses_a_shell():
    worker = inspect.getsource(S._worker)
    assert "shell=True" not in worker
    assert "shell" not in worker.replace("shell=False", "")


def test_every_run_forces_offline_mode():
    """No button may spend a token. The env var is set in the worker, once."""
    worker = inspect.getsource(S._worker)
    assert 'env["PRAMAAN_LLM_OFFLINE"] = "1"' in worker


def test_the_server_binds_to_loopback_only():
    """Checked against the bind call, not the source text.

    The first version grepped the whole function and failed on its own comment,
    which says "127.0.0.1, never 0.0.0.0" -- a test that reads prose rather than
    code, and would equally have passed a function whose comment claimed
    loopback while the call bound everywhere. Comments are stripped first.
    """
    code = "\n".join(
        line for line in inspect.getsource(S.serve).splitlines()
        if not line.strip().startswith("#")
    )
    assert 'Server(("127.0.0.1", port)' in code
    assert "0.0.0.0" not in code


def test_each_run_declares_a_duration_and_what_it_writes():
    """Both are shown on the button. A wrong duration is how somebody starts an
    eight-minute bootstrap on camera."""
    for run_id, spec in S.RUNS.items():
        assert isinstance(spec["seconds"], int) and spec["seconds"] > 0, run_id
        assert spec["writes"], run_id
        assert spec["label"], run_id


def test_snapshot_arguments_name_paths_inside_the_project():
    """A run's follow-up snapshot may only point at build/ artifacts.

    The first version of this table left every entry on the snapshot's
    defaults, so ``demo-dev`` rebuilt a 200-event ledger and then
    re-aggregated the 6,000-event one -- a run that looked like it did nothing.
    Pinning the shape stops that returning, and stops an absolute path
    appearing in a command line.
    """
    for run_id, spec in S.RUNS.items():
        extra = spec.get("snapshot")
        if not extra:
            continue
        for part in extra:
            if part.startswith("--"):
                continue
            assert part.startswith("build/"), "%s: %r" % (run_id, part)
            assert ".." not in part, "%s: %r" % (run_id, part)


def test_only_one_run_reaches_a_third_party():
    """Exactly one button is allowed to leave the machine, and it says so.

    Everything else in this project is keyless by construction; the live Sarvam
    synthesis is the single exception and is flagged in the UI. If a second run
    ever becomes live, this fails and somebody has to decide that deliberately.
    """
    live = [k for k, v in S.RUNS.items() if v.get("live")]
    assert live == ["voice-live"], live


# --------------------------------------------------------------------------
# The run-state machine
# --------------------------------------------------------------------------


def test_a_second_run_is_refused_while_one_is_in_flight():
    """Two writers on one SQLite ledger is a corrupted ledger, so the second
    caller is refused rather than queued."""
    state = S.RunState()
    assert state.begin("demo-dev") is True
    assert state.begin("demo-full") is False, "a concurrent run was allowed"
    state.end(0)
    assert state.begin("demo-full") is True


def test_beginning_a_run_clears_the_previous_output():
    state = S.RunState()
    state.begin("demo-dev")
    state.append("stale line")
    state.end(0)
    state.begin("demo-full")
    assert state.snapshot(0)["lines"] == []
    assert state.snapshot(0)["status"] == "running"


def test_a_nonzero_exit_is_reported_as_failed():
    """`SOME CHECKS FAILED` exits 1, and the page must show that rather than a
    green badge over a failed run."""
    state = S.RunState()
    state.begin("demo-dev")
    state.end(1)
    assert state.snapshot(0)["status"] == "failed"
    assert state.snapshot(0)["returncode"] == 1


def test_the_log_is_served_incrementally():
    """The browser polls with a cursor; re-sending the whole log every 400 ms
    would grow quadratically over a 400-line run."""
    state = S.RunState()
    state.begin("demo-dev")
    for i in range(5):
        state.append("line %d" % i)
    first = state.snapshot(0)
    assert len(first["lines"]) == 5 and first["total"] == 5
    state.append("line 5")
    later = state.snapshot(first["total"])
    assert [l["text"] for l in later["lines"]] == ["line 5"]


def test_elapsed_stops_moving_once_the_run_ends():
    state = S.RunState()
    state.begin("demo-dev")
    state.end(0)
    first = state.snapshot(0)["elapsed"]
    assert first is not None
    assert state.snapshot(0)["elapsed"] == first


# --------------------------------------------------------------------------
# The log classifier
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,kind",
    [
        ("  ALLOW                 3,925", "envelope"),
        ("  REJECT                1,140", "envelope"),
        ("  verify_chain         PASS", "ledger"),
        ("  C - A (does it recover money) +10.43 pp", "measure"),
        ("  ALL CHECKS PASS", "done"),
        ("  SOME CHECKS FAILED", "fail"),
        ("THE HEADLINE, TWO WAYS", "measure"),
    ],
)
def test_milestone_lines_are_classified(line, kind):
    assert S._classify(line) == kind


def test_prose_is_not_classified_as_a_milestone():
    """Anchored to the start of the line, not matched anywhere in it.

    The first version substring-matched, so the title line was tagged as a
    measurement because it contains the word "incremental", and a paragraph of
    explanation was tagged as a ledger event because it contains "head". Colour
    that lands on prose teaches the eye that the colour means nothing.
    """
    prose = [
        "Pramaan -- revenue recovery, Day 3: the incremental number",
        "   but a truncated tail leaves every surviving row and link correct --",
        "  (note the asymmetry: re-hashing catches a *mutated* row on its own,",
    ]
    for line in prose:
        assert S._classify(line) is None, line


def test_blank_lines_classify_as_nothing():
    assert S._classify("") is None
    assert S._classify("    ") is None


# --------------------------------------------------------------------------
# The live call
# --------------------------------------------------------------------------


def test_the_live_call_refuses_without_a_sarvam_key(monkeypatch):
    """The one endpoint that cannot be keyless says so, rather than failing
    somewhere deeper with a less obvious message."""
    from pramaan.config import Config

    monkeypatch.setattr(
        "pramaan.config.load_config",
        lambda: Config(seed=42, mode="shadow", llm_offline=True, sarvam_api_key=None),
    )
    S.CALL.reset()
    with pytest.raises(RuntimeError, match="SARVAM_API_KEY"):
        S._live_call_turn(b"not really audio")


def test_resetting_the_call_clears_the_transcript():
    S.CALL.turns.append({"speaker": "customer", "text": "leftover"})
    S.CALL.started = True
    S.CALL.reset()
    assert S.CALL.turns == []
    assert S.CALL.started is False
    assert S.CALL.promise is None


def test_the_agent_opens_without_a_key_is_refused(monkeypatch):
    """Opening a call is the same keyed path as taking a turn, and says so."""
    from pramaan.config import Config

    monkeypatch.setattr(
        "pramaan.config.load_config",
        lambda: Config(seed=42, mode="shadow", llm_offline=True, sarvam_api_key=None),
    )
    S.CALL.reset()
    with pytest.raises(RuntimeError, match="SARVAM_API_KEY"):
        S._live_call_open()


def test_the_first_utterance_of_a_live_call_is_the_disclosure(monkeypatch):
    """R10's requirement is ordering, not presence.

    A transcript that discloses the AI in turn four satisfies "the disclosure
    appears" and violates the rule. ``_live_call_open`` exists so the agent
    speaks before the human does, and this reads index 0 to confirm what it
    said. TTS is stubbed: the assertion is about sequence, not audio.
    """
    from pramaan.config import Config
    from pramaan.converse import voice

    monkeypatch.setattr(
        "pramaan.config.load_config",
        lambda: Config(seed=42, mode="shadow", llm_offline=True,
                       sarvam_api_key="test-key-not-used"),
    )
    monkeypatch.setattr(voice.SarvamClient, "synthesize",
                        lambda self, text: b"\xff\xfb" + text.encode("utf-8")[:4])
    S.CALL.reset()
    opened = S._live_call_open()

    assert [t["speaker"] for t in opened["turns"]] == ["agent", "agent"], (
        "the human has not spoken yet -- that is the whole point"
    )
    assert opened["turns"][0]["text"] == voice.DISCLOSURE_LINE
    assert opened["ruling"]["verdict"] != "REJECT"
    assert S.CALL.started is True, (
        "so the first human turn does not replay the opening on top of itself"
    )
    S.CALL.reset()
