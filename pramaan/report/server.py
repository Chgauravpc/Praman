"""The demo server. Runs the real CLI, streams its output, refreshes the snapshot.

DASHBOARD-PLAN.md ruled this out -- "no server, no write path from the UI" --
and that was the wrong call for the artifact that actually has to be
demonstrated. A read-only report answers "what were the numbers"; a five-minute
video has to answer "what does the thing *do*", and a page that cannot start
anything cannot answer it. So this exists, with one hard rule to keep the
original concern honest:

**It reimplements nothing.** Every run is ``subprocess`` on the same
``python -m pramaan.cli ...`` a reviewer would type, with
``PRAMAAN_LLM_OFFLINE=1`` set exactly as the Makefile sets it. The server parses
stdout and moves files; it does not plan, judge, resolve, or compute a number.
There is therefore still no second path into the envelope -- the button presses
the same key the terminal does.

Three further rules, because a local server that runs subprocesses is worth
being careful about:

**Bound to 127.0.0.1.** Never 0.0.0.0. Nothing here should be reachable from
another machine.

**The command set is a fixed table, not a parameter.** ``RUNS`` below maps a
short id to an exact argv list. The request supplies an id and nothing else, so
no request body can reach a shell, add a flag, or change a path. A demo server
that interpolated a query parameter into a command line would be a remote-code
hole on the machine of whoever ran it.

**One run at a time.** A second POST while a run is in flight is refused rather
than queued: two concurrent writers on the same SQLite file is a corrupted
ledger, and the ledger is the artifact.
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pramaan.config import BUILD_DIR, ROOT

#: The only commands this server can run, by id. Argv lists, never strings --
#: nothing is passed through a shell.
#:
#: Durations are measured on the development machine and shown in the UI so a
#: demo does not start a five-minute bootstrap by accident on camera.
#: ``snapshot`` is the argv appended after the run finishes, and it must name
#: the artifacts *that run* wrote. Getting this wrong is not cosmetic: the first
#: version left every entry on the snapshot's defaults, so clicking "demo-dev"
#: correctly rebuilt a 200-event ledger and then re-aggregated the 6,000-event
#: one -- a run that appeared to do nothing, because the tiles never moved.
RUNS: Dict[str, Dict[str, Any]] = {
    "demo-dev": {
        "argv": [sys.executable, "-u", "-m", "pramaan.cli", "demo", "--dev"],
        "label": "Sense → envelope → resolve, 200 events. Start here.",
        "seconds": 15,
        "writes": "build/pramaan-dev.db",
        "snapshot": ["--db", "build/pramaan-dev.db",
                     "--metrics", "build/execute-dev-metrics.json"],
    },
    "execute-dev": {
        "argv": [sys.executable, "-u", "-m", "pramaan.cli", "execute", "--dev"],
        "label": "Adds the planner: arm C wired, contrasts with intervals",
        "seconds": 45,
        "writes": "build/execute-dev-metrics.json",
        # The execute run's own DB holds PLAN rows and no OUTCOME rows, so the
        # ledger view stays on the demo batch while the headline comes from the
        # metrics this run just wrote.
        "snapshot": ["--db", "build/pramaan-dev.db",
                     "--metrics", "build/execute-dev-metrics.json"],
    },
    # Measured, not guessed. This entry said 90s until it was timed at just
    # over 300 -- the sensitivity sweep and the tamper probes dominate. An
    # estimate that is wrong by 3x is worse than none: it is the number someone
    # trusts when deciding whether to start a run on camera.
    "demo-full": {
        "argv": [sys.executable, "-u", "-m", "pramaan.cli", "demo", "--full"],
        "label": "The measurement batch: 6,000 events across all five types",
        "seconds": 310,
        "writes": "build/pramaan-full.db",
        "snapshot": ["--db", "build/pramaan-full.db",
                     "--metrics", "build/execute-full-metrics.json"],
    },
    "execute-full": {
        "argv": [sys.executable, "-u", "-m", "pramaan.cli", "execute", "--full"],
        "label": "The headline: 6,000 events + a 10,000-resample bootstrap",
        "seconds": 480,
        "writes": "build/execute-full-metrics.json",
        "snapshot": ["--db", "build/pramaan-full.db",
                     "--metrics", "build/execute-full-metrics.json"],
    },
    "voice": {
        "argv": [sys.executable, "-u", "-m", "pramaan.cli", "voice"],
        "label": "The Hinglish recovery call — envelope-gated, keyless",
        "seconds": 8,
        "writes": "build/voice.db",
        "snapshot": [],
    },
    # The one button that deliberately reaches a third-party API, and the only
    # place ``PRAMAAN_LLM_OFFLINE`` is irrelevant -- Sarvam is TTS, not an LLM.
    # It needs SARVAM_API_KEY and it spends money, so it is labelled as live and
    # nothing else on this page can trigger it by accident.
    "voice-live": {
        "argv": [sys.executable, "-u", "-m", "pramaan.cli", "voice", "--live-sarvam"],
        "label": "LIVE Sarvam TTS — re-synthesises assets/voice-demo.mp3",
        "seconds": 40,
        "writes": "assets/voice-demo.mp3",
        "live": True,
        "snapshot": [],
    },
    # The snapshot module's own defaults are the full batch, so this button is
    # also the fast way back to the headline view after a dev run -- five
    # seconds instead of re-running a four-minute batch to undo a demo.
    "snapshot": {
        "argv": [sys.executable, "-u", "-m", "pramaan.report.snapshot"],
        "label": "Back to the full batch — re-aggregates the committed ledger",
        "seconds": 5,
        "writes": "build/dashboard.json",
        "snapshot": None,
    },
}

#: Lines worth tinting in the log, so the phases of a run are findable while
#: several hundred lines scroll past.
#:
#: Anchored to the *start* of the stripped line rather than matched anywhere in
#: it, which the first version did -- and a loose substring match tinted the
#: title line as a measurement because it contains the word "incremental", and
#: tinted a paragraph of prose as a ledger event because it contains "head".
#: Noise tinted at random is worse than no tinting: it teaches the eye that the
#: colour means nothing.
MILESTONES: Tuple[Tuple[str, str], ...] = (
    ("ingested", "sense"),
    ("new events", "sense"),
    ("DETECT", "sense"),
    ("ALLOW", "envelope"),
    ("AMEND", "envelope"),
    ("REJECT", "envelope"),
    ("GATE", "envelope"),
    ("verify_chain", "ledger"),
    ("head hash", "ledger"),
    ("ledger rows", "ledger"),
    ("exported", "ledger"),
    ("wrote", "ledger"),
    ("rows", "ledger"),
    ("C - A", "measure"),
    ("C - B", "measure"),
    ("B - A", "measure"),
    ("arm A", "measure"),
    ("arm B", "measure"),
    ("arm C", "measure"),
    ("recovered", "measure"),
    ("ALL CHECKS PASS", "done"),
    ("SOME CHECKS FAILED", "fail"),
)

#: Section banners the CLI prints in capitals. Tinted as their own phase so the
#: log reads as a sequence of stages rather than a wall.
BANNERS: Dict[str, str] = {
    "THE HEADLINE, TWO WAYS": "measure",
    "THE HEADLINE": "measure",
    "LEDGER": "ledger",
    "RESULT": "done",
    "ENVELOPE": "envelope",
    "GATE": "envelope",
}


def _classify(line: str) -> Optional[str]:
    text = line.strip()
    if not text:
        return None
    for banner, kind in BANNERS.items():
        if text.startswith(banner):
            return kind
    for needle, kind in MILESTONES:
        if text.startswith(needle):
            return kind
    return None


class RunState:
    """One run's output, appended by a worker thread and polled by the browser.

    Polling rather than server-sent events: SSE in ``http.server`` means holding
    a thread per client and getting the flush semantics right, and a 400 ms poll
    of an in-memory list is indistinguishable on screen. Fewer moving parts in
    the thing whose job is to work on camera.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.lines: List[Dict[str, Any]] = []
        self.run_id: Optional[str] = None
        self.status = "idle"          # idle | running | done | failed
        self.returncode: Optional[int] = None
        self.started: Optional[float] = None
        self.finished: Optional[float] = None

    def snapshot(self, after: int) -> Dict[str, Any]:
        with self.lock:
            elapsed = None
            if self.started is not None:
                end = self.finished if self.finished is not None else time.time()
                elapsed = round(end - self.started, 1)
            return {
                "run_id": self.run_id,
                "status": self.status,
                "returncode": self.returncode,
                "elapsed": elapsed,
                "total": len(self.lines),
                "lines": self.lines[after:],
            }

    def begin(self, run_id: str) -> bool:
        with self.lock:
            if self.status == "running":
                return False
            self.lines = []
            self.run_id = run_id
            self.status = "running"
            self.returncode = None
            self.started = time.time()
            self.finished = None
            return True

    def append(self, text: str) -> None:
        with self.lock:
            self.lines.append({
                "n": len(self.lines),
                "text": text,
                "kind": _classify(text),
            })

    def end(self, returncode: int) -> None:
        with self.lock:
            self.returncode = returncode
            self.status = "done" if returncode == 0 else "failed"
            self.finished = time.time()


