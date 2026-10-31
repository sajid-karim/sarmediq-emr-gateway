"""Quarantine: data we refused to write, kept with full diagnostics for replay (§7)."""

from collections.abc import Sequence
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.types import QuarantineScope, QuarantineStatus
from app.db.models import Incident, Quarantine

log = structlog.get_logger("quarantine")


def quarantine_record(
    session: Session,
    *,
    vendor: str,
    tenant_id: int,
    resource: str,
    raw_payload_id: int,
    record_index: int,
    vendor_record_id: str | None,
    issues: Sequence[dict[str, Any]],
    mapping_version: int,
    fingerprint_diff: dict[str, Any] | None,
    incidents: Sequence[Incident],
) -> Quarantine:
    """One open row per broken record, not per fetch: a re-fetch updates the existing row."""
    query = select(Quarantine).where(
        Quarantine.vendor == vendor,
        Quarantine.tenant_id == tenant_id,
        Quarantine.resource == resource,
        Quarantine.scope == QuarantineScope.RECORD,
        Quarantine.status == QuarantineStatus.QUARANTINED,
    )
    if vendor_record_id is not None:
        query = query.where(Quarantine.vendor_record_id == vendor_record_id)
    else:
        query = query.where(
            Quarantine.raw_payload_id == raw_payload_id, Quarantine.record_index == record_index
        )
    row = session.scalar(query)
    now = utcnow()
    if row is None:
        row = Quarantine(
            vendor=vendor,
            tenant_id=tenant_id,
            resource=resource,
            scope=QuarantineScope.RECORD,
            vendor_record_id=vendor_record_id,
            first_seen=now,
            attempts=0,
        )
        session.add(row)
    row.raw_payload_id = raw_payload_id
    row.record_index = record_index
    row.error_codes = sorted({issue["code"] for issue in issues})
    row.detail = list(issues)
    row.mapping_version = mapping_version
    row.fingerprint_diff = fingerprint_diff
    row.last_seen = now
    for incident in incidents:
        if incident not in row.incidents:
            row.incidents.append(incident)
    session.flush()
    log.warning(
        "record.quarantined",
        resource=resource,
        vendor_record_id=vendor_record_id,
        codes=row.error_codes,
    )
    return row


def quarantine_page(
    session: Session,
    *,
    vendor: str,
    tenant_id: int,
    resource: str,
    raw_payload_id: int,
    issue: dict[str, Any],
    mapping_version: int,
    incident: Incident,
) -> Quarantine:
    row = Quarantine(
        vendor=vendor,
        tenant_id=tenant_id,
        resource=resource,
        scope=QuarantineScope.PAGE,
        raw_payload_id=raw_payload_id,
        record_index=None,
        error_codes=[issue["code"]],
        detail=[issue],
        mapping_version=mapping_version,
        attempts=0,
    )
    row.incidents.append(incident)
    session.add(row)
    session.flush()
    log.warning("page.quarantined", resource=resource, raw_payload_id=raw_payload_id)
    return row


def resolve(row: Quarantine, note: str) -> list[int]:
    row.status = QuarantineStatus.RESOLVED
    row.resolution_note = note
    row.last_seen = utcnow()
    return [incident.id for incident in row.incidents]


def resolve_superseded(
    session: Session, *, vendor: str, tenant_id: int, resource: str, vendor_record_id: str
) -> list[int]:
    """A valid version of a previously quarantined record arrived; the old failure is moot."""
    rows = session.scalars(
        select(Quarantine).where(
            Quarantine.vendor == vendor,
            Quarantine.tenant_id == tenant_id,
            Quarantine.resource == resource,
            Quarantine.vendor_record_id == vendor_record_id,
            Quarantine.status == QuarantineStatus.QUARANTINED,
        )
    ).all()
    incident_ids: list[int] = []
    for row in rows:
        incident_ids += resolve(row, "superseded by a valid record")
    return incident_ids
