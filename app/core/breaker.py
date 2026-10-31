"""Circuit breaker per (vendor, tenant, resource), persisted so restarts don't forget (§5).

Trips on HTTP failures *and* on data failures (envelope change, batch threshold, loops):
a vendor returning 200 with garbage is still broken. HALF_OPEN probing is documented,
not built; an OPEN breaker is closed by a successful replay or a manual reset.
"""

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import ErrorCode
from app.core.types import BreakerState
from app.db.models import CircuitState

log = structlog.get_logger("breaker")


def get_or_create(session: Session, vendor: str, tenant_id: int, resource: str) -> CircuitState:
    query = select(CircuitState).where(
        CircuitState.vendor == vendor,
        CircuitState.tenant_id == tenant_id,
        CircuitState.resource == resource,
    )
    state = session.scalar(query)
    if state is not None:
        return state
    try:
        with session.begin_nested():
            state = CircuitState(vendor=vendor, tenant_id=tenant_id, resource=resource)
            session.add(state)
    except IntegrityError:
        state = session.scalar(query)
    assert state is not None
    return state


def is_open(state: CircuitState) -> bool:
    return state.state == BreakerState.OPEN


def trip(state: CircuitState, code: ErrorCode, incident_id: int | None) -> None:
    state.state = BreakerState.OPEN
    state.opened_at = utcnow()
    state.reason_code = code
    state.incident_id = incident_id
    log.error("breaker.opened", resource=state.resource, tenant_id=state.tenant_id, reason=code)


def record_failure(
    state: CircuitState, code: ErrorCode, incident_id: int | None, threshold: int
) -> bool:
    """Transient failures trip the breaker only after `threshold` consecutive failed runs."""
    state.consecutive_failures += 1
    if state.consecutive_failures >= threshold:
        trip(state, code, incident_id)
        return True
    return False


def record_success(state: CircuitState) -> None:
    state.consecutive_failures = 0


def reset(state: CircuitState, reason: str) -> None:
    if state.state != BreakerState.CLOSED:
        log.info(
            "breaker.closed", resource=state.resource, tenant_id=state.tenant_id, reason=reason
        )
    state.state = BreakerState.CLOSED
    state.consecutive_failures = 0
    state.opened_at = None
    state.reason_code = None
    state.incident_id = None
