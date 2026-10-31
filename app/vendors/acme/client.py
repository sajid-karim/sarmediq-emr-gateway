"""One HTTP attempt against Acme. Retries are the core's job (app/core/retry.py)."""

from datetime import datetime
from urllib.parse import quote

import httpx

from app.core.types import Resource
from app.vendors.acme.contract import ENDPOINTS
from app.vendors.base import RawResponse, TenantRef, VendorTransportError


class AcmeClient:
    def __init__(
        self,
        base_url: str,
        *,
        page_size: int,
        connect_timeout: float,
        read_timeout: float,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._page_size = page_size
        self._http = http_client or httpx.Client(
            base_url=base_url, timeout=httpx.Timeout(read_timeout, connect=connect_timeout)
        )

    def fetch_page(
        self,
        tenant: TenantRef,
        resource: Resource,
        cursor: str | None,
        updated_since: datetime | None,
    ) -> RawResponse:
        practice_id = quote(str(tenant.locator["practice_id"]), safe="")
        params: dict[str, str | int] = {"limit": self._page_size}
        if cursor:
            params["cursor"] = cursor
        if updated_since:
            params["updated_since"] = updated_since.isoformat()
        try:
            response = self._http.get(f"/{practice_id}/{ENDPOINTS[resource]}", params=params)
        except httpx.TimeoutException as exc:
            raise VendorTransportError(f"timeout: {exc.__class__.__name__}") from exc
        except httpx.TransportError as exc:
            raise VendorTransportError(f"transport error: {exc.__class__.__name__}") from exc
        return RawResponse(
            status_code=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
            url=str(response.request.url),
        )

    def close(self) -> None:
        self._http.close()
