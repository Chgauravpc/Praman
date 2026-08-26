.PHONY: help demo demo-full demo-live test verify golden clean install

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

# The only target permitted to touch the network. It rewrites fixtures/llm_cache,
# so the diff it produces is the record of what a prompt change cost.
demo-live:
	@PRAMAAN_LLM_OFFLINE=0 $(PYTHON) -m pramaan.cli demo --dev

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
