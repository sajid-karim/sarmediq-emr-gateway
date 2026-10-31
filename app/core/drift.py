"""Schema drift detection (ARCHITECTURE §3 step ④). Observational: it never blocks.

A page's *shape* maps every field path to the set of JSON types seen on that page:
  - union across records,
  - null adds no type (only "key present"),
  - array indexes dropped: identifiers[0].value -> identifiers[].value.
A drift is identified by *what changed* versus the baseline, so the same vendor change seen on
30 pages and 2 tenants produces one drift event.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.types import DriftStatus
from app.db.models import DriftEvent, SchemaFingerprint

Shape = dict[str, list[str]]


def _json_type(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _visit(value: Any, path: str, types: dict[str, set[str]]) -> None:
    seen = types.setdefault(path, set())
    json_type = _json_type(value)
    if json_type:
        seen.add(json_type)
    if isinstance(value, dict):
        for key, child in value.items():
            _visit(child, f"{path}.{key}", types)
    elif isinstance(value, list):
        for item in value:
            _visit(item, f"{path}[]", types)


def shape_of(records: Iterable[Any]) -> Shape:
    types: dict[str, set[str]] = {}
    for record in records:
        if isinstance(record, dict):
            for key, value in record.items():
                _visit(value, key, types)
    return {path: sorted(found) for path, found in sorted(types.items())}


def _digest(prefix: str, value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(payload.encode()).hexdigest()}"


def shape_hash(shape: Shape) -> str:
    return _digest("shape", shape)


@dataclass(frozen=True)
class ShapeDiff:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def as_dict(self) -> dict[str, Any]:
        return {"added": list(self.added), "removed": list(self.removed), "changed": self.changed}

    def hash(self) -> str:
        return _digest("diff", self.as_dict())


def diff_shapes(baseline: Shape, current: Shape) -> ShapeDiff:
    changed = {}
    for path in sorted(set(baseline) & set(current)):
        before, after = baseline[path], current[path]
        # Only compare when both sides saw a real value; null-only columns tell us nothing.
        if before and after and not set(after) <= set(before):
            changed[path] = {"from": before, "to": after}
    return ShapeDiff(
        added=tuple(sorted(set(current) - set(baseline))),
        removed=tuple(sorted(set(baseline) - set(current))),
        changed=changed,
    )


@dataclass(frozen=True)
class DriftObservation:
    diff: ShapeDiff | None
    new_event: DriftEvent | None


def observe_page(
    session: Session,
    *,
    vendor: str,
    tenant_id: int,
    resource: str,
    records: list[Any],
    raw_payload_id: int,
) -> DriftObservation:
    shape = shape_of(records)
    if not shape:
        return DriftObservation(None, None)
    now = utcnow()
    baseline = session.scalar(
        select(SchemaFingerprint).where(
            SchemaFingerprint.vendor == vendor,
            SchemaFingerprint.resource == resource,
            SchemaFingerprint.is_baseline.is_(True),
        )
    )
    if baseline is None:
        # First shape ever seen becomes the baseline (bootstrap from the documented v1 data).
        _get_or_create(session, vendor, resource, shape_hash(shape), shape, is_baseline=True)
        return DriftObservation(None, None)

    diff = diff_shapes(baseline.field_types, shape)
    if diff.is_empty:
        baseline.last_seen = now
        return DriftObservation(None, None)

    fingerprint, created = _get_or_create(
        session, vendor, resource, diff.hash(), shape, is_baseline=False
    )
    if not created:
        fingerprint.last_seen = now
        return DriftObservation(diff, None)
    event = DriftEvent(
        fingerprint_id=fingerprint.id,
        vendor=vendor,
        resource=resource,
        first_seen_tenant_id=tenant_id,
        diff=diff.as_dict(),
        raw_payload_id=raw_payload_id,
        status=DriftStatus.NEW,
    )
    session.add(event)
    session.flush()
    return DriftObservation(diff, event)


def _get_or_create(
    session: Session, vendor: str, resource: str, digest: str, shape: Shape, *, is_baseline: bool
) -> tuple[SchemaFingerprint, bool]:
    query = select(SchemaFingerprint).where(
        SchemaFingerprint.vendor == vendor,
        SchemaFingerprint.resource == resource,
        SchemaFingerprint.hash == digest,
    )
    existing = session.scalar(query)
    if existing is not None:
        return existing, False
    try:
        with session.begin_nested():
            fingerprint = SchemaFingerprint(
                vendor=vendor,
                resource=resource,
                hash=digest,
                field_types=shape,
                is_baseline=is_baseline,
            )
            session.add(fingerprint)
    except IntegrityError:
        # Another tenant's sync recorded the same shape a moment earlier.
        return session.scalar(query), False  # type: ignore[return-value]
    return fingerprint, True


def promote(session: Session, event: DriftEvent) -> None:
    """A human confirmed the new shape is expected: it becomes the baseline."""
    old = session.scalars(
        select(SchemaFingerprint).where(
            SchemaFingerprint.vendor == event.vendor,
            SchemaFingerprint.resource == event.resource,
            SchemaFingerprint.is_baseline.is_(True),
        )
    ).all()
    for fingerprint in old:
        fingerprint.is_baseline = False
    event.fingerprint.is_baseline = True
    event.fingerprint.promoted_at = utcnow()
    event.status = DriftStatus.ACKED
