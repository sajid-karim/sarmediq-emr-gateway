"""The sync pipeline (ARCHITECTURE §3) and replay (§7).

One run = one (tenant, resource). Per page:
  ① fetch with retry  ② store raw  ③ envelope check  ④ drift  ⑤ classify each record
  ⑥ idempotent write / ⑦ quarantine / pending  ⑧ batch health  ⑨ checkpoint
Every record is its own transaction; the checkpoint only moves after the page is committed.
"""

import hashlib
import json
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Any

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core import breaker, drift, incidents, pending, quarantine
from app.core.classifier import classify, is_batch_unhealthy
from app.core.clock import utcnow
from app.core.errors import ErrorCode
from app.core.idempotency import WriteOutcome, record_key, upsert_record
from app.core.retry import RetryPolicy, fetch_with_retry
from app.core.types import (
    BreakerState,
    DlqStatus,
    IncidentStatus,
    Outcome,
    PendingStatus,
    QuarantineScope,
    QuarantineStatus,
    Resource,
    Severity,
)
from app.db.models import (
    TABLE_BY_RESOURCE,
    Incident,
    ProcessingLedger,
    Quarantine,
    RawPayload,
    RequestDLQ,
    SyncCheckpoint,
    Tenant,
)
from app.vendors.base import (
    Criticality,
    EnvelopeError,
    MappingIssue,
    RawResponse,
    TenantRef,
    VendorAdapter,
)

log = structlog.get_logger("sync")

ISSUE_SEVERITY = {
    Criticality.CRITICAL: Severity.CRITICAL,
    Criticality.IMPORTANT: Severity.WARNING,
    Criticality.OPTIONAL: Severity.INFO,
}

# Incidents that describe a run-level halt; replay that drains the quarantine closes them.
RUN_LEVEL_CODES = (ErrorCode.ENVELOPE_CHANGED, ErrorCode.BATCH_THRESHOLD)


class SyncStatus(StrEnum):
    COMPLETED = "completed"
    HALTED = "halted"
    BLOCKED = "blocked"


class SyncInProgressError(Exception):
    pass


class UnknownTenantError(Exception):
    pass


@dataclass
class SyncReport:
    run_id: str
    tenant: str
    resource: str
    mapping_version: int
    status: SyncStatus = SyncStatus.COMPLETED
    halt_reason: str | None = None
    pages: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    degraded: int = 0
    pending: int = 0
    quarantined: int = 0
    retries: int = 0
    pending_resolved: int = 0
    drift_events: list[int] = field(default_factory=list)
    incidents: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def note_incident(self, incident_id: int) -> None:
        if incident_id not in self.incidents:
            self.incidents.append(incident_id)


@dataclass
class ReplayReport:
    scope: str
    mapping_version: int
    replayed: int = 0
    resolved: int = 0
    still_quarantined: int = 0
    moved_to_pending: int = 0
    incidents_closed: list[int] = field(default_factory=list)
    breakers_reset: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecordResult:
    outcome: Outcome
    write: WriteOutcome | None = None


