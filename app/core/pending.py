"""Records parked until their parent arrives (e.g. an appointment before its patient)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.types import PendingStatus
from app.db.models import PendingRecord


def park(
    session: Session,
    *,
    vendor: str,
    tenant_id: int,
    resource: str,
    vendor_id: str,
    raw_payload_id: int,
    record_index: int,
    waiting_on_resource: str,
    waiting_on_vendor_id: str,
) -> PendingRecord:
    row = session.scalar(
        select(PendingRecord).where(
            PendingRecord.vendor == vendor,
            PendingRecord.tenant_id == tenant_id,
            PendingRecord.resource == resource,
            PendingRecord.vendor_id == vendor_id,
        )
    )
    if row is None:
        row = PendingRecord(
            vendor=vendor,
            tenant_id=tenant_id,
            resource=resource,
            vendor_id=vendor_id,
            attempts=0,
        )
        session.add(row)
    elif row.status != PendingStatus.WAITING:
        row.status = PendingStatus.WAITING
        row.attempts = 0
    row.raw_payload_id = raw_payload_id
    row.record_index = record_index
    row.waiting_on_resource = waiting_on_resource
    row.waiting_on_vendor_id = waiting_on_vendor_id
    row.updated_at = utcnow()
    session.flush()
    return row


def resolve_pending(
    session: Session, *, vendor: str, tenant_id: int, resource: str, vendor_id: str
) -> None:
    row = session.scalar(
        select(PendingRecord).where(
            PendingRecord.vendor == vendor,
            PendingRecord.tenant_id == tenant_id,
            PendingRecord.resource == resource,
            PendingRecord.vendor_id == vendor_id,
            PendingRecord.status == PendingStatus.WAITING,
        )
    )
    if row is not None:
        row.status = PendingStatus.RESOLVED
        row.updated_at = utcnow()


def waiting_on(
    session: Session, *, vendor: str, tenant_id: int, parent_resource: str
) -> list[PendingRecord]:
    return list(
        session.scalars(
            select(PendingRecord)
            .where(
                PendingRecord.vendor == vendor,
                PendingRecord.tenant_id == tenant_id,
                PendingRecord.waiting_on_resource == parent_resource,
                PendingRecord.status == PendingStatus.WAITING,
            )
            .order_by(PendingRecord.id)
        ).all()
    )
