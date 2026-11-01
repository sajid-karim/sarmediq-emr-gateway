import base64

import pytest
from fastapi.testclient import TestClient

from mock_emr.app import app, state


@pytest.fixture
def client():
    state.reset("v1", slow_seconds=0)
    with TestClient(app) as c:
        yield c
    state.reset("v1", slow_seconds=0)


def use(client, scenario):
    assert (
        client.post("/_admin/scenario", json={"scenario": scenario, "slow_seconds": 0}).status_code
        == 200
    )


def walk(client, practice, resource):
    rows, cursor, pages = [], None, 0
    while True:
        params = {"cursor": cursor} if cursor else {}
        response = client.get(f"/{practice}/{resource}", params=params)
        assert response.status_code == 200
        rows.extend(response.json())
        pages += 1
        cursor = response.headers.get("x-next-cursor")
        if not cursor:
            return rows, pages


def test_v1_pages_through_every_record(client):
    patients, pages = walk(client, "practice_a", "patients")
    assert len(patients) == 25 and pages == 3
    assert len({p["id"] for p in patients}) == 25
    assert set(patients[0]) == {"id", "mrn", "dob", "name", "sex", "contact", "address", "language"}


def test_practices_share_ids_but_not_people(client):
    a = client.get("/practice_a/patients").json()[0]
    b = client.get("/practice_b/patients").json()[0]
    assert a["id"] == b["id"] == "P-1001"
    assert a["mrn"] != b["mrn"]


def test_unknown_practice_is_404(client):
    assert client.get("/practice_zzz/patients").status_code == 404


def test_updated_since_after_fixture_date_returns_nothing(client):
    response = client.get("/practice_a/patients", params={"updated_since": "2026-10-02T00:00:00Z"})
    assert response.json() == []


def test_updated_since_without_timezone_is_400_not_500(client):
    response = client.get("/practice_a/patients", params={"updated_since": "2026-10-05T00:00:00"})
    assert response.status_code == 400


@pytest.mark.parametrize("raw", [b"offset:-5", b"offset:abc", b"page:3", b"offset"])
def test_bad_cursors_are_400(client, raw):
    cursor = base64.urlsafe_b64encode(raw).decode()
    assert client.get("/practice_a/patients", params={"cursor": cursor}).status_code == 400
    assert client.get("/practice_a/patients", params={"cursor": "!!!"}).status_code == 400


def test_breaking_renames_and_nests(client):
    use(client, "breaking")
    row = client.get("/practice_a/patients").json()[0]
    assert "dob" not in row and "birth_date" in row
    assert "mrn" not in row and row["identifiers"][0]["system"] == "MRN"


def test_envelope_wraps_body_and_moves_cursor(client):
    use(client, "envelope")
    response = client.get("/practice_a/patients")
    body = response.json()
    assert isinstance(body, dict) and len(body["data"]) == 10
    assert body["meta"]["next_cursor"] and "x-next-cursor" not in response.headers


def test_new_status_only_on_some_appointments(client):
    use(client, "new_status")
    rows = client.get("/practice_a/appointments").json()
    assert [r["status"] for r in rows].count("checked_in_virtual") == 1


def test_report_change(client):
    use(client, "report_change")
    rows = client.get("/practice_a/reports").json()
    assert rows[0]["status"] == "amended"
    assert "units" not in rows[1] and rows[1]["result"]["units"]
    assert rows[2]["status"] == "corrected"


def test_rate_limit_every_third_call(client):
    use(client, "rate_limit")
    codes = [client.get("/practice_a/patients").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_malformed_returns_html_and_null_element(client):
    use(client, "malformed")
    first = client.get("/practice_a/patients")
    assert first.json()[3] is None
    client.get("/practice_a/patients")
    third = client.get("/practice_a/patients")
    assert third.status_code == 200 and third.text.startswith("<html>")


def test_pagination_loops(client):
    use(client, "pagination")
    first = client.get("/practice_a/patients").headers["x-next-cursor"]
    second = client.get("/practice_a/patients", params={"cursor": first}).headers["x-next-cursor"]
    assert second == first


def test_flaky_returns_503_first(client):
    use(client, "flaky")
    assert client.get("/practice_a/patients").status_code == 503


def test_unknown_scenario_rejected(client):
    assert client.post("/_admin/scenario", json={"scenario": "nope"}).status_code == 400


def test_orphan_points_one_appointment_at_a_missing_patient(client):
    use(client, "orphan")
    rows = client.get("/practice_a/appointments").json()
    assert rows[0]["patient_id"] == "P-9999"
    assert all(r["patient_id"] != "P-9999" for r in rows[1:])


def test_sex_codes_converts_male_and_female(client):
    use(client, "sex_codes")
    sexes = {r["sex"] for r in client.get("/practice_a/patients").json()}
    assert sexes == {"M", "F", "other"}
