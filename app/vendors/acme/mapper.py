"""Acme raw record -> canonical record.

Rules:
- Read each field at its contracted path. A rename looks like "missing" + "new field".
- Newer mapping versions may add alias reads, but only after a human approved them.
- No coercion: a date must arrive as an ISO string, an enum as a known value.
- Collect every issue, not just the first, so one incident shows the full picture.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from app.core.canonical import MODEL_BY_RESOURCE
from app.core.errors import ErrorCode
from app.core.types import Resource
from app.vendors.acme.contract import CONTRACTS
from app.vendors.base import Criticality, FieldKind, FieldSpec, MappingIssue, MapResult

_MISSING = object()

Reader = Callable[[dict], Any]


def read_path(path: str) -> Reader:
    def read(raw: dict) -> Any:
        node: Any = raw
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return _MISSING
            node = node[key]
        return node

    return read


def read_identifier(system: str) -> Reader:
    def read(raw: dict) -> Any:
        identifiers = raw.get("identifiers")
        if not isinstance(identifiers, list):
            return _MISSING
        for item in identifiers:
            if isinstance(item, dict) and item.get("system") == system:
                return item.get("value", _MISSING)
        return _MISSING

    return read


# Extra reads per mapping version, tried after the contracted path.
# v2 = fixes approved after the "breaking" incident (dob -> birth_date, mrn -> identifiers[]).
ALIASES: dict[int, dict[tuple[Resource, str], tuple[Reader, ...]]] = {
    1: {},
    2: {
        (Resource.PATIENT, "dob"): (read_path("birth_date"),),
        (Resource.PATIENT, "mrn"): (read_identifier("MRN"),),
    },
}

_MISSING_CODE = {
    Criticality.CRITICAL: ErrorCode.MISSING_CRITICAL_FIELD,
    Criticality.IMPORTANT: ErrorCode.MISSING_IMPORTANT_FIELD,
    Criticality.OPTIONAL: ErrorCode.MISSING_OPTIONAL_FIELD,
}


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


class AcmeMapper:
    def __init__(self, mapping_version: int) -> None:
        if mapping_version not in ALIASES:
            raise ValueError(f"unknown Acme mapping version {mapping_version}")
        self.mapping_version = mapping_version

    def map_record(self, resource: Resource, raw: Any) -> MapResult:
        if not isinstance(raw, dict):
            issue = MappingIssue(
                ErrorCode.MALFORMED_RECORD, "$", Criticality.CRITICAL, "object", json_type(raw)
            )
            return MapResult(record=None, vendor_id=None, issues=(issue,))

        values: dict[str, Any] = {}
        issues: list[MappingIssue] = []
        absent: set[str] = set()
        for spec in CONTRACTS[resource].fields:
            value, issue, was_absent = self._read_field(resource, spec, raw)
            values[spec.name] = value
            if issue:
                issues.append(issue)
            if was_absent:
                absent.add(spec.name)

        vendor_id = values["vendor_id"] if isinstance(values.get("vendor_id"), str) else None
        if any(issue.blocking for issue in issues):
            return MapResult(record=None, vendor_id=vendor_id, issues=tuple(issues))
        record = MODEL_BY_RESOURCE[resource](**values)
        return MapResult(
            record=record,
            vendor_id=vendor_id,
            issues=tuple(issues),
            absent_fields=frozenset(absent),
        )

    def _read_field(
        self, resource: Resource, spec: FieldSpec, raw: dict
    ) -> tuple[Any, MappingIssue | None, bool]:
        readers = (
            read_path(spec.path),
            *ALIASES[self.mapping_version].get((resource, spec.name), ()),
        )
        value: Any = _MISSING
        for read in readers:
            value = read(raw)
            if value is not _MISSING:
                break

        if value is _MISSING:
            issue = MappingIssue(
                _MISSING_CODE[spec.criticality], spec.path, spec.criticality, spec.kind, "absent"
            )
            return None, issue, True
        if value is None:
            if spec.criticality is Criticality.CRITICAL:
                issue = MappingIssue(
                    ErrorCode.MISSING_CRITICAL_FIELD, spec.path, spec.criticality, spec.kind, "null"
                )
                return None, issue, False
            return None, None, False

        converted, issue = self._convert(spec, value)
        return converted, issue, False

    @staticmethod
    def _convert(spec: FieldSpec, value: Any) -> tuple[Any, MappingIssue | None]:
        def problem(code: ErrorCode, actual: str) -> tuple[None, MappingIssue]:
            return None, MappingIssue(code, spec.path, spec.criticality, spec.kind, actual)

        if not isinstance(value, str):
            return problem(ErrorCode.TYPE_CHANGED, json_type(value))

        if spec.kind is FieldKind.STRING:
            return value, None
        if spec.kind is FieldKind.DATE:
            try:
                return date.fromisoformat(value), None
            except ValueError:
                return problem(ErrorCode.BAD_FORMAT, value)
        if spec.kind is FieldKind.DATETIME:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return problem(ErrorCode.BAD_FORMAT, value)
            if parsed.tzinfo is None:
                return problem(ErrorCode.BAD_FORMAT, f"{value} (no timezone)")
            return parsed.astimezone(UTC), None
        if spec.kind is FieldKind.ENUM:
            assert spec.enum_map is not None
            if value not in spec.enum_map:
                # Name the allowed values, so the incident is actionable without the source.
                allowed = f"one of: {', '.join(spec.enum_map)}"
                issue = MappingIssue(
                    ErrorCode.UNKNOWN_ENUM_VALUE, spec.path, spec.criticality, allowed, value
                )
                return None, issue
            return spec.enum_map[value], None
        raise AssertionError(f"unhandled field kind {spec.kind}")
