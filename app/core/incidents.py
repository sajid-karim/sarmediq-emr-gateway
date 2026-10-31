"""Deduplicated incidents: one row per (vendor, tenant, resource, error_code, field_path)."""

from collections.abc import Iterable
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import ErrorCode
from app.core.types import IncidentStatus, QuarantineStatus, Severity
from app.db.models import Incident, Quarantine, quarantine_incident

log = structlog.get_logger("incidents")

MAX_SAMPLES = 5


def group_key(vendor: str, tenant_id: int, resource: str, code: ErrorCode, field_path: str) -> str:
    return f"{vendor}|{tenant_id}|{resource}|{code}|{field_path}"


def record_incident(
    session: Session,
    *,
    vendor: str,
    tenant_id: int,
    resource: str,
    code: ErrorCode,
    severity: Severity,
    field_path: str = "",
    raw_payload_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> Incident:
    key = group_key(vendor, tenant_id, resource, code, field_path)
    now = utcnow()
    incident = session.scalar(select(Incident).where(Incident.group_key == key))
    if incident is None:
        incident = Incident(
            group_key=key,
            vendor=vendor,
            tenant_id=tenant_id,
            resource=resource,
            error_code=code,
            field_path=field_path,
            severity=severity,
            status=IncidentStatus.OPEN,
            count=0,
            first_seen=now,
            sample_payload_ids=[],
            detail=detail,
        )
        session.add(incident)
        log.warning(
            "incident.opened", code=code, field=field_path, resource=resource, severity=severity
        )
    elif incident.status == IncidentStatus.CLOSED:
        incident.status = IncidentStatus.OPEN
        incident.resolved_at = None
        log.warning("incident.reopened", code=code, field=field_path, resource=resource)

    incident.count += 1
    incident.last_seen = now
    if detail is not None:
        incident.detail = detail
    samples = list(incident.sample_payload_ids or [])
    if raw_payload_id is not None and raw_payload_id not in samples and len(samples) < MAX_SAMPLES:
        incident.sample_payload_ids = [*samples, raw_payload_id]
    session.flush()
    return incident


def close_incident(incident: Incident, reason: str) -> None:
    if incident.status != IncidentStatus.CLOSED:
        incident.status = IncidentStatus.CLOSED
        incident.resolved_at = utcnow()
        log.info("incident.closed", incident_id=incident.id, reason=reason)


def close_if_drained(session: Session, incident_ids: Iterable[int]) -> list[int]:
    """Close incidents that no longer have any quarantined record attached."""
    closed = []
    for incident_id in set(incident_ids):
        open_rows = session.scalar(
            select(func.count())
            .select_from(quarantine_incident)
            .join(Quarantine, Quarantine.id == quarantine_incident.c.quarantine_id)
            .where(
                quarantine_incident.c.incident_id == incident_id,
                Quarantine.status == QuarantineStatus.QUARANTINED,
            )
        )
        incident = session.get(Incident, incident_id)
        if incident is not None and open_rows == 0 and incident.status == IncidentStatus.OPEN:
            close_incident(incident, "no quarantined records left")
            closed.append(incident_id)
    return closed
