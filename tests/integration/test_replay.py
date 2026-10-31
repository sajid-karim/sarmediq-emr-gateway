"""Recovering from an undocumented change: fix the mapper, replay from stored raw (§7)."""

from datetime import date

from app.core.errors import ErrorCode
from app.core.types import (
    BreakerState,
    IncidentStatus,
    QuarantineScope,
    QuarantineStatus,
    Resource,
)
from app.db.models import CircuitState, Incident, PatientRow, Quarantine, RawPayload
from app.sync.orchestrator import SyncStatus
from mock_emr.data import records


def break_practice_b(gateway):
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)  # baseline shape
    gateway.scenario("breaking")
    report = gateway.orchestrator.sync("practice_b", Resource.PATIENT)
    assert report.status is SyncStatus.HALTED and report.quarantined == 10


def test_replay_with_old_mapper_changes_nothing(gateway):
    break_practice_b(gateway)
    report = gateway.orchestrator.replay(resource=Resource.PATIENT)
    assert (report.replayed, report.resolved, report.still_quarantined) == (10, 0, 10)
    assert gateway.count(PatientRow, PatientRow.tenant_id == 2) == 0


def test_fix_then_replay_recovers_without_calling_the_vendor(gateway):
    break_practice_b(gateway)
    raw_before = gateway.count(RawPayload)
    gateway.scenario("v1")  # even if the vendor changes again, replay uses stored bytes

    fixed = gateway.with_mapping_version(2)
    report = fixed.replay(tenant_name="practice_b", error_code=ErrorCode.MISSING_CRITICAL_FIELD)

    assert (report.replayed, report.resolved, report.still_quarantined) == (10, 10, 0)
    assert report.breakers_reset == ["practice_b/patient"]
    assert gateway.count(RawPayload) == raw_before, "no vendor call"

    rows = gateway.all(PatientRow, PatientRow.tenant_id == 2)
    assert len(rows) == 10 and {r.mapping_version for r in rows} == {2}
    original = {r["id"]: r for r in records("practice_b", "patients")}
    for row in rows:
        assert row.dob == date.fromisoformat(original[row.vendor_id]["dob"])
        assert row.mrn == original[row.vendor_id]["mrn"]

    assert all(
        i.status == IncidentStatus.CLOSED for i in gateway.all(Incident, Incident.tenant_id == 2)
    )
    assert gateway.all(CircuitState, CircuitState.tenant_id == 2)[0].state == BreakerState.CLOSED


def test_replay_twice_is_a_no_op(gateway):
    break_practice_b(gateway)
    fixed = gateway.with_mapping_version(2)
    fixed.replay(resource=Resource.PATIENT)
    again = fixed.replay(resource=Resource.PATIENT)
    assert again.replayed == 0
    assert gateway.count(PatientRow, PatientRow.tenant_id == 2) == 10


def test_after_replay_the_next_sync_finishes_on_the_fixed_mapper(gateway):
    break_practice_b(gateway)
    fixed = gateway.with_mapping_version(2)
    fixed.replay(resource=Resource.PATIENT)
    report = fixed.sync("practice_b", Resource.PATIENT)  # vendor still sends the new shape
    assert report.status is SyncStatus.COMPLETED
    assert (report.unchanged, report.inserted) == (10, 15)
    assert gateway.count(PatientRow, PatientRow.tenant_id == 2) == 25


def test_envelope_page_replay(gateway):
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    gateway.scenario("envelope")
    halted = gateway.orchestrator.sync("practice_b", Resource.PATIENT)
    assert halted.status is SyncStatus.HALTED

    fixed = gateway.with_mapping_version(2)
    report = fixed.replay(scope=QuarantineScope.PAGE)
    assert (report.replayed, report.resolved) == (1, 1)
    assert report.breakers_reset == ["practice_b/patient"]
    assert gateway.count(PatientRow, PatientRow.tenant_id == 2) == 10
    assert gateway.count(Quarantine, Quarantine.status == QuarantineStatus.QUARANTINED) == 0

    finished = fixed.sync("practice_b", Resource.PATIENT)
    assert finished.status is SyncStatus.COMPLETED
    assert gateway.count(PatientRow, PatientRow.tenant_id == 2) == 25
