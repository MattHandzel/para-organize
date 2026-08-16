PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff

.PHONY: test lint fmt

test:
	$(PYTHON) -m pytest tests/

lint:
	$(RUFF) check src tests

fmt:
	$(RUFF) format src tests
