from datetime import timedelta

from app.core import breaker
from app.core.clock import utcnow
from app.core.errors import ErrorCode
from app.core.types import BreakerState
from app.db.models import CircuitState

COOLDOWN = 300


def fresh() -> CircuitState:
    return CircuitState(
        vendor="acme",
        tenant_id=1,
        resource="patient",
        state=BreakerState.CLOSED,
        consecutive_failures=0,
    )


def test_transient_failures_trip_after_threshold():
    state = fresh()
    assert not breaker.record_failure(state, ErrorCode.UPSTREAM_UNAVAILABLE, None, threshold=3)
    assert not breaker.record_failure(state, ErrorCode.UPSTREAM_UNAVAILABLE, None, threshold=3)
    assert breaker.record_failure(state, ErrorCode.UPSTREAM_UNAVAILABLE, 7, threshold=3)
    assert state.state == BreakerState.OPEN and state.incident_id == 7


def test_success_resets_the_failure_count():
    state = fresh()
    breaker.record_failure(state, ErrorCode.UPSTREAM_UNAVAILABLE, None, threshold=3)
    breaker.record_success(state)
    assert state.consecutive_failures == 0 and state.state == BreakerState.CLOSED


def test_open_blocks_until_cooldown_then_allows_one_probe():
    state = fresh()
    breaker.trip(state, ErrorCode.ENVELOPE_CHANGED, 1)
    now = state.opened_at
    assert not breaker.allow_run(state, now + timedelta(seconds=10), COOLDOWN)
    assert breaker.seconds_until_probe(state, now + timedelta(seconds=10), COOLDOWN) == 290
    assert breaker.allow_run(state, now + timedelta(seconds=COOLDOWN), COOLDOWN)
    assert state.state == BreakerState.HALF_OPEN


def test_probe_success_closes():
    state = fresh()
    breaker.trip(state, ErrorCode.BATCH_THRESHOLD, 1)
    breaker.allow_run(state, utcnow() + timedelta(seconds=COOLDOWN + 1), COOLDOWN)
    breaker.record_success(state)
    assert state.state == BreakerState.CLOSED and state.reason_code is None


def test_probe_failure_reopens_immediately_even_below_threshold():
    state = fresh()
    breaker.trip(state, ErrorCode.UPSTREAM_UNAVAILABLE, 1)
    breaker.allow_run(state, utcnow() + timedelta(seconds=COOLDOWN + 1), COOLDOWN)
    state.consecutive_failures = 0
    assert breaker.record_failure(state, ErrorCode.UPSTREAM_UNAVAILABLE, 2, threshold=3)
    assert state.state == BreakerState.OPEN


def test_closed_and_half_open_always_allow():
    state = fresh()
    assert breaker.allow_run(state, utcnow(), COOLDOWN)
    state.state = BreakerState.HALF_OPEN
    assert breaker.allow_run(state, utcnow(), COOLDOWN)
