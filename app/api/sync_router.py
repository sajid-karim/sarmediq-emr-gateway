from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.api.deps import get_services
from app.core.types import Resource
from app.sync.orchestrator import SyncInProgressError, SyncStatus, UnknownTenantError

router = APIRouter(tags=["sync"])


@router.post("/sync/{resource}")
def run_sync(
    resource: Resource,
    tenant: str = Query(..., description="Tenant name, e.g. practice_a"),
    full: bool = Query(False, description="Ignore updated_since and re-read everything"),
    services=Depends(get_services),
) -> JSONResponse:
    try:
        report = services.orchestrator.sync(tenant, resource, full=full)
    except UnknownTenantError as exc:
        raise HTTPException(status_code=404, detail=f"unknown tenant {exc}") from exc
    except SyncInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    status_code = 503 if report.status is SyncStatus.BLOCKED else 200
    return JSONResponse(report.as_dict(), status_code=status_code)
