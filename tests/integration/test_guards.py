"""Run lock and breaker reset."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from app.core.clock import utcnow
from app.core.errors import ErrorCode
from app.core.types import BreakerState, Resource
from app.db.models import CircuitState, Incident, SyncCheckpoint
from app.sync.orchestrator import SyncInProgressError, SyncStatus


def hold_lock(gateway, heartbeat):
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)  # creates the checkpoint row
    with gateway.sessions() as session:
        session.execute(
            update(SyncCheckpoint).values(run_id="someone-else", heartbeat_at=heartbeat)
        )
        session.commit()


def test_second_concurrent_sync_is_refused(gateway):
    hold_lock(gateway, utcnow())
    with pytest.raises(SyncInProgressError):
        gateway.orchestrator.sync("practice_a", Resource.PATIENT)


def test_stale_lock_from_a_dead_worker_is_taken_over(gateway):
    hold_lock(gateway, utcnow() - timedelta(hours=1))
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert report.status is SyncStatus.COMPLETED


def test_other_tenants_are_not_locked(gateway):
    hold_lock(gateway, utcnow())
    with gateway.sessions() as session:
        session.execute(update(SyncCheckpoint).values(run_id=None))
        session.commit()
    hold_lock(gateway, utcnow())
    assert gateway.orchestrator.sync("practice_b", Resource.PATIENT).status is SyncStatus.COMPLETED


def test_open_breaker_blocks_until_reset(gateway):
    gateway.scenario("pagination")
    gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    gateway.scenario("v1")
    assert gateway.orchestrator.sync("practice_a", Resource.PATIENT).status is SyncStatus.BLOCKED

    with gateway.sessions() as session:
        session.execute(update(CircuitState).values(state=BreakerState.CLOSED))
        session.commit()
    assert gateway.orchestrator.sync("practice_a", Resource.PATIENT).status is SyncStatus.COMPLETED


def test_run_that_lost_its_lock_cannot_move_the_checkpoint(gateway, monkeypatch):
    """Fencing: a run whose stale lock was taken over must not overwrite the new owner's cursor."""
    adapter = gateway.orchestrator.adapter
    real_fetch = adapter.fetch_page
    calls = []

    def fetch_then_lose_lock(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:  # while fetching page 2, another run takes over the stream
            with gateway.sessions() as session:
                session.execute(update(SyncCheckpoint).values(run_id="new-owner"))
                session.commit()
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(adapter, "fetch_page", fetch_then_lose_lock)
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)

    assert report.status is SyncStatus.HALTED and "lock lost" in report.halt_reason
    assert len(calls) == 2, "stopped at the first fenced write, page 3 never fetched"
    with gateway.sessions() as session:
        checkpoint = session.query(SyncCheckpoint).one()
        assert checkpoint.run_id == "new-owner", "the new owner's lock is left alone"
        assert checkpoint.last_success_started_at is None, "run was not marked successful"
        first_page_cursor = checkpoint.cursor
    assert first_page_cursor is not None, "cursor still points after page 1, not page 2"


def test_unexpected_error_opens_an_incident_and_counts_a_breaker_failure(gateway, monkeypatch):
    adapter = gateway.orchestrator.adapter

    def boom(*args, **kwargs):
        raise RuntimeError("bug in the mapper")

    monkeypatch.setattr(adapter, "map_record", boom)
    with pytest.raises(RuntimeError, match="bug in the mapper"):
        gateway.orchestrator.sync("practice_a", Resource.PATIENT)

    with gateway.sessions() as session:
        [incident] = session.query(Incident).filter_by(error_code=ErrorCode.INTERNAL_ERROR).all()
        assert incident.status == "open"
        assert session.query(CircuitState).one().consecutive_failures == 1
        assert session.query(SyncCheckpoint).one().run_id is None, "lock released"

    monkeypatch.undo()
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert report.status is SyncStatus.COMPLETED
    with gateway.sessions() as session:
        assert session.get(Incident, incident.id).status == "closed", "a good run closes it"
        assert session.query(CircuitState).one().consecutive_failures == 0
