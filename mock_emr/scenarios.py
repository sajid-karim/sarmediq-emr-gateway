"""Undocumented vendor changes the mock can switch on. Each is a pure transform of the v1 data.

Behavioural scenarios (rate_limit, flaky, malformed*, pagination, envelope) are applied in
app.py because they change the HTTP response, not the records.
"""

from copy import deepcopy

SCENARIOS = (
    "v1",
    "additive",
    "optional_missing",
    "breaking",
    "envelope",
    "report_change",
    "new_status",
    "rate_limit",
    "pagination",
    "malformed",
    "malformed_always",
    "flaky",
    "orphan",
)


def transform(scenario: str, resource: str, records: list[dict]) -> list:
    rows: list = deepcopy(records)

    if scenario == "additive" and resource == "patients":
        for row in rows:
            row["preferred_language"] = row["language"]
            row["portal_enabled"] = True

    elif scenario == "optional_missing" and resource == "patients":
        for row in rows:
            row["name"].pop("middle", None)

    elif scenario == "breaking" and resource == "patients":
        for row in rows:
            row["birth_date"] = row.pop("dob")
            row["identifiers"] = [{"system": "MRN", "value": row.pop("mrn")}]

    elif scenario == "report_change" and resource == "reports":
        rows[0]["status"] = "amended"
        rows[1]["result"] = {"value": "5.4", "units": rows[1].pop("units")}
        rows[2]["status"] = "corrected"

    elif scenario == "new_status" and resource == "appointments":
        for index, row in enumerate(rows):
            if index % 10 == 4:
                row["status"] = "checked_in_virtual"

    elif scenario == "malformed" and resource == "patients":
        rows[3] = None

    elif scenario == "orphan" and resource == "appointments":
        rows[0]["patient_id"] = "P-9999"  # a patient this practice never returns

    return rows
