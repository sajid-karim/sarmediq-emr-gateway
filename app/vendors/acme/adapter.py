"""AcmeAdapter: the only object the core talks to for Acme. Implements VendorAdapter."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.core.types import Resource
from app.vendors.acme.client import AcmeClient
from app.vendors.acme.contract import CONTRACTS
from app.vendors.acme.mapper import AcmeMapper, json_type
from app.vendors.base import (
    ContractSpec,
    EnvelopeError,
    MapResult,
    ParsedPage,
    RawResponse,
    TenantRef,
)


class AcmeAdapter:
    vendor = "acme"

    def __init__(self, client: AcmeClient, mapping_version: int = 1) -> None:
        self._client = client
        self._mapper = AcmeMapper(mapping_version)
        self.mapping_version = mapping_version

    def fetch_page(
        self,
        tenant: TenantRef,
        resource: Resource,
        cursor: str | None,
        updated_since: datetime | None,
    ) -> RawResponse:
        return self._client.fetch_page(tenant, resource, cursor, updated_since)

    def parse_page(
        self, resource: Resource, payload: Any, headers: Mapping[str, str]
    ) -> ParsedPage:
        if isinstance(payload, list):
            return ParsedPage(records=payload, next_cursor=headers.get("x-next-cursor") or None)
        # v2: approved support for the {"data": [...], "meta": {...}} envelope.
        if (
            self.mapping_version >= 2
            and isinstance(payload, dict)
            and isinstance(payload.get("data"), list)
        ):
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            return ParsedPage(records=payload["data"], next_cursor=meta.get("next_cursor") or None)
        shape = (
            f"object with keys {sorted(payload)}"
            if isinstance(payload, dict)
            else json_type(payload)
        )
        raise EnvelopeError(f"expected a JSON array of records, got {shape}")

    def contract(self, resource: Resource) -> ContractSpec:
        return CONTRACTS[resource]

    def map_record(self, resource: Resource, raw: Any) -> MapResult:
        return self._mapper.map_record(resource, raw)

    def close(self) -> None:
        self._client.close()
