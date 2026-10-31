PY := .venv/bin/python

.PHONY: install test lint mock run demo

install:
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

mock:
	$(PY) -m uvicorn mock_emr.app:app --port 9000 --reload

run:
	$(PY) -m uvicorn app.main:app --port 8000 --reload

demo:
	$(PY) scripts/demo.py
