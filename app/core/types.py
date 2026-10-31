from enum import StrEnum


class Resource(StrEnum):
    PATIENT = "patient"
    APPOINTMENT = "appointment"
    CLINICAL_REPORT = "clinical_report"


class Outcome(StrEnum):
    COMPATIBLE = "compatible"
    DEGRADED = "degraded"
    INCOMPATIBLE = "incompatible"
    PENDING_DEPENDENCY = "pending_dependency"


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class IncidentStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class QuarantineScope(StrEnum):
    RECORD = "record"
    PAGE = "page"


class QuarantineStatus(StrEnum):
    QUARANTINED = "quarantined"
    RESOLVED = "resolved"
    DISCARDED = "discarded"


class PendingStatus(StrEnum):
    WAITING = "waiting"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class DlqStatus(StrEnum):
    OPEN = "open"
    REFETCHED = "refetched"


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class DriftStatus(StrEnum):
    NEW = "new"
    ACKED = "acked"
