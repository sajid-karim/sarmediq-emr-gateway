"""The boundary between vendor-specific code and the vendor-agnostic core."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from app.core.canonical import CanonicalRecord
from app.core.errors import ErrorCode
from app.core.types import Resource


class Criticality(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    OPTIONAL = "optional"


class FieldKind(StrEnum):
    STRING = "string"
    DATE = "date"
    DATETIME = "datetime"
    ENUM = "enum"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    path: str
    criticality: Criticality
    kind: FieldKind = FieldKind.STRING
    enum_map: Mapping[str, StrEnum] | None = None


@dataclass(frozen=True)
class ContractSpec:
    resource: Resource
    fields: tuple[FieldSpec, ...]
    parent: Resource | None = None
    parent_field: str | None = None


@dataclass(frozen=True)
class TenantRef:
    id: int
    name: str
    vendor: str
    locator: Mapping[str, Any]


@dataclass(frozen=True)
class RawResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    url: str


@dataclass(frozen=True)
class ParsedPage:
    records: list[Any]
    next_cursor: str | None


@dataclass(frozen=True)
class MappingIssue:
    code: ErrorCode
    field: str
    criticality: Criticality
    expected: str
    actual: str

    @property
    def blocking(self) -> bool:
        return self.criticality is Criticality.CRITICAL

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "field": self.field,
            "criticality": self.criticality,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class MapResult:
    record: CanonicalRecord | None
    vendor_id: str | None
    issues: tuple[MappingIssue, ...] = field(default=())
    absent_fields: frozenset[str] = field(default=frozenset())

    @property
    def blocking_issues(self) -> tuple[MappingIssue, ...]:
        return tuple(i for i in self.issues if i.blocking)


class VendorTransportError(Exception):
    """The request never produced an HTTP response (timeout, connection reset, DNS...)."""


class EnvelopeError(Exception):
    """The page's top-level shape does not match the contract."""


class VendorAdapter(Protocol):
    vendor: str
    mapping_version: int

    def fetch_page(
        self,
        tenant: TenantRef,
        resource: Resource,
        cursor: str | None,
        updated_since: datetime | None,
    ) -> RawResponse: ...

    def parse_page(
        self, resource: Resource, payload: Any, headers: Mapping[str, str]
    ) -> ParsedPage: ...

    def contract(self, resource: Resource) -> ContractSpec: ...

    def map_record(self, resource: Resource, raw: Any) -> MapResult: ...

    def close(self) -> None: ...
