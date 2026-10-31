from collections.abc import Iterator
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.db.models import Tenant


def get_services(request: Request) -> Any:
    """The app's Services container (see app.main), built once at startup."""
    return request.app.state.services


def get_session(services: Any = Depends(get_services)) -> Iterator[Session]:
    with services.session_factory() as session:
        yield session


def tenant_id_for(session: Session, name: str) -> int:
    tenant_id = session.scalar(select(Tenant.id).where(Tenant.name == name))
    if tenant_id is None:
        raise HTTPException(status_code=404, detail=f"unknown tenant {name!r}")
    return tenant_id


def row_to_dict(row: Any) -> dict[str, Any]:
    return {attr.key: getattr(row, attr.key) for attr in inspect(row).mapper.column_attrs}
