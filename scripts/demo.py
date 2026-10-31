"""End-to-end story in one command (`make demo`), no servers to start by hand.

Starts the mock vendor on a free port, runs the gateway's HTTP API in-process against a fresh
SQLite file, and walks through: baseline -> additive change -> breaking change -> fix -> replay.
"""

import json
import socket
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")

from app.config import Settings  # noqa: E402
from app.main import build_acme_adapter, build_services, create_app  # noqa: E402
from app.sync.orchestrator import SyncOrchestrator  # noqa: E402
from mock_emr.app import app as mock_app  # noqa: E402


def start_mock() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mock_app, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}"


def step(title: str) -> None:
    print(f"\n{'=' * 78}\n  {title}\n{'=' * 78}")


def show(label: str, data: object, keys: tuple[str, ...] = ()) -> None:
    if keys and isinstance(data, dict):
        data = {k: data[k] for k in keys if k in data}
    print(f"- {label}: {json.dumps(data, default=str)}")


REPORT_KEYS = ("status", "halt_reason", "fetched", "inserted", "updated", "unchanged")
REPORT_KEYS += ("degraded", "pending", "quarantined", "retries", "drift_events")


def main() -> None:
    mock_url = start_mock()
    workdir = tempfile.mkdtemp(prefix="emr-demo-")
    settings = Settings(
        database_url=f"sqlite:///{workdir}/demo.db",
        vendor_base_url=mock_url,
        backoff_base=0.05,
        retry_after_cap=1,
        log_level="CRITICAL",
    )
    services = build_services(settings)

    def scenario(name: str) -> None:
        httpx.post(f"{mock_url}/_admin/scenario", json={"scenario": name}).raise_for_status()
        print(f"  [vendor silently switches to: {name}]")

    with TestClient(create_app(services)) as api:
        step("1. Baseline: documented v1 contract")
        for resource in ("patient", "appointment", "clinical_report"):
            report = api.post(f"/sync/{resource}", params={"tenant": "practice_a"}).json()
            show(f"sync {resource} practice_a", report, REPORT_KEYS)

        step("2. Re-sync everything: idempotent, nothing duplicated")
        report = api.post("/sync/patient", params={"tenant": "practice_a", "full": True}).json()
        show("full re-sync", report, REPORT_KEYS)

        step("3. Backward-compatible change: new fields appear (HTTP 200, no error)")
        scenario("additive")
        report = api.post("/sync/patient", params={"tenant": "practice_a", "full": True}).json()
        show("sync", report, REPORT_KEYS)
        show("drift detected", api.get("/drift").json()[-1]["diff"])
        show("open incidents", len(api.get("/incidents").json()))

        step("4. Behavioural change: unknown appointment status")
        scenario("new_status")
        report = api.post("/sync/appointment", params={"tenant": "practice_a", "full": True})
        show("sync", report.json(), REPORT_KEYS)

        step("5. Breaking change: dob -> birth_date, mrn -> identifiers[] (practice_b)")
        scenario("breaking")
        report = api.post("/sync/patient", params={"tenant": "practice_b"}).json()
        show("sync", report, REPORT_KEYS)
        health = api.get("/health").json()
        show("health", health, ("status", "quarantined_records", "open_incidents"))
        blocked = api.post("/sync/patient", params={"tenant": "practice_b"})
        show("sync again (breaker open)", {"http_status": blocked.status_code})
        for incident in api.get("/incidents").json():
            if incident["severity"] == "critical":
                show(
                    "incident",
                    incident,
                    ("error_code", "resource", "field_path", "count", "sample_payload_ids"),
                )
        patients_q = api.get("/quarantine", params={"resource": "patient"}).json()
        show("what changed (vs baseline)", patients_q[0]["fingerprint_diff"])
        show(
            "replay with the OLD mapper",
            api.post("/replay").json(),
            ("resolved", "still_quarantined"),
        )

    step("6. Engineer ships the fix: mapping_version 2 adds the approved aliases")
    fixed = build_acme_adapter(settings, mapping_version=2)
    services.orchestrator = SyncOrchestrator(services.session_factory, fixed, settings)
    with TestClient(create_app(services)) as api:
        report = api.post("/replay", params={"resource": "patient"}).json()
        show("replay from stored raw (no vendor call)", report)
        report = api.post("/sync/patient", params={"tenant": "practice_b"}).json()
        show("next sync resumes on the fixed mapper", report, REPORT_KEYS)
        health = api.get("/health").json()
        show("health", health, ("status", "quarantined_records", "open_incidents"))
        patients = api.get("/patients", params={"tenant": "practice_b"}).json()
        show("practice_b patients stored", len(patients))
        show("sample", patients[0], ("vendor_id", "mrn", "dob", "mapping_version", "version"))
        left = api.get("/quarantine").json()
        show(
            "still quarantined (unknown appointment status awaits a product decision)",
            [(q["resource"], q["vendor_record_id"], q["error_codes"]) for q in left],
        )

    services.close()
    print(f"\nDatabase kept for inspection: {workdir}/demo.db")


if __name__ == "__main__":
    main()
