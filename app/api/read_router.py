"""Read canonical data back, proving what was stored.

Lists use keyset pagination: `?after=<vendor_id>` instead of OFFSET. The database seeks straight
to the next row via the (tenant_id, vendor_id) index, and rows inserted between pages can't
shift the results. Same style as the vendor contract: the next cursor is in `X-Next-Cursor`.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
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


def _list(
    session: Session, resource: Resource, tenant: str, limit: int, after: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    table = TABLE_BY_RESOURCE[resource]
    query = select(table).where(table.tenant_id == tenant_id_for(session, tenant))
    if after is not None:
        query = query.where(table.vendor_id > after)
    # Ask for one extra row: if it exists there is a next page, without a second COUNT query.
    rows = list(session.scalars(query.order_by(table.vendor_id).limit(limit + 1)))
    next_cursor = rows[limit - 1].vendor_id if len(rows) > limit else None
    return [row_to_dict(row) for row in rows[:limit]], next_cursor


def _get(session: Session, resource: Resource, record_id: int) -> dict[str, Any]:
    row = session.get(TABLE_BY_RESOURCE[resource], record_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{resource} {record_id} not found")
    return row_to_dict(row)


def _register(path: str, resource: Resource) -> None:
    @router.get(f"/{path}", name=f"list_{path}")
    def list_records(
        response: Response,
        tenant: str,
        limit: int = Query(100, ge=1, le=1000),
        after: str | None = Query(None, description="X-Next-Cursor from the previous page"),
        session: Session = Depends(get_session),
    ) -> list[dict[str, Any]]:
        rows, next_cursor = _list(session, resource, tenant, limit, after)
        if next_cursor is not None:
            response.headers["X-Next-Cursor"] = next_cursor
        return rows

    @router.get(f"/{path}/{{record_id}}", name=f"get_{path}")
    def get_record(record_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
        return _get(session, resource, record_id)


for _path, _resource in PATHS.items():
    _register(_path, _resource)
