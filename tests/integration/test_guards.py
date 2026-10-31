"""Run lock and breaker reset."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from app.core.clock import utcnow
from app.core.types import BreakerState, Resource
from app.db.models import CircuitState, SyncCheckpoint
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
