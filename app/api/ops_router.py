"""What an on-call engineer uses: health, incidents, drift, quarantine, DLQ, replay, breaker."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_services, get_session, row_to_dict, tenant_id_for
from app.core import breaker, drift
from app.core.errors import ErrorCode
from app.core.types import (
    BreakerState,
    DlqStatus,
    DriftStatus,
    IncidentStatus,
    PendingStatus,
    QuarantineScope,
    QuarantineStatus,
    Resource,
)
from app.db.models import (
    CircuitState,
    DriftEvent,
    Incident,
    PendingRecord,
    Quarantine,
    RequestDLQ,
    Tenant,
)
from app.sync.orchestrator import UnknownTenantError

router = APIRouter(tags=["ops"])

# Lists page with keyset pagination on the primary key, like the read API:
# `?after=<last id seen>`, next cursor in `X-Next-Cursor`. These tables grow with every incident
# and quarantined record, so an unbounded list would get slower and bigger over time.
Limit = Query(100, ge=1, le=1000)
After = Query(None, description="X-Next-Cursor from the previous page")


def _page(
    session: Session, query: Any, model: Any, response: Response, limit: int, after: int | None
) -> list[Any]:
    if after is not None:
        query = query.where(model.id > after)
    # One extra row tells us whether a next page exists, without a COUNT query.
    rows = list(session.scalars(query.order_by(model.id).limit(limit + 1)))
    if len(rows) > limit:
        response.headers["X-Next-Cursor"] = str(rows[limit - 1].id)
    return rows[:limit]


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, Any]:
    session.execute(text("SELECT 1"))
    names = dict(session.execute(select(Tenant.id, Tenant.name)).all())
    breakers = [
        {
            "tenant": names.get(state.tenant_id),
            "resource": state.resource,
            "state": state.state,
            "reason": state.reason_code,
            "incident_id": state.incident_id,
        }
        for state in session.scalars(select(CircuitState).order_by(CircuitState.id))
    ]
    open_incidents = dict(
        session.execute(
            select(Incident.severity, func.count())
            .where(Incident.status == IncidentStatus.OPEN)
            .group_by(Incident.severity)
        ).all()
    )

    def count(model: Any, *where: Any) -> int:
        return session.scalar(select(func.count()).select_from(model).where(*where)) or 0

    any_open = any(b["state"] != BreakerState.CLOSED for b in breakers)
    return {
        "status": "degraded" if any_open or open_incidents.get("critical") else "ok",
        "breakers": breakers,
        "open_incidents": open_incidents,
        "quarantined_records": count(Quarantine, Quarantine.status == QuarantineStatus.QUARANTINED),
        "pending_records": count(PendingRecord, PendingRecord.status == PendingStatus.WAITING),
        "open_request_dlq": count(RequestDLQ, RequestDLQ.status == DlqStatus.OPEN),
        "new_drift_events": count(DriftEvent, DriftEvent.status == DriftStatus.NEW),
    }


@router.get("/incidents")
def list_incidents(
    response: Response,
    status: str = Query("open", pattern="^(open|closed|all)$"),
    limit: int = Limit,
    after: int | None = After,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(Incident)
    if status != "all":
        query = query.where(Incident.status == status)
    return [row_to_dict(row) for row in _page(session, query, Incident, response, limit, after)]


@router.get("/drift")
def list_drift(
    response: Response,
    limit: int = Limit,
    after: int | None = After,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = _page(session, select(DriftEvent), DriftEvent, response, limit, after)
    return [row_to_dict(row) for row in rows]


@router.post("/drift/{event_id}/promote")
def promote_drift(event_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    event = session.get(DriftEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="drift event not found")
    drift.promote(session, event)
    session.commit()
    return row_to_dict(event)


@router.get("/quarantine")
def list_quarantine(
    response: Response,
    status: QuarantineStatus = QuarantineStatus.QUARANTINED,
    resource: Resource | None = None,
    tenant: str | None = None,
    limit: int = Limit,
    after: int | None = After,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = (
        select(Quarantine)
        .where(Quarantine.status == status)
        # Load every row's incidents in one extra query, not one query per row (N+1).
        .options(selectinload(Quarantine.incidents))
    )
    if resource:
        query = query.where(Quarantine.resource == resource)
    if tenant:
        query = query.where(Quarantine.tenant_id == tenant_id_for(session, tenant))
    rows = []
    for row in _page(session, query, Quarantine, response, limit, after):
        item = row_to_dict(row)
        item["incident_ids"] = [incident.id for incident in row.incidents]
        rows.append(item)
    return rows


@router.get("/dlq")
def list_dlq(
    response: Response,
    limit: int = Limit,
    after: int | None = After,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = _page(session, select(RequestDLQ), RequestDLQ, response, limit, after)
    return [row_to_dict(row) for row in rows]


@router.post("/replay")
def replay(
    scope: QuarantineScope = QuarantineScope.RECORD,
    tenant: str | None = None,
    vendor: str | None = None,
    resource: Resource | None = None,
    error_code: ErrorCode | None = None,
    services=Depends(get_services),
) -> dict[str, Any]:
    """Replay runs with one vendor's mapper, so it must know which vendor is meant."""
    try:
        if tenant:
            orchestrator = services.orchestrator_for(tenant)
        elif vendor:
            if vendor not in services.orchestrators:
                raise HTTPException(status_code=404, detail=f"unknown vendor {vendor!r}")
            orchestrator = services.orchestrators[vendor]
        elif len(services.orchestrators) == 1:
            [orchestrator] = services.orchestrators.values()
        else:
            raise HTTPException(
                status_code=422, detail="several vendors are configured: pass tenant or vendor"
            )
        report = orchestrator.replay(
            scope=scope, tenant_name=tenant, resource=resource, error_code=error_code
        )
    except UnknownTenantError as exc:
        raise HTTPException(status_code=404, detail=f"unknown tenant {exc}") from exc
    return report.as_dict()


@router.post("/breaker/reset")
def reset_breaker(
    tenant: str, resource: Resource, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = session.scalar(select(Tenant).where(Tenant.name == tenant))
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown tenant {tenant!r}")
    state = breaker.get_or_create(session, row.vendor, row.id, resource)
    breaker.reset(state, "manual reset")
    session.commit()
    return {"tenant": tenant, "resource": resource, "state": state.state}
