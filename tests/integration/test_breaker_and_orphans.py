"""HALF_OPEN probing after the cooldown, and pending records that never find their parent."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from app.core.clock import utcnow
from app.core.errors import ErrorCode
from app.core.types import (
    BreakerState,
    IncidentStatus,
    PendingStatus,
    QuarantineStatus,
    Resource,
)
from app.db.models import AppointmentRow, CircuitState, Incident, PendingRecord, Quarantine
from app.main import create_app
from app.sync.orchestrator import SyncStatus


def trip_with_pagination_loop(gateway):
    gateway.scenario("pagination")
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert report.status is SyncStatus.HALTED
    assert gateway.all(CircuitState)[0].state == BreakerState.OPEN


def age_breaker(gateway, seconds):
    with gateway.sessions() as session:
        session.execute(
            update(CircuitState).values(opened_at=utcnow() - timedelta(seconds=seconds))
        )
        session.commit()


def test_open_breaker_blocks_during_cooldown_and_says_when_to_retry(gateway):
    trip_with_pagination_loop(gateway)
    blocked = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert blocked.status is SyncStatus.BLOCKED
    assert 0 < blocked.retry_after_seconds <= gateway.services.settings.breaker_cooldown_seconds

    with TestClient(create_app(gateway.services)) as client:
        response = client.post("/sync/patient", params={"tenant": "practice_a"})
    assert response.status_code == 503 and int(response.headers["retry-after"]) > 0


def test_probe_after_cooldown_closes_the_breaker_when_vendor_recovered(gateway):
    trip_with_pagination_loop(gateway)
    age_breaker(gateway, gateway.services.settings.breaker_cooldown_seconds + 1)
    gateway.scenario("v1")

    probe = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert probe.breaker_probe is True and probe.status is SyncStatus.COMPLETED
    state = gateway.all(CircuitState)[0]
    assert state.state == BreakerState.CLOSED and state.reason_code is None
    [loop] = gateway.all(Incident, Incident.error_code == ErrorCode.PAGINATION_LOOP)
    assert loop.status == IncidentStatus.CLOSED, "a completed run proves the stream recovered"

    normal = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert normal.breaker_probe is False and normal.status is SyncStatus.COMPLETED


def test_failed_probe_reopens_and_restarts_the_cooldown(gateway):
    trip_with_pagination_loop(gateway)
    age_breaker(gateway, gateway.services.settings.breaker_cooldown_seconds + 1)

    probe = gateway.orchestrator.sync("practice_a", Resource.PATIENT)  # still looping
    assert probe.breaker_probe is True and probe.status is SyncStatus.HALTED
    state = gateway.all(CircuitState)[0]
    assert state.state == BreakerState.OPEN
    assert utcnow() - state.opened_at < timedelta(seconds=30), "cooldown restarted"
    assert gateway.orchestrator.sync("practice_a", Resource.PATIENT).status is SyncStatus.BLOCKED


def test_failed_probe_on_upstream_error_reopens_immediately(gateway):
    gateway.scenario("malformed_always")
    for _ in range(3):
        gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert gateway.all(CircuitState)[0].state == BreakerState.OPEN
    age_breaker(gateway, gateway.services.settings.breaker_cooldown_seconds + 1)

    probe = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert probe.breaker_probe is True and probe.status is SyncStatus.HALTED
    assert gateway.all(CircuitState)[0].state == BreakerState.OPEN


def test_child_whose_parent_never_arrives_is_escalated_as_orphan(gateway):
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    gateway.scenario("orphan")
    report = gateway.orchestrator.sync("practice_a", Resource.APPOINTMENT)
    assert (report.inserted, report.pending) == (24, 1)
    [waiting] = gateway.all(PendingRecord)
    assert (waiting.vendor_id, waiting.waiting_on_vendor_id) == ("A-2001", "P-9999")

    max_attempts = gateway.services.settings.pending_max_attempts
    for attempt in range(1, max_attempts + 1):
        gateway.orchestrator.sync("practice_a", Resource.PATIENT, full=True)
        row = gateway.all(PendingRecord)[0]
        if attempt < max_attempts:
            assert (row.status, row.attempts) == (PendingStatus.WAITING, attempt)
    assert row.status == PendingStatus.ESCALATED

    [orphan] = gateway.all(Quarantine)
    assert orphan.status == QuarantineStatus.QUARANTINED
    assert (orphan.vendor_record_id, orphan.error_codes) == ("A-2001", [ErrorCode.ORPHAN_REFERENCE])
    [incident] = gateway.all(Incident)
    assert (incident.error_code, incident.field_path) == (ErrorCode.ORPHAN_REFERENCE, "patient_id")
    assert gateway.count(AppointmentRow, AppointmentRow.vendor_id == "A-2001") == 0


def test_orphan_resolves_if_the_parent_shows_up_before_escalation(gateway):
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    gateway.scenario("orphan")
    gateway.orchestrator.sync("practice_a", Resource.APPOINTMENT)
    gateway.scenario("v1")  # vendor fixes the reference
    report = gateway.orchestrator.sync("practice_a", Resource.APPOINTMENT, full=True)
    assert report.inserted == 1
    assert gateway.all(PendingRecord)[0].status == PendingStatus.RESOLVED
