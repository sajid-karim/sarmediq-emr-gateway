"""Database schema. See docs/ARCHITECTURE.md §8 for the reasoning behind each table."""

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.clock import utcnow
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


class UTCDateTime(TypeDecorator):
    """Stores naive UTC (SQLite has no timezone type) and always returns aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSON,
        list[Any]: JSON,
        datetime: UTCDateTime,
        date: Date,
    }


# --- tenants -------------------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenant"
    __table_args__ = (UniqueConstraint("vendor", "external_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100), unique=True)
    external_key: Mapped[str] = mapped_column(String(255))
    locator: Mapped[dict[str, Any]]
    credentials_ref: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# --- evidence and processing ---------------------------------------------------------


class RawPayload(Base):
    __tablename__ = "raw_payload"
    __table_args__ = (Index("ix_raw_payload_lookup", "vendor", "tenant_id", "resource", "sha256"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    resource: Mapped[str] = mapped_column(String(50))
    operation: Mapped[str] = mapped_column(String(20), default="list")
    endpoint: Mapped[str] = mapped_column(String(1000))
    cursor: Mapped[str | None] = mapped_column(String(500))
    http_status: Mapped[int]
    content_type: Mapped[str | None] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    body: Mapped[bytes] = mapped_column(LargeBinary)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)


class RecordVersion(Base):
    __tablename__ = "record_version"
    __table_args__ = (UniqueConstraint("resource", "record_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    resource: Mapped[str] = mapped_column(String(50))
    record_id: Mapped[int]
    version: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    diff: Mapped[dict[str, Any] | None]
    raw_payload_id: Mapped[int] = mapped_column(ForeignKey("raw_payload.id"))
    mapping_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProcessingLedger(Base):
    __tablename__ = "processing_ledger"
    __table_args__ = (UniqueConstraint("raw_payload_id", "record_key", "mapping_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_payload_id: Mapped[int] = mapped_column(ForeignKey("raw_payload.id"))
    record_key: Mapped[str] = mapped_column(String(200))
    mapping_version: Mapped[int]
    outcome: Mapped[str] = mapped_column(String(50))
    processed_at: Mapped[datetime] = mapped_column(default=utcnow)


# --- canonical clinical tables ---------------------------------------------------------


class CanonicalRow:
    """Columns every canonical table has. Natural key: (vendor, tenant_id, vendor_id)."""

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    vendor_id: Mapped[str] = mapped_column(String(100))
    content_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int]
    mapping_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class PatientRow(CanonicalRow, Base):
    __tablename__ = "patient"
    __table_args__ = (
        UniqueConstraint("vendor", "tenant_id", "vendor_id"),
        Index("ix_patient_tenant_vendor_id", "tenant_id", "vendor_id"),  # keyset paging
    )

    mrn: Mapped[str] = mapped_column(String(100))
    dob: Mapped[date]
    family_name: Mapped[str] = mapped_column(String(200))
    given_name: Mapped[str] = mapped_column(String(200))
    middle_name: Mapped[str | None] = mapped_column(String(200))
    sex: Mapped[str] = mapped_column(String(20))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(500))
    language: Mapped[str | None] = mapped_column(String(20))


class AppointmentRow(CanonicalRow, Base):
    __tablename__ = "appointment"
    __table_args__ = (
        UniqueConstraint("vendor", "tenant_id", "vendor_id"),
        Index("ix_appointment_tenant_vendor_id", "tenant_id", "vendor_id"),  # keyset paging
    )

    patient_id: Mapped[int] = mapped_column(ForeignKey("patient.id"))
    vendor_patient_id: Mapped[str] = mapped_column(String(100))
    start_time: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(30))
    provider_id: Mapped[str | None] = mapped_column(String(100))
    location: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(500))


class ClinicalReportRow(CanonicalRow, Base):
    __tablename__ = "clinical_report"
    __table_args__ = (
        UniqueConstraint("vendor", "tenant_id", "vendor_id"),
        Index("ix_clinical_report_tenant_vendor_id", "tenant_id", "vendor_id"),  # keyset paging
    )

    patient_id: Mapped[int] = mapped_column(ForeignKey("patient.id"))
    vendor_patient_id: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    body: Mapped[str] = mapped_column(Text)
    units: Mapped[str] = mapped_column(String(50))
    author: Mapped[str | None] = mapped_column(String(200))
    issued_at: Mapped[datetime | None]
    summary: Mapped[str | None] = mapped_column(Text)


TABLE_BY_RESOURCE: dict[Resource, type[CanonicalRow]] = {
    Resource.PATIENT: PatientRow,
    Resource.APPOINTMENT: AppointmentRow,
    Resource.CLINICAL_REPORT: ClinicalReportRow,
}


# --- failure handling ------------------------------------------------------------------


class Incident(Base):
    __tablename__ = "incident"
    __table_args__ = (Index("ix_incident_stream", "tenant_id", "resource", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_key: Mapped[str] = mapped_column(String(500), unique=True)
    vendor: Mapped[str] = mapped_column(String(50))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    resource: Mapped[str] = mapped_column(String(50))
    error_code: Mapped[str] = mapped_column(String(50))
    field_path: Mapped[str] = mapped_column(String(200), default="")
    severity: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=IncidentStatus.OPEN, index=True)
    count: Mapped[int] = mapped_column(default=0)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)
    sample_payload_ids: Mapped[list[Any]] = mapped_column(default=list)
    detail: Mapped[dict[str, Any] | None]
    resolved_at: Mapped[datetime | None]


quarantine_incident = Table(
    "quarantine_incident",
    Base.metadata,
    Column("quarantine_id", ForeignKey("quarantine.id"), primary_key=True),
    Column("incident_id", ForeignKey("incident.id"), primary_key=True),
)


class Quarantine(Base):
    __tablename__ = "quarantine"
    __table_args__ = (
        Index("ix_quarantine_stream", "tenant_id", "resource", "status"),
        Index("ix_quarantine_record", "tenant_id", "resource", "vendor_record_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    resource: Mapped[str] = mapped_column(String(50))
    operation: Mapped[str] = mapped_column(String(20), default="list")
    scope: Mapped[str] = mapped_column(String(10), default=QuarantineScope.RECORD)
    raw_payload_id: Mapped[int] = mapped_column(ForeignKey("raw_payload.id"))
    record_index: Mapped[int | None]
    vendor_record_id: Mapped[str | None] = mapped_column(String(100))
    error_codes: Mapped[list[Any]] = mapped_column(default=list)
    detail: Mapped[list[Any]] = mapped_column(default=list)
    fingerprint_diff: Mapped[dict[str, Any] | None]
    mapping_version: Mapped[int]
    status: Mapped[str] = mapped_column(
        String(20), default=QuarantineStatus.QUARANTINED, index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)
    resolution_note: Mapped[str | None] = mapped_column(String(500))

    incidents: Mapped[list[Incident]] = relationship(secondary=quarantine_incident)


class PendingRecord(Base):
    __tablename__ = "pending_record"
    __table_args__ = (
        UniqueConstraint("vendor", "tenant_id", "resource", "vendor_id"),
        Index("ix_pending_waiting", "tenant_id", "waiting_on_resource", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    resource: Mapped[str] = mapped_column(String(50))
    vendor_id: Mapped[str] = mapped_column(String(100))
    raw_payload_id: Mapped[int] = mapped_column(ForeignKey("raw_payload.id"))
    record_index: Mapped[int]
    waiting_on_resource: Mapped[str] = mapped_column(String(50))
    waiting_on_vendor_id: Mapped[str] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default=PendingStatus.WAITING)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class RequestDLQ(Base):
    __tablename__ = "request_dlq"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    resource: Mapped[str] = mapped_column(String(50))
    operation: Mapped[str] = mapped_column(String(20), default="list")
    cursor: Mapped[str | None] = mapped_column(String(500))
    failure_code: Mapped[str] = mapped_column(String(50))
    http_status: Mapped[int | None]
    attempts: Mapped[int]
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("raw_payload.id"))
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incident.id"))
    detail: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default=DlqStatus.OPEN, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None]


# --- drift ------------------------------------------------------------------------------


class SchemaFingerprint(Base):
    __tablename__ = "schema_fingerprint"
    __table_args__ = (UniqueConstraint("vendor", "resource", "hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50))
    resource: Mapped[str] = mapped_column(String(50))
    hash: Mapped[str] = mapped_column(String(80))
    field_types: Mapped[dict[str, Any]]
    is_baseline: Mapped[bool] = mapped_column(default=False)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)
    promoted_at: Mapped[datetime | None]


class DriftEvent(Base):
    __tablename__ = "drift_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint_id: Mapped[int] = mapped_column(ForeignKey("schema_fingerprint.id"), unique=True)
    vendor: Mapped[str] = mapped_column(String(50))
    resource: Mapped[str] = mapped_column(String(50))
    first_seen_tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    diff: Mapped[dict[str, Any]]
    raw_payload_id: Mapped[int] = mapped_column(ForeignKey("raw_payload.id"))
    status: Mapped[str] = mapped_column(String(20), default=DriftStatus.NEW, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    fingerprint: Mapped[SchemaFingerprint] = relationship()


# --- sync state -------------------------------------------------------------------------


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoint"
    __table_args__ = (UniqueConstraint("vendor", "tenant_id", "resource"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    resource: Mapped[str] = mapped_column(String(50))
    cursor: Mapped[str | None] = mapped_column(String(500))
    run_id: Mapped[str | None] = mapped_column(String(40))
    heartbeat_at: Mapped[datetime | None]
    run_started_at: Mapped[datetime | None]
    run_updated_since: Mapped[datetime | None]
    last_success_started_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class CircuitState(Base):
    __tablename__ = "circuit_state"
    __table_args__ = (UniqueConstraint("vendor", "tenant_id", "resource"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor: Mapped[str] = mapped_column(String(50))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    resource: Mapped[str] = mapped_column(String(50))
    state: Mapped[str] = mapped_column(String(20), default=BreakerState.CLOSED)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    opened_at: Mapped[datetime | None]
    reason_code: Mapped[str | None] = mapped_column(String(50))
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incident.id"))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
