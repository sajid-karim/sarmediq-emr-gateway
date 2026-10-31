"""Idempotent writes: natural key + content hash + optimistic locking (ARCHITECTURE §6).

at-least-once fetch + idempotent write = effectively-once records.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, StrEnum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.canonical import CanonicalRecord
from app.core.clock import utcnow
from app.core.types import Resource
from app.db.models import TABLE_BY_RESOURCE, RecordVersion


class WriteOutcome(StrEnum):
    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    row_id: int
    version: int


class ConcurrentWriteError(Exception):
    pass


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def canonical_dict(record: CanonicalRecord) -> dict[str, Any]:
    return {name: to_jsonable(getattr(record, name)) for name in type(record).model_fields}


def content_hash(record: CanonicalRecord) -> str:
    payload = json.dumps(canonical_dict(record), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def record_key(resource: Resource, vendor_id: str | None, raw_payload_id: int, index: int) -> str:
    if vendor_id:
        return f"{resource}:{vendor_id}"
    return f"{resource}:raw{raw_payload_id}#{index}"


def upsert_record(
    session: Session,
    *,
    vendor: str,
    tenant_id: int,
    resource: Resource,
    record: CanonicalRecord,
    raw_payload_id: int,
    mapping_version: int,
    extra_columns: dict[str, Any] | None = None,
    keep_existing: frozenset[str] = frozenset(),
) -> WriteResult:
    """Insert, update or no-op one canonical record. Caller commits.

    keep_existing: fields the vendor *stopped sending* (absent key). Absence is not deletion,
    so an existing stored value is kept instead of being overwritten with null.
    """
    table = TABLE_BY_RESOURCE[resource]
    for _ in range(3):
        row = session.scalar(
            select(table).where(
                table.vendor == vendor,
                table.tenant_id == tenant_id,
                table.vendor_id == record.vendor_id,
            )
        )
        if row is not None and keep_existing:
            kept = {name: getattr(row, name) for name in keep_existing}
            record = record.model_copy(update=kept)
        values = {**record.model_dump(), **(extra_columns or {})}
        new_hash = content_hash(record)

        if row is None:
            try:
                with session.begin_nested():
                    row = table(
                        vendor=vendor,
                        tenant_id=tenant_id,
                        content_hash=new_hash,
                        version=1,
                        mapping_version=mapping_version,
                        **values,
                    )
                    session.add(row)
            except IntegrityError:
                continue  # another writer inserted the same natural key first; retry as update
            _add_version(
                session, resource, row.id, 1, new_hash, None, raw_payload_id, mapping_version
            )
            return WriteResult(WriteOutcome.INSERTED, row.id, 1)

        if row.content_hash == new_hash:
            return WriteResult(WriteOutcome.UNCHANGED, row.id, row.version)

        diff = {
            name: {"from": to_jsonable(getattr(row, name)), "to": to_jsonable(value)}
            for name, value in values.items()
            if to_jsonable(getattr(row, name)) != to_jsonable(value)
        }
        new_version = row.version + 1
        result = session.execute(
            update(table)
            .where(table.id == row.id, table.version == row.version)
            .values(
                **values,
                content_hash=new_hash,
                version=new_version,
                mapping_version=mapping_version,
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        session.expire(row)
        if result.rowcount != 1:
            continue  # optimistic lock lost: someone else updated it; re-read and retry
        _add_version(
            session, resource, row.id, new_version, new_hash, diff, raw_payload_id, mapping_version
        )
        return WriteResult(WriteOutcome.UPDATED, row.id, new_version)

    raise ConcurrentWriteError(f"could not write {resource} {record.vendor_id} after 3 attempts")


def _add_version(
    session: Session,
    resource: Resource,
    record_id: int,
    version: int,
    digest: str,
    diff: dict[str, Any] | None,
    raw_payload_id: int,
    mapping_version: int,
) -> None:
    session.add(
        RecordVersion(
            resource=resource,
            record_id=record_id,
            version=version,
            content_hash=digest,
            diff=diff,
            raw_payload_id=raw_payload_id,
            mapping_version=mapping_version,
        )
    )
