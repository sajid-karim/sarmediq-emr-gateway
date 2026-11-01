"""Mock "Acme EMR" vendor API. See CONTRACT.md for the documented v1 behaviour."""

import base64
import threading
import time
from collections import Counter
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from mock_emr.data import FIXTURES_MODIFIED_AT, PRACTICES, RESOURCES, records
from mock_emr.scenarios import SCENARIOS, transform

app = FastAPI(title="Acme EMR (mock)")


class MockState:
    def __init__(self) -> None:
        self.scenario = "v1"
        self.slow_seconds = 2.0
        self._calls: Counter[tuple[str, str]] = Counter()
        self._lock = threading.Lock()

    def reset(self, scenario: str, slow_seconds: float) -> None:
        with self._lock:
            self.scenario = scenario
            self.slow_seconds = slow_seconds
            self._calls.clear()

    def next_call(self, key: tuple[str, str]) -> int:
        with self._lock:
            self._calls[key] += 1
            return self._calls[key]


state = MockState()


class ScenarioRequest(BaseModel):
    scenario: str
    slow_seconds: float = 2.0


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode()


def decode_cursor(cursor: str) -> int:
    try:
        prefix, value = base64.urlsafe_b64decode(cursor.encode()).decode().split(":")
        offset = int(value)
        if prefix != "offset" or offset < 0:
            raise ValueError(cursor)
        return offset
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


@app.post("/_admin/scenario")
def set_scenario(request: ScenarioRequest) -> dict:
    if request.scenario not in SCENARIOS:
        raise HTTPException(status_code=400, detail=f"unknown scenario; choose one of {SCENARIOS}")
    state.reset(request.scenario, request.slow_seconds)
    return {"scenario": state.scenario, "slow_seconds": state.slow_seconds}


@app.get("/_admin/scenario")
def get_scenario() -> dict:
    return {"scenario": state.scenario, "slow_seconds": state.slow_seconds}


@app.get("/{practice_id}/{resource}")
def list_records(
    practice_id: str,
    resource: str,
    limit: int = Query(10, ge=1, le=100),
    cursor: str | None = None,
    updated_since: datetime | None = None,
) -> Response:
    if practice_id not in PRACTICES or resource not in RESOURCES:
        raise HTTPException(status_code=404, detail="not found")
    if updated_since is not None and updated_since.tzinfo is None:
        raise HTTPException(status_code=400, detail="updated_since must include a timezone")

    scenario = state.scenario
    call = state.next_call((practice_id, resource))

    if scenario == "rate_limit" and call % 3 == 0:
        return JSONResponse(
            {"error": "rate limited"}, status_code=429, headers={"Retry-After": "1"}
        )
    if scenario == "flaky":
        if call % 4 == 1:
            return JSONResponse({"error": "service unavailable"}, status_code=503)
        if call % 4 == 2:
            time.sleep(state.slow_seconds)
    if scenario == "malformed_always" or (scenario == "malformed" and call % 3 == 0):
        return HTMLResponse("<html><body><h1>502 Bad Gateway</h1></body></html>")

    rows = transform(scenario, resource, records(practice_id, resource))
    if updated_since is not None and updated_since >= FIXTURES_MODIFIED_AT:
        rows = []

    offset = decode_cursor(cursor) if cursor else 0
    page = rows[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = encode_cursor(next_offset) if next_offset < len(rows) else None
    if scenario == "pagination" and cursor is not None:
        next_cursor = cursor

    if scenario == "envelope":
        return JSONResponse(
            {"data": page, "meta": {"next_cursor": next_cursor, "count": len(page)}}
        )
    headers = {"X-Next-Cursor": next_cursor} if next_cursor else {}
    return JSONResponse(page, headers=headers)
