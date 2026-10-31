"""v1 baseline, idempotency, tenant isolation, incremental sync, crash-and-resume."""

import pytest

from app.core.types import Resource
from app.db.models import (
    AppointmentRow,
    PatientRow,
    ProcessingLedger,
    RawPayload,
    RecordVersion,
    SchemaFingerprint,
    SyncCheckpoint,
)
from app.sync.orchestrator import SyncOrchestrator, SyncStatus


def test_v1_baseline_writes_every_record(gateway):
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert report.status is SyncStatus.COMPLETED
    assert (report.pages, report.fetched, report.inserted, report.quarantined) == (3, 25, 25, 0)
    assert gateway.count(PatientRow) == 25
    assert gateway.count(RawPayload) == 3
    assert gateway.count(RecordVersion) == 25
    assert gateway.count(SchemaFingerprint, SchemaFingerprint.is_baseline.is_(True)) == 1
    checkpoint = gateway.all(SyncCheckpoint)[0]
    assert checkpoint.cursor is None and checkpoint.run_id is None
    assert checkpoint.last_success_started_at is not None


def test_all_three_resources(gateway):
    for resource, expected in (
        (Resource.PATIENT, 25),
        (Resource.APPOINTMENT, 25),
        (Resource.CLINICAL_REPORT, 15),
    ):
        report = gateway.orchestrator.sync("practice_a", resource)
        assert report.status is SyncStatus.COMPLETED and report.inserted == expected


def test_full_resync_is_a_no_op(gateway):
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    again = gateway.orchestrator.sync("practice_a", Resource.PATIENT, full=True)
    assert (again.fetched, again.inserted, again.updated, again.unchanged) == (25, 0, 0, 25)
    assert gateway.count(PatientRow) == 25
    assert gateway.count(RecordVersion) == 25


def test_incremental_sync_asks_only_for_changes(gateway):
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    again = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert again.status is SyncStatus.COMPLETED and again.fetched == 0


def test_same_vendor_ids_in_two_practices_stay_separate(gateway):
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    gateway.orchestrator.sync("practice_b", Resource.PATIENT)
    rows = gateway.all(PatientRow, PatientRow.vendor_id == "P-1001")
    assert len(rows) == 2
    assert {row.tenant_id for row in rows} == {1, 2}
    assert rows[0].mrn != rows[1].mrn


def test_appointments_link_to_the_patient_in_the_same_practice(gateway):
    for tenant in ("practice_a", "practice_b"):
        gateway.orchestrator.sync(tenant, Resource.PATIENT)
        gateway.orchestrator.sync(tenant, Resource.APPOINTMENT)
    patients = {(p.tenant_id, p.vendor_id): p.id for p in gateway.all(PatientRow)}
    for appointment in gateway.all(AppointmentRow):
        key = (appointment.tenant_id, appointment.vendor_patient_id)
        assert appointment.patient_id == patients[key]


def test_crash_mid_run_resumes_from_checkpoint_without_duplicates(gateway, monkeypatch):
    original = SyncOrchestrator._process_record
    calls = {"n": 0}

    def crash_on_15th(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 15:
            raise RuntimeError("simulated crash")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SyncOrchestrator, "_process_record", crash_on_15th)
    with pytest.raises(RuntimeError):
        gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    monkeypatch.setattr(SyncOrchestrator, "_process_record", original)

    checkpoint = gateway.all(SyncCheckpoint)[0]
    assert checkpoint.cursor is not None, "page 1 committed, so the cursor points at page 2"
    assert checkpoint.run_id is None, "the lock is released even on a crash"
    assert gateway.count(PatientRow) == 14

    resumed = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert resumed.status is SyncStatus.COMPLETED
    assert resumed.fetched == 15, "resumed at page 2, not page 1"
    assert (resumed.inserted, resumed.unchanged) == (11, 4)
    assert gateway.count(PatientRow) == 25


def test_ledger_records_every_processed_record(gateway):
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert gateway.count(ProcessingLedger) == 25
