"""The gateway's HTTP surface, end to end through FastAPI."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(gateway):
    with TestClient(create_app(gateway.services)) as c:
        yield c


def test_sync_and_read_back(client):
    response = client.post("/sync/patient", params={"tenant": "practice_a"})
    assert response.status_code == 200 and response.json()["inserted"] == 25

    patients = client.get("/patients", params={"tenant": "practice_a"}).json()
    assert len(patients) == 25 and patients[0]["vendor_id"] == "P-1001"
    one = client.get(f"/patients/{patients[0]['id']}").json()
    assert one["mrn"] == patients[0]["mrn"]
    assert client.get("/patients/99999").status_code == 404


def test_unknown_tenant_is_404(client):
    assert client.post("/sync/patient", params={"tenant": "nope"}).status_code == 404


def test_unknown_resource_is_422(client):
    assert client.post("/sync/medication", params={"tenant": "practice_a"}).status_code == 422


def test_breaking_change_story_through_the_api(client, gateway):
    client.post("/sync/patient", params={"tenant": "practice_a"})
    gateway.scenario("breaking")
    halted = client.post("/sync/patient", params={"tenant": "practice_b"}).json()
    assert halted["status"] == "halted"

    health = client.get("/health").json()
    assert health["status"] == "degraded" and health["quarantined_records"] == 10
    assert client.post("/sync/patient", params={"tenant": "practice_b"}).status_code == 503

    incidents = client.get("/incidents").json()
    assert {i["field_path"] for i in incidents} >= {"dob", "mrn"}
    assert len(client.get("/quarantine", params={"tenant": "practice_b"}).json()) == 10
    [drift] = client.get("/drift").json()
    assert "birth_date" in drift["diff"]["added"]

    replay = client.post("/replay", params={"resource": "patient"}).json()
    assert replay["still_quarantined"] == 10, "old mapper: nothing guessed"


def test_promote_drift_and_reset_breaker(client, gateway):
    client.post("/sync/patient", params={"tenant": "practice_a"})
    gateway.scenario("additive")
    client.post("/sync/patient", params={"tenant": "practice_a", "full": True})
    [event] = client.get("/drift").json()
    promoted = client.post(f"/drift/{event['id']}/promote").json()
    assert promoted["status"] == "acked"

    gateway.scenario("pagination")
    client.post("/sync/patient", params={"tenant": "practice_b"})
    assert client.post("/sync/patient", params={"tenant": "practice_b"}).status_code == 503
    reset = client.post("/breaker/reset", params={"tenant": "practice_b", "resource": "patient"})
    assert reset.json()["state"] == "closed"


def test_dlq_endpoint(client, gateway):
    gateway.scenario("malformed_always")
    client.post("/sync/patient", params={"tenant": "practice_a"})
    [row] = client.get("/dlq").json()
    assert row["failure_code"] == "MALFORMED_RESPONSE"
