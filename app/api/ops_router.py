"""What an on-call engineer uses: health, incidents, drift, quarantine, DLQ, replay, breaker."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

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
    status: str = Query("open", pattern="^(open|closed|all)$"),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(Incident).order_by(Incident.id)
    if status != "all":
        query = query.where(Incident.status == status)
    return [row_to_dict(row) for row in session.scalars(query)]


@router.get("/drift")
def list_drift(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [row_to_dict(row) for row in session.scalars(select(DriftEvent).order_by(DriftEvent.id))]


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
    status: str = Query(QuarantineStatus.QUARANTINED),
    resource: Resource | None = None,
    tenant: str | None = None,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(Quarantine).where(Quarantine.status == status).order_by(Quarantine.id)
    if resource:
        query = query.where(Quarantine.resource == resource)
    if tenant:
        query = query.where(Quarantine.tenant_id == tenant_id_for(session, tenant))
    rows = []
    for row in session.scalars(query):
        item = row_to_dict(row)
        item["incident_ids"] = [incident.id for incident in row.incidents]
        rows.append(item)
    return rows


@router.get("/dlq")
def list_dlq(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [row_to_dict(row) for row in session.scalars(select(RequestDLQ).order_by(RequestDLQ.id))]


@router.post("/replay")
def replay(
    scope: QuarantineScope = QuarantineScope.RECORD,
    tenant: str | None = None,
    resource: Resource | None = None,
    error_code: ErrorCode | None = None,
    services=Depends(get_services),
) -> dict[str, Any]:
    try:
        report = services.orchestrator.replay(
            scope=scope, tenant_name=tenant, resource=resource, error_code=error_code
        )
    except UnknownTenantError as exc:
        raise HTTPException(status_code=404, detail=f"unknown tenant {exc}") from exc
    return report.as_dict()


@router.post("/breaker/reset")
def reset_breaker(
    tenant: str, resource: Resource, session: Session = Depends(get_session)
) -> dict[str, Any]:
    tenant_id = tenant_id_for(session, tenant)
    vendor = session.scalar(select(Tenant.vendor).where(Tenant.id == tenant_id))
    assert vendor is not None
    state = breaker.get_or_create(session, vendor, tenant_id, resource)
    breaker.reset(state, "manual reset")
    session.commit()
    return {"tenant": tenant, "resource": resource, "state": state.state}
