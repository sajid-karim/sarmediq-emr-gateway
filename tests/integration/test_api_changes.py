"""Each undocumented vendor change, and what the gateway does about it (ARCHITECTURE §9)."""

from app.core.errors import ErrorCode
from app.core.types import (
    BreakerState,
    IncidentStatus,
    PendingStatus,
    QuarantineScope,
    QuarantineStatus,
    Resource,
    Severity,
)
from app.db.models import (
    AppointmentRow,
    CircuitState,
    ClinicalReportRow,
    DriftEvent,
    Incident,
    PatientRow,
    PendingRecord,
    Quarantine,
    RecordVersion,
)
from app.sync.orchestrator import SyncStatus


def baseline(gateway, tenant="practice_a", resource=Resource.PATIENT):
    report = gateway.orchestrator.sync(tenant, resource, full=True)
    assert report.status is SyncStatus.COMPLETED
    return report


def test_additive_fields_are_accepted_and_reported_once(gateway):
    baseline(gateway)
    gateway.scenario("additive")
    first = gateway.orchestrator.sync("practice_a", Resource.PATIENT, full=True)
    second = gateway.orchestrator.sync("practice_a", Resource.PATIENT, full=True)

    assert first.status is SyncStatus.COMPLETED and first.unchanged == 25
    assert len(first.drift_events) == 1 and second.drift_events == []
    [event] = gateway.all(DriftEvent)
    assert set(event.diff["added"]) == {"portal_enabled", "preferred_language"}
    assert event.diff["removed"] == []
    assert gateway.count(Incident) == 0
    assert gateway.count(Quarantine) == 0


def test_missing_optional_field_degrades_but_keeps_stored_value(gateway):
    baseline(gateway)
    before = {p.vendor_id: p.middle_name for p in gateway.all(PatientRow)}
    gateway.scenario("optional_missing")
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT, full=True)

    assert report.status is SyncStatus.COMPLETED
    assert (report.degraded, report.unchanged, report.quarantined) == (25, 25, 0)
    after = {p.vendor_id: p.middle_name for p in gateway.all(PatientRow)}
    assert after == before, "absence is not deletion: middle names are kept"
    [incident] = gateway.all(Incident)
    assert (incident.error_code, incident.field_path) == (
        ErrorCode.MISSING_OPTIONAL_FIELD,
        "name.middle",
    )
    assert incident.severity == Severity.INFO and incident.count == 25
    [event] = gateway.all(DriftEvent)
    assert event.diff["removed"] == ["name.middle"]


def test_breaking_change_is_quarantined_and_trips_the_breaker(gateway):
    baseline(gateway, "practice_a")
    gateway.scenario("breaking")
    report = gateway.orchestrator.sync("practice_b", Resource.PATIENT)

    assert report.status is SyncStatus.HALTED and report.halt_reason == ErrorCode.BATCH_THRESHOLD
    assert report.inserted == 0 and report.quarantined == 10
    assert gateway.count(PatientRow, PatientRow.tenant_id == 2) == 0, (
        "nothing guessed into practice_b"
    )

    codes = {(i.error_code, i.field_path) for i in gateway.all(Incident)}
    assert (ErrorCode.MISSING_CRITICAL_FIELD, "dob") in codes
    assert (ErrorCode.MISSING_CRITICAL_FIELD, "mrn") in codes
    assert (ErrorCode.BATCH_THRESHOLD, "") in codes
    dob = next(i for i in gateway.all(Incident) if i.field_path == "dob")
    assert dob.count == 10, "one incident for ten records, not ten incidents"

    row = gateway.all(Quarantine)[0]
    assert {"dob", "mrn"} <= set(row.fingerprint_diff["removed"])
    assert "birth_date" in row.fingerprint_diff["added"]

    [breaker] = gateway.all(CircuitState, CircuitState.tenant_id == 2)
    assert breaker.state == BreakerState.OPEN

    blocked = gateway.orchestrator.sync("practice_b", Resource.PATIENT)
    assert blocked.status is SyncStatus.BLOCKED


