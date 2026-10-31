"""Read canonical data back, proving what was stored."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session, row_to_dict, tenant_id_for
from app.core.types import Resource
from app.db.models import TABLE_BY_RESOURCE

router = APIRouter(tags=["read"])

PATHS = {
    "patients": Resource.PATIENT,
    "appointments": Resource.APPOINTMENT,
    "reports": Resource.CLINICAL_REPORT,
}


def _list(session: Session, resource: Resource, tenant: str, limit: int, offset: int) -> list:
    table = TABLE_BY_RESOURCE[resource]
    query = (
        select(table)
        .where(table.tenant_id == tenant_id_for(session, tenant))
        .order_by(table.vendor_id)
        .limit(limit)
        .offset(offset)
    )
    return [row_to_dict(row) for row in session.scalars(query)]


def _get(session: Session, resource: Resource, record_id: int) -> dict[str, Any]:
    row = session.get(TABLE_BY_RESOURCE[resource], record_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{resource} {record_id} not found")
    return row_to_dict(row)


def _register(path: str, resource: Resource) -> None:
    @router.get(f"/{path}", name=f"list_{path}")
    def list_records(
        tenant: str,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        session: Session = Depends(get_session),
    ) -> list[dict[str, Any]]:
        return _list(session, resource, tenant, limit, offset)

    @router.get(f"/{path}/{{record_id}}", name=f"get_{path}")
    def get_record(record_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
        return _get(session, resource, record_id)


for _path, _resource in PATHS.items():
    _register(_path, _resource)
