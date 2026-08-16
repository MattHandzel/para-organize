PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff

.PHONY: test lint fmt perf gate

test:
	$(PYTHON) -m pytest tests/

# The spec 09 §4 full-scale perf gates (10k reindex < 5s etc.) — excluded
# from the default run via addopts "-m 'not slow'"; part of every phase gate.
perf:
	$(PYTHON) -m pytest tests/ -q -m slow

# Everything a phase must pass before its checkpoint commit.
gate: test perf lint

lint:
	$(RUFF) check src tests

fmt:
	$(RUFF) format src tests