class SyncOrchestrator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: VendorAdapter,
        settings: Settings,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._sessions = session_factory
        self.adapter = adapter
        self._settings = settings
        self._sleep = sleep
        self._clock = clock
        self._rand = rand
        self._policy = RetryPolicy(
            max_attempts=settings.max_attempts,
            backoff_base=settings.backoff_base,
            backoff_cap=settings.backoff_cap,
            retry_after_cap=settings.retry_after_cap,
        )

    # ------------------------------------------------------------------------------ sync

    def sync(self, tenant_name: str, resource: Resource, *, full: bool = False) -> SyncReport:
        run_id = uuid.uuid4().hex[:12]
        vendor = self.adapter.vendor
        with self._sessions() as session:
            tenant = self._load_tenant(session, tenant_name)
            report = SyncReport(run_id, tenant.name, resource, self.adapter.mapping_version)
            run_log = log.bind(run_id=run_id, tenant=tenant.name, resource=str(resource))

            state = breaker.get_or_create(session, vendor, tenant.id, resource)
            session.commit()
            if breaker.is_open(state):
                report.status = SyncStatus.BLOCKED
                report.halt_reason = f"circuit breaker OPEN ({state.reason_code})"
                if state.incident_id:
                    report.note_incident(state.incident_id)
                run_log.warning("sync.blocked", reason=state.reason_code)
                return report

            checkpoint = self._acquire_lock(session, tenant, resource, run_id, full)
            run_log.info("sync.started", resume_cursor=checkpoint.cursor, full=full)
            try:
                self._run_pages(session, tenant, resource, checkpoint, report, run_log)
            finally:
                session.rollback()
                session.execute(
                    update(SyncCheckpoint)
                    .where(SyncCheckpoint.id == checkpoint.id, SyncCheckpoint.run_id == run_id)
                    .values(run_id=None, heartbeat_at=None)
                )
                session.commit()

            if report.status is SyncStatus.COMPLETED and resource is Resource.PATIENT:
                report.pending_resolved = self._retry_pending(session, tenant, report)
            run_log.info("sync.finished", **_summary(report))
        return report

    def _load_tenant(self, session: Session, name: str) -> TenantRef:
        tenant = session.scalar(
            select(Tenant).where(
                Tenant.name == name, Tenant.vendor == self.adapter.vendor, Tenant.active.is_(True)
            )
        )
        if tenant is None:
            raise UnknownTenantError(name)
        return TenantRef(tenant.id, tenant.name, tenant.vendor, dict(tenant.locator))

    def _acquire_lock(
        self, session: Session, tenant: TenantRef, resource: Resource, run_id: str, full: bool
    ) -> SyncCheckpoint:
        query = select(SyncCheckpoint).where(
            SyncCheckpoint.vendor == tenant.vendor,
            SyncCheckpoint.tenant_id == tenant.id,
            SyncCheckpoint.resource == resource,
        )
        checkpoint = session.scalar(query)
        if checkpoint is None:
            try:
                with session.begin_nested():
                    checkpoint = SyncCheckpoint(
                        vendor=tenant.vendor, tenant_id=tenant.id, resource=resource
                    )
                    session.add(checkpoint)
            except IntegrityError:
                checkpoint = session.scalar(query)
            session.commit()
        assert checkpoint is not None

        now = self._clock()
        stale_before = now - timedelta(seconds=self._settings.sync_lock_ttl_seconds)
        # Conditional UPDATE = compare-and-set: only one concurrent caller can win the lock.
        won = session.execute(
            update(SyncCheckpoint)
            .where(
                SyncCheckpoint.id == checkpoint.id,
                or_(SyncCheckpoint.run_id.is_(None), SyncCheckpoint.heartbeat_at < stale_before),
            )
            .values(run_id=run_id, heartbeat_at=now)
            .execution_options(synchronize_session=False)
        )
        session.commit()
        if won.rowcount != 1:
            raise SyncInProgressError(f"{tenant.name}/{resource} is already syncing")
        session.refresh(checkpoint)

        if full or checkpoint.cursor is None:
            checkpoint.cursor = None
            checkpoint.run_started_at = now
            checkpoint.run_updated_since = None if full else checkpoint.last_success_started_at
        session.commit()
        return checkpoint

    def _run_pages(
        self,
        session: Session,
        tenant: TenantRef,
        resource: Resource,
        checkpoint: SyncCheckpoint,
        report: SyncReport,
        run_log: Any,
    ) -> None:
        vendor = self.adapter.vendor
        cursor = checkpoint.cursor
        seen_cursors = {cursor} if cursor else set()

        while True:
            fetched = fetch_with_retry(
                partial(
                    self.adapter.fetch_page, tenant, resource, cursor, checkpoint.run_updated_since
                ),
                partial(self._store_raw, session, tenant, resource, cursor),
                self._policy,
                sleep=self._sleep,
                now=self._clock,
                rand=self._rand,
                on_retry=partial(_note_retry, report, run_log),
            )
            if not fetched.ok:
                self._handle_fetch_failure(session, tenant, resource, cursor, fetched, report)
                return
            self._close_dlq(session, tenant, resource, cursor)
            raw_payload_id = fetched.raw_payload_id
            assert raw_payload_id is not None and fetched.response is not None

            try:
                page = self.adapter.parse_page(resource, fetched.payload, fetched.response.headers)
            except EnvelopeError as exc:
                self._handle_envelope_change(session, tenant, resource, raw_payload_id, exc, report)
                return

            report.pages += 1
            report.fetched += len(page.records)
            observation = drift.observe_page(
                session,
                vendor=vendor,
                tenant_id=tenant.id,
                resource=resource,
                records=page.records,
                raw_payload_id=raw_payload_id,
            )
            session.commit()
            if observation.new_event is not None:
                report.drift_events.append(observation.new_event.id)
                run_log.warning("drift.detected", diff=observation.new_event.diff)
            drift_diff = observation.diff.as_dict() if observation.diff else None

            bad = 0
            for index, raw in enumerate(page.records):
                result = self._process_record(
                    session, tenant, resource, raw, index, raw_payload_id, drift_diff, report
                )
                bad += result.outcome is Outcome.INCOMPATIBLE

            if is_batch_unhealthy(
                len(page.records),
                bad,
                min_records=self._settings.batch_min_records,
                max_bad_ratio=self._settings.batch_max_bad_ratio,
            ):
                incident = self._incident(
                    session,
                    tenant,
                    resource,
                    ErrorCode.BATCH_THRESHOLD,
                    raw_payload_id=raw_payload_id,
                    detail={"bad": bad, "page_size": len(page.records)},
                )
                self._halt(session, tenant, resource, incident, report, trip=True)
                return

            next_cursor = page.next_cursor
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                incident = self._incident(
                    session,
                    tenant,
                    resource,
                    ErrorCode.PAGINATION_LOOP,
                    raw_payload_id=raw_payload_id,
                    detail={"repeated_cursor": next_cursor},
                )
                self._halt(session, tenant, resource, incident, report, trip=True)
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            checkpoint.cursor = next_cursor
            checkpoint.heartbeat_at = self._clock()
            session.commit()

        checkpoint.cursor = None
        checkpoint.last_success_started_at = checkpoint.run_started_at
        checkpoint.run_updated_since = None
        breaker.record_success(breaker.get_or_create(session, vendor, tenant.id, resource))
        session.commit()

    # --------------------------------------------------------------------- one record

    def _process_record(
        self,
        session: Session,
        tenant: TenantRef,
        resource: Resource,
        raw: Any,
        index: int,
        raw_payload_id: int,
        drift_diff: dict[str, Any] | None,
        report: SyncReport | None,
    ) -> RecordResult:
        vendor = self.adapter.vendor
        mapping_version = self.adapter.mapping_version
        spec = self.adapter.contract(resource)
        result = self.adapter.map_record(resource, raw)

        parent_id: int | None = None
        parent_exists: bool | None = None
        parent_vendor_id: str | None = None
        if result.record is not None and spec.parent is not None and spec.parent_field:
            parent_vendor_id = getattr(result.record, spec.parent_field)
            parent_id = self._find_parent(session, tenant, spec.parent, parent_vendor_id)
            parent_exists = parent_id is not None

        outcome = classify(result, parent_exists)
        write: WriteOutcome | None = None

        if outcome is Outcome.INCOMPATIBLE:
            linked = [
                self._incident(
                    session,
                    tenant,
                    resource,
                    issue.code,
                    field_path=issue.field,
                    severity=Severity.CRITICAL,
                    raw_payload_id=raw_payload_id,
                )
                for issue in result.blocking_issues
            ]
            quarantine.quarantine_record(
                session,
                vendor=vendor,
                tenant_id=tenant.id,
                resource=resource,
                raw_payload_id=raw_payload_id,
                record_index=index,
                vendor_record_id=result.vendor_id,
                issues=[issue.as_dict() for issue in result.issues],
                mapping_version=mapping_version,
                fingerprint_diff=drift_diff,
                incidents=linked,
            )
            if report:
                report.quarantined += 1
                for incident in linked:
                    report.note_incident(incident.id)

        elif outcome is Outcome.PENDING_DEPENDENCY:
            assert result.vendor_id and spec.parent and parent_vendor_id
            pending.park(
                session,
                vendor=vendor,
                tenant_id=tenant.id,
                resource=resource,
                vendor_id=result.vendor_id,
                raw_payload_id=raw_payload_id,
                record_index=index,
                waiting_on_resource=spec.parent,
                waiting_on_vendor_id=parent_vendor_id,
            )
            if report:
                report.pending += 1

        else:
            assert result.record is not None
            extra = {"patient_id": parent_id} if spec.parent is not None else {}
            written = upsert_record(
                session,
                vendor=vendor,
                tenant_id=tenant.id,
                resource=resource,
                record=result.record,
                raw_payload_id=raw_payload_id,
                mapping_version=mapping_version,
                extra_columns=extra,
                keep_existing=result.absent_fields,
            )
            write = written.outcome
            for issue in result.issues:
                self._degraded_incident(session, tenant, resource, issue, raw_payload_id, report)
            touched = quarantine.resolve_superseded(
                session,
                vendor=vendor,
                tenant_id=tenant.id,
                resource=resource,
                vendor_record_id=result.record.vendor_id,
            )
            incidents.close_if_drained(session, touched)
            pending.resolve_pending(
                session,
                vendor=vendor,
                tenant_id=tenant.id,
                resource=resource,
                vendor_id=result.record.vendor_id,
            )
            if report:
                setattr(report, write.value, getattr(report, write.value) + 1)
                report.degraded += outcome is Outcome.DEGRADED

        self._ledger(
            session,
            raw_payload_id,
            record_key(resource, result.vendor_id, raw_payload_id, index),
            mapping_version,
            outcome,
        )
        session.commit()
        return RecordResult(outcome, write)

    def _find_parent(
        self, session: Session, tenant: TenantRef, parent: Resource, vendor_id: str
    ) -> int | None:
        table = TABLE_BY_RESOURCE[parent]
        return session.scalar(
            select(table.id).where(
                table.vendor == self.adapter.vendor,
                table.tenant_id == tenant.id,  # same tenant only: IDs repeat across practices
                table.vendor_id == vendor_id,
            )
        )

    def _degraded_incident(
        self,
        session: Session,
        tenant: TenantRef,
        resource: Resource,
        issue: MappingIssue,
        raw_payload_id: int,
        report: SyncReport | None,
    ) -> None:
        incident = self._incident(
            session,
            tenant,
            resource,
            issue.code,
            field_path=issue.field,
            severity=ISSUE_SEVERITY[issue.criticality],
            raw_payload_id=raw_payload_id,
        )
        if report:
            report.note_incident(incident.id)

    @staticmethod
    def _ledger(
        session: Session, raw_payload_id: int, key: str, mapping_version: int, outcome: Outcome
    ) -> None:
        row = session.scalar(
            select(ProcessingLedger).where(
                ProcessingLedger.raw_payload_id == raw_payload_id,
                ProcessingLedger.record_key == key,
                ProcessingLedger.mapping_version == mapping_version,
            )
        )
        if row is None:
            session.add(
                ProcessingLedger(
                    raw_payload_id=raw_payload_id,
                    record_key=key,
                    mapping_version=mapping_version,
                    outcome=outcome,
                )
            )
        else:
            row.outcome = outcome
            row.processed_at = utcnow()

    # ------------------------------------------------------------------ failure paths

    def _store_raw(
        self,
        session: Session,
        tenant: TenantRef,
        resource: Resource,
        cursor: str | None,
        response: RawResponse,
    ) -> int:
        row = RawPayload(
            vendor=self.adapter.vendor,
            tenant_id=tenant.id,
            resource=resource,
            endpoint=response.url,
            cursor=cursor,
            http_status=response.status_code,
            content_type=response.headers.get("content-type"),
            sha256=hashlib.sha256(response.body).hexdigest(),
            body=response.body,
        )
        session.add(row)
        session.commit()
        return row.id

    def _incident(
        self,
        session: Session,
        tenant: TenantRef,
        resource: Resource,
        code: ErrorCode,
        *,
        field_path: str = "",
        severity: Severity = Severity.CRITICAL,
        raw_payload_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Incident:
        return incidents.record_incident(
            session,
            vendor=self.adapter.vendor,
            tenant_id=tenant.id,
            resource=resource,
            code=code,
            severity=severity,
            field_path=field_path,
            raw_payload_id=raw_payload_id,
            detail=detail,
        )

    def _halt(
        self,
        session: Session,
        tenant: TenantRef,
        resource: Resource,
        incident: Incident,
        report: SyncReport,
        *,
        trip: bool,
    ) -> None:
        state = breaker.get_or_create(session, self.adapter.vendor, tenant.id, resource)
        if trip:
            breaker.trip(state, ErrorCode(incident.error_code), incident.id)
        session.commit()
        report.status = SyncStatus.HALTED
        report.halt_reason = incident.error_code
        report.note_incident(incident.id)

    def _handle_fetch_failure(
        self,
        session: Session,
        tenant: TenantRef,
        resource: Resource,
        cursor: str | None,
        fetched: Any,
        report: SyncReport,
    ) -> None:
        code: ErrorCode = fetched.failure
        incident = self._incident(
            session,
            tenant,
            resource,
            code,
            raw_payload_id=fetched.raw_payload_id,
            detail={"detail": fetched.detail, "cursor": cursor},
        )
        session.add(
            RequestDLQ(
                vendor=self.adapter.vendor,
                tenant_id=tenant.id,
                resource=resource,
                cursor=cursor,
                failure_code=code,
                http_status=fetched.response.status_code if fetched.response else None,
                attempts=fetched.attempts,
                raw_payload_id=fetched.raw_payload_id,
                incident_id=incident.id,
                detail=fetched.detail,
            )
        )
        state = breaker.get_or_create(session, self.adapter.vendor, tenant.id, resource)
        if code is ErrorCode.CONTRACT_OR_AUTH:
            breaker.trip(state, code, incident.id)  # retrying a 401/404 later won't help either
        else:
            breaker.record_failure(
                state, code, incident.id, self._settings.breaker_failure_threshold
            )
        session.commit()
        report.status = SyncStatus.HALTED
        report.halt_reason = f"{code}: {fetched.detail}"
        report.note_incident(incident.id)

    def _close_dlq(
        self, session: Session, tenant: TenantRef, resource: Resource, cursor: str | None
    ) -> None:
        cursor_match = (
            RequestDLQ.cursor.is_(None) if cursor is None else RequestDLQ.cursor == cursor
        )
        session.execute(
            update(RequestDLQ)
            .where(
                RequestDLQ.vendor == self.adapter.vendor,
                RequestDLQ.tenant_id == tenant.id,
                RequestDLQ.resource == resource,
                RequestDLQ.status == DlqStatus.OPEN,
                cursor_match,
            )
            .values(status=DlqStatus.REFETCHED, resolved_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        session.commit()

    def _handle_envelope_change(
        self,
        session: Session,
        tenant: TenantRef,
        resource: Resource,
        raw_payload_id: int,
        error: EnvelopeError,
        report: SyncReport,
    ) -> None:
        incident = self._incident(
            session,
            tenant,
            resource,
            ErrorCode.ENVELOPE_CHANGED,
            raw_payload_id=raw_payload_id,
            detail={"detail": str(error)},
        )
        quarantine.quarantine_page(
            session,
            vendor=self.adapter.vendor,
            tenant_id=tenant.id,
            resource=resource,
            raw_payload_id=raw_payload_id,
            issue={
                "code": ErrorCode.ENVELOPE_CHANGED,
                "field": "$",
                "criticality": Criticality.CRITICAL,
                "expected": "JSON array",
                "actual": str(error),
            },
            mapping_version=self.adapter.mapping_version,
            incident=incident,
        )
        self._halt(session, tenant, resource, incident, report, trip=True)

    # -------------------------------------------------------------------- pending

    def _retry_pending(self, session: Session, tenant: TenantRef, report: SyncReport) -> int:
        resolved = 0
        rows = pending.waiting_on(
            session,
            vendor=self.adapter.vendor,
            tenant_id=tenant.id,
            parent_resource=Resource.PATIENT,
        )
        for row in rows:
            resource = Resource(row.resource)
            raw = self._load_record(session, row.raw_payload_id, resource, row.record_index)
            result = self._process_record(
                session, tenant, resource, raw, row.record_index, row.raw_payload_id, None, None
            )
            if result.outcome is Outcome.PENDING_DEPENDENCY:
                row.attempts += 1
                if row.attempts >= self._settings.pending_max_attempts:
                    self._escalate_orphan(session, tenant, resource, row, report)
                session.commit()
            elif result.write is not None:
                resolved += 1
        return resolved

    def _escalate_orphan(
        self, session: Session, tenant: TenantRef, resource: Resource, row: Any, report: SyncReport
    ) -> None:
        spec = self.adapter.contract(resource)
        issue = MappingIssue(
            ErrorCode.ORPHAN_REFERENCE,
            spec.parent_field or "",
            Criticality.CRITICAL,
            f"existing {spec.parent}",
            row.waiting_on_vendor_id,
        )
        incident = self._incident(
            session,
            tenant,
            resource,
            ErrorCode.ORPHAN_REFERENCE,
            field_path=issue.field,
            raw_payload_id=row.raw_payload_id,
        )
        quarantine.quarantine_record(
            session,
            vendor=self.adapter.vendor,
            tenant_id=tenant.id,
            resource=resource,
            raw_payload_id=row.raw_payload_id,
            record_index=row.record_index,
            vendor_record_id=row.vendor_id,
            issues=[issue.as_dict()],
            mapping_version=self.adapter.mapping_version,
            fingerprint_diff=None,
            incidents=[incident],
        )
        row.status = PendingStatus.ESCALATED
        report.note_incident(incident.id)

    # --------------------------------------------------------------------- replay

    def replay(
        self,
        *,
        scope: QuarantineScope = QuarantineScope.RECORD,
        tenant_name: str | None = None,
        resource: Resource | None = None,
        error_code: ErrorCode | None = None,
    ) -> ReplayReport:
        """Re-process quarantined data from the *stored* raw payloads with the current mapping.

        No vendor call. Safe to repeat: resolved rows are skipped and writes are idempotent.
        """
        report = ReplayReport(scope=scope, mapping_version=self.adapter.mapping_version)
        with self._sessions() as session:
            query = select(Quarantine).where(
                Quarantine.status == QuarantineStatus.QUARANTINED,
                Quarantine.scope == scope,
                Quarantine.vendor == self.adapter.vendor,
            )
            if tenant_name:
                tenant = self._load_tenant(session, tenant_name)
                query = query.where(Quarantine.tenant_id == tenant.id)
            if resource:
                query = query.where(Quarantine.resource == resource)
            rows = [
                row
                for row in session.scalars(query.order_by(Quarantine.id)).all()
                if error_code is None or error_code in row.error_codes
            ]

            touched_incidents: list[int] = []
            touched_streams: set[tuple[int, str]] = set()
            for row in rows:
                report.replayed += 1
                touched_streams.add((row.tenant_id, row.resource))
                touched_incidents += [incident.id for incident in row.incidents]
                tenant = self._tenant_by_id(session, row.tenant_id)
                if scope is QuarantineScope.PAGE:
                    self._replay_page(session, tenant, row, report)
                else:
                    self._replay_record(session, tenant, row, report)

            report.incidents_closed = incidents.close_if_drained(session, touched_incidents)
            for tenant_id, resource_name in sorted(touched_streams):
                if self._reset_if_drained(session, tenant_id, Resource(resource_name)):
                    tenant = self._tenant_by_id(session, tenant_id)
                    report.breakers_reset.append(f"{tenant.name}/{resource_name}")
            session.commit()
        log.info("replay.finished", **report.as_dict())
        return report

    def _replay_record(
        self, session: Session, tenant: TenantRef, row: Quarantine, report: ReplayReport
    ) -> None:
        resource = Resource(row.resource)
        assert row.record_index is not None
        try:
            raw = self._load_record(session, row.raw_payload_id, resource, row.record_index)
        except EnvelopeError:
            row.attempts += 1
            report.still_quarantined += 1
            session.commit()
            return
        result = self._process_record(
            session, tenant, resource, raw, row.record_index, row.raw_payload_id, None, None
        )
        session.refresh(row)
        if result.outcome is Outcome.INCOMPATIBLE:
            row.attempts += 1
            report.still_quarantined += 1
        elif result.outcome is Outcome.PENDING_DEPENDENCY:
            quarantine.resolve(row, "replayed: waiting for parent record")
            report.moved_to_pending += 1
        else:
            if row.status == QuarantineStatus.QUARANTINED:
                quarantine.resolve(row, "replayed")
            report.resolved += 1
        session.commit()

    def _replay_page(
        self, session: Session, tenant: TenantRef, row: Quarantine, report: ReplayReport
    ) -> None:
        resource = Resource(row.resource)
        raw_payload = session.get(RawPayload, row.raw_payload_id)
        assert raw_payload is not None
        try:
            page = self.adapter.parse_page(resource, json.loads(raw_payload.body), {})
        except (EnvelopeError, ValueError):
            row.attempts += 1
            report.still_quarantined += 1
            session.commit()
            return
        for index, raw in enumerate(page.records):
            self._process_record(session, tenant, resource, raw, index, raw_payload.id, None, None)
        quarantine.resolve(row, f"page replayed: {len(page.records)} records re-classified")
        report.resolved += 1
        session.commit()

    def _reset_if_drained(self, session: Session, tenant_id: int, resource: Resource) -> bool:
        """Nothing left quarantined for this stream: close run-level incidents and the breaker."""
        still_open = session.scalar(
            select(Quarantine.id).where(
                Quarantine.tenant_id == tenant_id,
                Quarantine.resource == resource,
                Quarantine.status == QuarantineStatus.QUARANTINED,
            )
        )
        if still_open is not None:
            return False
        for incident in session.scalars(
            select(Incident).where(
                Incident.tenant_id == tenant_id,
                Incident.resource == resource,
                Incident.status == IncidentStatus.OPEN,
                Incident.error_code.in_(RUN_LEVEL_CODES),
            )
        ):
            incidents.close_incident(incident, "replay drained the quarantine")
        state = breaker.get_or_create(session, self.adapter.vendor, tenant_id, resource)
        if state.state == BreakerState.OPEN and state.reason_code in RUN_LEVEL_CODES:
            breaker.reset(state, "replay drained the quarantine")
            return True
        return False

    # -------------------------------------------------------------------- helpers

    def _load_record(
        self, session: Session, raw_payload_id: int, resource: Resource, index: int
    ) -> Any:
        raw_payload = session.get(RawPayload, raw_payload_id)
        assert raw_payload is not None
        page = self.adapter.parse_page(resource, json.loads(raw_payload.body), {})
        return page.records[index]

    def _tenant_by_id(self, session: Session, tenant_id: int) -> TenantRef:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        return TenantRef(tenant.id, tenant.name, tenant.vendor, dict(tenant.locator))


def _note_retry(report: SyncReport, run_log: Any, attempt: int, detail: str, delay: float) -> None:
    report.retries += 1
    run_log.warning("fetch.retry", attempt=attempt, detail=detail, delay=round(delay, 3))


def _summary(report: SyncReport) -> dict[str, Any]:
    keys = ("status", "pages", "fetched", "inserted", "updated", "unchanged", "degraded")
    keys += ("pending", "quarantined", "retries", "pending_resolved")
    return {key: getattr(report, key) for key in keys}
