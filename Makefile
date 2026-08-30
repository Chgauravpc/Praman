.PHONY: help demo demo-full demo-live investigate investigate-live models test verify golden clean install execute execute-full execute-live voice voice-live

PYTHON ?= python

# `make demo` runs the 200-event dev batch, needs no API key, and makes no
# network call. That is the reviewer path: git clone && make demo reproduces the
# headline number with zero setup (NFR-4, F10).
#
# On the `--dev` spelling: the build plan writes this target as `make demo --dev`,
# but GNU make parses a leading `--` as one of its own options and rejects an
# unknown one, so `make demo --dev` cannot work in any Makefile. The dev batch is
# therefore the *default*, with `make demo-full` for the 6,000-event run. The
# underlying CLI does accept the flag literally:
#     python -m pramaan.cli demo --dev
# See DECISIONS.md ADR-008.

help:
	@echo "Pramaan -- targets"
	@echo "  make demo        200-event dev batch. No API key needed. Start here."
	@echo "  make demo-full   6,000-event batch (sized from the PRD power calc)"
	@echo "  make demo-live   re-run against live APIs and refresh the cache"
	@echo "  make investigate      the LLM investigator, from the committed cache"
	@echo "  make investigate-live the same, against a live model; refreshes the cache"
	@echo "  make models      print the configured model IDs and verify they resolve"
	@echo "  make execute          Day 5: plan -> envelope -> resolve, arm C wired, shadow mode"
	@echo "  make execute-full     the same, on the 6,000-event batch"
	@echo "  make execute-live     also creates one real order + payment link in Razorpay TEST mode"
	@echo "  make voice            Day 7: the Hinglish recovery call -> transcript + ledger, keyless"
	@echo "  make voice-live       the same on the live stack; writes assets/voice-demo.mp3 (Sarvam key)"
	@echo "  make test        the full test suite, including invariants I1/I2/I7"
	@echo "  make verify      test + demo determinism check (I8)"
	@echo "  make golden      regenerate the golden ledger. Read the diff."
	@echo "  make clean       remove build artefacts"

install:
	$(PYTHON) -m pip install -r requirements.txt

demo:
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli demo --dev

demo-full:
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli demo --full

# The investigator (Day 4). Runs from the committed cache and makes no network
# call, like every other offline target.
investigate:
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli investigate

# The two targets permitted to touch the network. They rewrite fixtures/llm_cache,
# so the diff they produce is the record of what a prompt change cost.
demo-live:
	@PRAMAAN_LLM_OFFLINE=0 $(PYTHON) -m pramaan.cli demo --dev

investigate-live:
	@PRAMAAN_LLM_OFFLINE=0 $(PYTHON) -m pramaan.cli investigate

# Costs no completion tokens: a GET against each provider's model list.
models:
	@$(PYTHON) -m pramaan.cli models

# Day 5. Shadow mode: arm C wired, planner memoised, C-B ablation, organic
# violation rate. Offline like every other default target -- with no LLM key
# the planner falls back to the NFR-2 deterministic default for every
# signature, which is a real and honestly-labelled result, not a stub.
execute:
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli execute --dev

execute-full:
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli execute --full

# The one target that may touch a real Razorpay account. Requires
# RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (test-mode) in .env; without them this
# prints the same honest blocker `make investigate` prints for a missing LLM
# key, and still exits 0.
execute-live:
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli execute --dev --live-razorpay

# Day 7. The Hinglish voice call. Deterministic and keyless: places the canonical
# recovery call, writes the verbatim transcript to assets/voice-transcript.md and
# the CONVERSE/PROMISE rows to a hash-chained ledger. No key, no network.
voice:
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli voice

# The same call on the live stack: the LLM turn policy (if a key is set) and
# Sarvam TTS to assets/voice-demo.mp3 (needs SARVAM_API_KEY). Without the keys it
# prints the same honest blocker every other live target does and still exits 0.
voice-live:
	@PRAMAAN_LLM_OFFLINE=0 $(PYTHON) -m pramaan.cli voice --live-sarvam

test:
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pytest tests -q

# Invariant I8: same seed plus same cache produces byte-identical output. Two
# runs are diffed rather than eyeballed, because a stray timestamp in the demo
# output is exactly the kind of regression a human skims past.
verify: test
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli demo --dev > /tmp/pramaan-run-a.txt
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli demo --dev > /tmp/pramaan-run-b.txt
	@diff /tmp/pramaan-run-a.txt /tmp/pramaan-run-b.txt \
		&& echo "I8 PASS: two runs are byte-identical" \
		|| (echo "I8 FAIL: demo output is not reproducible"; exit 1)

golden:
	@PRAMAAN_LLM_OFFLINE=1 $(PYTHON) -m pramaan.cli demo --dev > /dev/null
	@cp build/ledger-dev.jsonl tests/golden/ledger.jsonl
	@echo "golden ledger regenerated -- read the diff before committing it"

clean:
	@rm -rf build
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@rm -rf .pytest_cache