def test_breaker_is_isolated_per_tenant(gateway):
    baseline(gateway, "practice_a")
    gateway.scenario("breaking")
    gateway.orchestrator.sync("practice_b", Resource.PATIENT)
    gateway.scenario("v1")
    assert (
        gateway.orchestrator.sync("practice_a", Resource.PATIENT, full=True).status
        is SyncStatus.COMPLETED
    )


def test_envelope_change_quarantines_the_page_and_halts(gateway):
    baseline(gateway, "practice_a")
    gateway.scenario("envelope")
    report = gateway.orchestrator.sync("practice_b", Resource.PATIENT)

    assert report.status is SyncStatus.HALTED and report.halt_reason == ErrorCode.ENVELOPE_CHANGED
    assert report.fetched == 0
    [row] = gateway.all(Quarantine)
    assert row.scope == QuarantineScope.PAGE and row.record_index is None
    [incident] = gateway.all(Incident)
    assert incident.error_code == ErrorCode.ENVELOPE_CHANGED
    assert gateway.all(CircuitState, CircuitState.tenant_id == 2)[0].state == BreakerState.OPEN


def test_unknown_status_quarantines_only_those_records(gateway):
    baseline(gateway)
    gateway.scenario("new_status")
    report = gateway.orchestrator.sync("practice_a", Resource.APPOINTMENT)

    assert report.status is SyncStatus.COMPLETED, "1 bad in 10 is data, not a contract break"
    assert (report.inserted, report.quarantined) == (22, 3)
    [incident] = gateway.all(Incident)
    assert (incident.error_code, incident.field_path, incident.count) == (
        ErrorCode.UNKNOWN_ENUM_VALUE,
        "status",
        3,
    )
    statuses = {a.status for a in gateway.all(AppointmentRow)}
    assert "checked_in_virtual" not in statuses


def test_report_change(gateway):
    baseline(gateway)
    baseline(gateway, resource=Resource.CLINICAL_REPORT)
    gateway.scenario("report_change")
    report = gateway.orchestrator.sync("practice_a", Resource.CLINICAL_REPORT, full=True)

    assert report.status is SyncStatus.COMPLETED
    assert (report.updated, report.quarantined, report.unchanged) == (1, 2, 12)
    codes = {(q.vendor_record_id, tuple(q.error_codes)) for q in gateway.all(Quarantine)}
    assert codes == {
        ("R-3002", (ErrorCode.MISSING_CRITICAL_FIELD,)),
        ("R-3003", (ErrorCode.UNKNOWN_ENUM_VALUE,)),
    }
    amended = gateway.all(ClinicalReportRow, ClinicalReportRow.vendor_id == "R-3001")[0]
    assert amended.status == "amended" and amended.version == 2
    [change] = gateway.all(
        RecordVersion, RecordVersion.record_id == amended.id, RecordVersion.version == 2
    )
    assert change.diff == {"status": {"from": "final", "to": "amended"}}


def test_child_before_parent_waits_then_lands(gateway):
    early = gateway.orchestrator.sync("practice_b", Resource.APPOINTMENT)
    assert (early.status, early.pending, early.inserted) == (SyncStatus.COMPLETED, 25, 0)
    assert gateway.count(Incident) == 0, "waiting for a parent is timing, not an incident"

    patients = gateway.orchestrator.sync("practice_b", Resource.PATIENT)
    assert patients.pending_resolved == 25
    assert gateway.count(AppointmentRow) == 25
    assert gateway.count(PendingRecord, PendingRecord.status == PendingStatus.RESOLVED) == 25


def test_valid_record_supersedes_old_quarantine(gateway):
    baseline(gateway)
    gateway.scenario("new_status")
    gateway.orchestrator.sync("practice_a", Resource.APPOINTMENT)
    gateway.scenario("v1")
    report = gateway.orchestrator.sync("practice_a", Resource.APPOINTMENT, full=True)

    assert report.inserted == 3
    assert gateway.count(Quarantine, Quarantine.status == QuarantineStatus.QUARANTINED) == 0
    assert gateway.all(Incident)[0].status == IncidentStatus.CLOSED
