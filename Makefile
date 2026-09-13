PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff
NVIM ?= nvim

.PHONY: test test-plugin lint fmt perf gate

test:
	$(PYTHON) -m pytest tests/

# The Neovim thin client's suite (spec 10 §2: the nvim client's own tests
# cover rendering/keymap dispatch; `e2e_spec.lua` additionally drives a REAL
# core end-to-end, which is the spec 09 §3 gate).
#
# Each spec runs in its OWN headless nvim, IN-PROCESS via plenary.busted.run:
# `:PlenaryBustedFile` spawns a child nvim WITHOUT forwarding `-u`, so the
# child would load the developer's personal init.lua instead of
# tests/plugin/minimal_init.lua (verified in plenary's test_harness.lua:84).
# A process per spec also keeps the seats' module injections — and the real
# `organize serve` children they spawn — isolated from one another.
#
# TMPDIR is handed to nvim symlink-free: on macOS it is /var/folders/…, and
# /var -> /private/var, so fixture paths built from `vim.fn.tempname()` never
# equalled the resolved paths the core returns.
test-plugin:
	@fail=0; \
	tmp=$$(cd "$${TMPDIR:-/tmp}" && pwd -P); \
	for spec in tests/plugin/*_spec.lua; do \
		echo "=== $$spec"; \
		TMPDIR="$$tmp" $(NVIM) --headless --noplugin -u tests/plugin/minimal_init.lua \
			-c "lua require('plenary.busted').run('$(CURDIR)/$$spec')" || fail=1; \
	done; \
	if [ $$fail -ne 0 ]; then echo "PLUGIN SUITE FAILED"; exit 1; fi; \
	echo "plugin suite green"

# The spec 09 §4 full-scale perf gates (10k reindex < 5s etc.) — excluded
# from the default run via addopts "-m 'not slow'"; part of every phase gate.
perf:
	$(PYTHON) -m pytest tests/ -q -m slow

# Everything a phase must pass before its checkpoint commit.
gate: test test-plugin perf lint

lint:
	$(RUFF) check src tests

fmt:
	$(RUFF) format src tests
