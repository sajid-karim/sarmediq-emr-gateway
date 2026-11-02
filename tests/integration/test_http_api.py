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


def test_read_api_uses_keyset_pagination(client):
    client.post("/sync/patient", params={"tenant": "practice_a"})
    seen, after, pages = [], None, 0
    while True:
        params = {"tenant": "practice_a", "limit": 10}
        if after:
            params["after"] = after
        response = client.get("/patients", params=params)
        seen += [row["vendor_id"] for row in response.json()]
        pages += 1
        after = response.headers.get("x-next-cursor")
        if not after:
            break
    assert pages == 3 and len(seen) == 25 and seen == sorted(set(seen))
    assert (
        client.get("/patients", params={"tenant": "practice_a", "limit": 25}).headers.get(
            "x-next-cursor"
        )
        is None
    ), "an exact-size last page has no next cursor"


def test_quarantine_listing_does_not_query_per_row(client, gateway):
    from sqlalchemy import event

    client.post("/sync/patient", params={"tenant": "practice_a"})
    gateway.scenario("breaking")
    client.post("/sync/patient", params={"tenant": "practice_b"})

    statements = []
    engine = gateway.services.engine
    listener = lambda *args: statements.append(args[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        rows = client.get("/quarantine").json()
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert len(rows) == 10 and all(row["incident_ids"] for row in rows)
    assert len(statements) <= 3, "one query for rows + one for all their incidents, not 1 + N"


def test_quarantine_status_is_validated(client):
    assert client.get("/quarantine", params={"status": "bogus"}).status_code == 422


def test_ops_lists_use_keyset_pagination(client, gateway):
    client.post("/sync/patient", params={"tenant": "practice_a"})
    gateway.scenario("breaking")
    client.post("/sync/patient", params={"tenant": "practice_b"})

    ids, after, pages = [], None, 0
    while True:
        params = {"limit": 4, **({"after": after} if after else {})}
        response = client.get("/quarantine", params=params)
        ids += [row["id"] for row in response.json()]
        pages += 1
        after = response.headers.get("x-next-cursor")
        if not after:
            break
    assert pages == 3 and len(ids) == 10 and ids == sorted(set(ids))

    first = client.get("/incidents", params={"limit": 1})
    assert len(first.json()) == 1 and first.headers.get("x-next-cursor")
    rest = client.get("/incidents", params={"after": first.headers["x-next-cursor"]}).json()
    assert first.json()[0]["id"] not in {row["id"] for row in rest}
    assert client.get("/dlq", params={"limit": 0}).status_code == 422
