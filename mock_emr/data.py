"""Deterministic synthetic fixtures for the mock vendor. No real patient data.

Both practices use the same ID ranges (P-1001.., A-2001.., R-3001..) for different
people on purpose: vendor IDs are only unique within a practice.
"""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from functools import cache

PRACTICES = ("practice_a", "practice_b")
RESOURCES = ("patients", "appointments", "reports")
FIXTURES_MODIFIED_AT = datetime(2026, 10, 1, tzinfo=UTC)

_GIVEN = ("John", "Maria", "Ahmed", "Li", "Fatima", "James", "Sofia", "Omar", "Emma", "Raj")
_FAMILY = ("Smith", "Garcia", "Khan", "Chen", "Ali", "Brown", "Rossi", "Haddad", "Wilson", "Patel")
_MIDDLE = ("Robert", None, "Ann", "Lee", None)
_SEX = ("male", "female", "female", "male", "other")
_APPOINTMENT_STATUS = ("scheduled", "completed", "cancelled", "no_show", "scheduled")
_REPORT_TYPES = (("blood_panel", "mg/dL"), ("lipid_panel", "mmol/L"), ("hba1c", "%"))


def _iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _patients(p: int) -> list[dict]:
    rows = []
    for i in range(25):
        n = i + 7 * p
        rows.append(
            {
                "id": f"P-{1001 + i}",
                "mrn": f"MRN-{p + 1}{1001 + i}",
                "dob": date(1945 + (n * 7) % 60, 1 + n % 12, 1 + (n * 3) % 28).isoformat(),
                "name": {
                    "family": _FAMILY[n % 10],
                    "given": _GIVEN[(n * 3) % 10],
                    "middle": _MIDDLE[n % 5],
                },
                "sex": _SEX[n % 5],
                "contact": {"phone": f"555-{p}{i:03d}", "email": f"patient{p}{i:02d}@example.test"},
                "address": f"{100 + i} Main St",
                "language": "en" if n % 4 else "es",
            }
        )
    return rows


def _appointments(p: int) -> list[dict]:
    base = datetime(2026, 11, 2, 9, tzinfo=UTC)
    rows = []
    for i in range(25):
        rows.append(
            {
                "id": f"A-{2001 + i}",
                "patient_id": f"P-{1001 + (i * 3 + p) % 25}",
                "start_time": _iso_z(base + timedelta(days=i % 10, hours=i % 8)),
                "status": _APPOINTMENT_STATUS[(i + p) % 5],
                "provider_id": f"DR-{101 + i % 5}",
                "location": f"Clinic {'AB'[p]}, Room {1 + i % 4}",
                "notes": None if i % 3 else "Bring recent lab results",
                "reason": "Follow-up" if i % 2 else "Annual check-up",
            }
        )
    return rows


def _reports(p: int) -> list[dict]:
    issued = datetime(2026, 10, 15, 8, tzinfo=UTC)
    rows = []
    for i in range(15):
        report_type, units = _REPORT_TYPES[i % 3]
        rows.append(
            {
                "id": f"R-{3001 + i}",
                "patient_id": f"P-{1001 + (i * 2 + p) % 25}",
                "type": report_type,
                "status": "preliminary" if i % 4 == 3 else "final",
                "body": f"{report_type} result within reference range",
                "units": units,
                "author": "Dr. Adams",
                "issued_at": _iso_z(issued + timedelta(days=i)),
                "summary": None if i % 2 else "Normal",
            }
        )
    return rows


@cache
def _build(practice_id: str) -> dict[str, list[dict]]:
    p = PRACTICES.index(practice_id)
    return {"patients": _patients(p), "appointments": _appointments(p), "reports": _reports(p)}


def records(practice_id: str, resource: str) -> list[dict]:
    return deepcopy(_build(practice_id)[resource])