STATE = RunState()


def _worker(run_id: str) -> None:
    """Run one command, line by line, into STATE. Also refreshes the snapshot.

    The environment is set the way the Makefile sets it -- offline, keyless --
    so a button press cannot spend tokens or reach a provider. ``-u`` on the
    child's argv keeps its stdout unbuffered; without it nothing appears until
    the process exits, which on a 90-second batch looks exactly like a hang.
    """
    spec = RUNS[run_id]
    env = dict(os.environ)
    env["PRAMAAN_LLM_OFFLINE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    STATE.append("$ " + " ".join(["python", "-u", "-m"] + spec["argv"][3:]))
    try:
        process = subprocess.Popen(
            spec["argv"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as error:
        STATE.append("failed to start: %s" % error)
        STATE.end(-1)
        return

    assert process.stdout is not None
    for raw in process.stdout:
        STATE.append(raw.rstrip("\n"))
    returncode = process.wait()

    # A run that changed the ledger leaves the dashboard stale, so refresh it
    # here rather than making the operator remember a second button.
    extra = spec.get("snapshot")
    if returncode == 0 and extra is not None:
        argv = RUNS["snapshot"]["argv"] + list(extra)
        STATE.append("")
        STATE.append("$ python -m pramaan.report.snapshot " + " ".join(extra))
        refresh = subprocess.run(
            argv, cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in (refresh.stdout or "").splitlines():
            STATE.append(line)
        if refresh.returncode != 0:
            returncode = refresh.returncode

    STATE.end(returncode)


# --------------------------------------------------------------------------
# Live conversation -- a person talks, the agent answers
# --------------------------------------------------------------------------
#
# ``make voice`` renders a *scripted* dialogue: both sides are fixed text
# (``DEMO_CUSTOMER_UTTERANCES`` and ``_fallback_reply``'s four keyword-selected
# lines), turned into audio by real Sarvam TTS. That is a reproducible artifact
# and it is honest about what it is, but it is not a conversation -- nobody is
# talking to anything.
#
# This endpoint is. One turn is:
#
#     browser mic -> WAV -> Sarvam STT -> the real turn-policy LLM
#                        -> Sarvam TTS -> audio back to the browser
#
# Every leg is the same function the rest of the project uses:
# ``SarvamClient.transcribe`` / ``.synthesize`` against the real endpoints, and
# ``voice.generate_reply`` with a live ``LLMClient`` -- so the agent's words are
# generated against the transcript so far, not selected from a list. It needs
# SARVAM_API_KEY and an LLM key, and it spends money per turn. That is the
# trade: this is the one path that cannot be keyless, because a real
# conversation cannot be cached before it happens.


class Conversation:
    """Server-side transcript for the live call. One at a time, like the runs."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.turns: List[Dict[str, str]] = []
        self.promise: Optional[Dict[str, Any]] = None
        self.ruling: Optional[Dict[str, Any]] = None
        self.started = False

    def reset(self) -> None:
        with self.lock:
            self.turns = []
            self.promise = None
            self.ruling = None
            self.started = False


CALL = Conversation()


def _live_call_turn(audio_wav: bytes) -> Dict[str, Any]:
    """One human turn in, one agent turn out. Raises on a missing key."""
    from pramaan.config import load_config
    from pramaan.converse import promises, voice
    from pramaan.envelope import EnvelopeContext
    from pramaan.llm.client import LLMClient

    config = load_config()
    sarvam = voice.SarvamClient(config.sarvam_api_key)
    if not sarvam.available:
        raise RuntimeError(
            "SARVAM_API_KEY is not set, so there is no speech-to-text to run. "
            "This endpoint is the one part of the project that cannot be keyless."
        )

    identity = voice.CallerIdentity()
    # Event time, not wall clock -- the same discipline the ledger is under, so
    # a promise extracted here lands on a comparable timeline.
    now = "2026-08-13T15:20:00+05:30"

    with CALL.lock:
        first_turn = not CALL.started
        transcript_snapshot = list(CALL.turns)

    ruling_payload = None
    if first_turn:
        # The envelope decides whether this call may happen at all, before a
        # single word is exchanged -- exactly as `preflight` does in the CLI.
        context = EnvelopeContext(
            at=now,
            legal_context="collection",
            reason_code="insufficient_funds",
            amount_paise=250_000,
            consent="explicit",
            ai_disclosure_scripted=True,
            self_identification_scripted=True,
        )
        judgement = voice.preflight(context)
        ruling_payload = {
            "verdict": judgement.verdict,
            "rule_id": judgement.rule_id,
            "reason": judgement.reason,
        }
        if judgement.verdict == "REJECT":
            raise RuntimeError(
                "the envelope refused this call: %s (%s)"
                % (judgement.reason, judgement.rule_id)
            )

    heard = sarvam.transcribe(audio_wav).strip()
    if not heard:
        raise RuntimeError("Sarvam returned an empty transcript for that audio")

    # The agent's reply, generated against the transcript so far by the real
    # turn-policy prompt. `llm=None` here would silently drop to the canned
    # fallback, which is the exact thing this endpoint exists not to do.
    llm = LLMClient(config)
    turns = [voice.Turn(speaker=t["speaker"], text=t["text"], scripted=False)
             for t in transcript_snapshot]
    turns.append(voice.Turn(speaker="customer", text=heard, scripted=False))
    reply, call_ids = voice.generate_reply(llm, turns, heard, identity, now=now)

    # Promise extraction, LLM first.
    #
    # Sarvam STT returns Devanagari and no documented parameter romanises it,
    # while ``extract_commitment``'s cues are Latin-script Hinglish ("pakka",
    # "tak", "friday") -- so the deterministic pass returns is_promise=False on
    # everything this endpoint hears. Verified, not assumed: the demo line round
    # -tripped through TTS and STT comes back as Devanagari and the regex misses
    # it. The LLM extractor is script-agnostic, so it leads here and the
    # deterministic one is the fallback, which is the reverse of the scripted
    # path's ordering and deliberately so.
    extracted = None
    extraction_via = "llm"
    try:
        extracted = promises.extract_commitment_via_llm(llm, heard, now=now)
    except Exception:  # noqa: BLE001 - a failed extraction must not drop the turn
        extracted = promises.extract_commitment(heard, now=now)
        extraction_via = "deterministic (LLM extraction failed)"

    promise_payload = None
    if extracted is not None and extracted.is_promise:
        promise_payload = {
            "verbatim": heard,
            "promised_date": extracted.promised_date,
            "confidence": extracted.confidence,
            "state": "promised",
            "extracted_by": extraction_via,
        }

    said = reply["say"]
    spoken = sarvam.synthesize(said)

    with CALL.lock:
        if first_turn:
            # R10: the disclosure is the first thing said on the call, always.
            for line in voice.opening_lines(identity):
                CALL.turns.append({"speaker": "agent", "text": line})
            CALL.ruling = ruling_payload
            CALL.started = True
        CALL.turns.append({"speaker": "customer", "text": heard})
        CALL.turns.append({"speaker": "agent", "text": said})
        if promise_payload:
            CALL.promise = promise_payload
        snapshot = list(CALL.turns)
        ruling = CALL.ruling
        promise = CALL.promise

    return {
        "heard": heard,
        "said": said,
        "intent": reply.get("intent"),
        "llm_call_ids": list(call_ids),
        "from_llm": bool(call_ids),
        "audio_b64": base64.b64encode(spoken).decode("ascii") if spoken else None,
        "turns": snapshot,
        "ruling": ruling,
        "promise": promise,
        "opening": voice.opening_lines(identity) if first_turn else None,
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files from the repo root, plus four JSON endpoints under /api/."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        """Quiet by default: the interesting log is the run's, not the server's."""
        if os.environ.get("PRAMAAN_SERVER_VERBOSE"):
            super().log_message(fmt, *args)

    def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Every artifact here is regenerated in place, so a cached copy is a
        # stale copy -- and a stale dashboard on camera is the failure mode.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        if self.path.startswith("/build/") or self.path.startswith("/dashboard/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.startswith("/api/runs"):
            self._json({
                "runs": [
                    {"id": key, "label": spec["label"],
                     "seconds": spec["seconds"], "writes": spec["writes"],
                     "live": bool(spec.get("live"))}
                    for key, spec in RUNS.items()
                ]
            })
            return
        if self.path.startswith("/api/log"):
            after = 0
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                for part in query.split("&"):
                    if part.startswith("after="):
                        try:
                            after = max(0, int(part[6:]))
                        except ValueError:
                            after = 0
            self._json(STATE.snapshot(after))
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/call/reset":
            CALL.reset()
            self._json({"reset": True})
            return

        if self.path == "/api/call/turn":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._json({"error": "no audio in the request body"}, status=400)
                return
            if length > 8 * 1024 * 1024:
                self._json({"error": "audio too large (8 MB cap)"}, status=413)
                return
            audio = self.rfile.read(length)
            try:
                self._json(_live_call_turn(audio))
            except Exception as error:  # noqa: BLE001 - reported, never a 500 page
                self._json({"error": str(error)}, status=400)
            return

        if not self.path.startswith("/api/run/"):
            self._json({"error": "unknown endpoint"}, status=404)
            return
        run_id = self.path[len("/api/run/"):].strip("/")
        if run_id not in RUNS:
            # The id is looked up in a fixed table and never interpolated
            # anywhere, so an unknown one is simply refused.
            self._json({"error": "unknown run id"}, status=400)
            return
        if not STATE.begin(run_id):
            self._json({"error": "a run is already in progress"}, status=409)
            return
        threading.Thread(target=_worker, args=(run_id,), daemon=True).start()
        self._json({"started": run_id})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int = 8000) -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    # 127.0.0.1, never 0.0.0.0: this server starts subprocesses, and nothing
    # about it should be reachable from another machine.
    httpd = Server(("127.0.0.1", port), Handler)
    print("Pramaan demo server")
    print("  root      %s" % ROOT.name)
    print("  open      http://127.0.0.1:%d/dashboard/" % port)
    print("  runs      %s" % ", ".join(RUNS))
    print("  offline   PRAMAAN_LLM_OFFLINE=1 is forced on every run")
    print()
    print("  Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="pramaan.report.server",
        description="Serve the dashboard and run the real pipeline from it.",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
