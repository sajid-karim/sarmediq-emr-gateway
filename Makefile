PY := .venv/bin/python
MOCK_PORT ?= 9000
GATEWAY_PORT ?= 8000

.PHONY: install test lint mock run demo up down

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
	$(PY) -m uvicorn mock_emr.app:app --port $(MOCK_PORT)

run:
	EMR_VENDOR_BASE_URL=http://127.0.0.1:$(MOCK_PORT) $(PY) -m uvicorn app.main:app --port $(GATEWAY_PORT)

demo:
	$(PY) scripts/demo.py

up:
	docker compose up --build -d

down:
	docker compose down -v
