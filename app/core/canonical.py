"""Vendor-independent internal model, shaped like a small subset of FHIR.

Strict and frozen: the mapper must hand over already-validated Python types
(date, aware datetime, enum members). Nothing is coerced here.
"""

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.core.types import Resource


class Sex(StrEnum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNKNOWN = "unknown"


class AppointmentStatus(StrEnum):
    BOOKED = "booked"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    NOSHOW = "noshow"


class ReportStatus(StrEnum):
    PRELIMINARY = "preliminary"
    FINAL = "final"
    AMENDED = "amended"
    CANCELLED = "cancelled"


class CanonicalRecord(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    vendor_id: str


class Patient(CanonicalRecord):
    mrn: str
    dob: date
    family_name: str
    given_name: str
    middle_name: str | None = None
    sex: Sex
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    language: str | None = None


class Appointment(CanonicalRecord):
    vendor_patient_id: str
    start_time: AwareDatetime
    status: AppointmentStatus
    provider_id: str | None = None
    location: str | None = None
    notes: str | None = None
    reason: str | None = None


class ClinicalReport(CanonicalRecord):
    vendor_patient_id: str
    type: str
    status: ReportStatus
    body: str
    units: str
    author: str | None = None
    issued_at: AwareDatetime | None = None
    summary: str | None = None


MODEL_BY_RESOURCE: dict[Resource, type[CanonicalRecord]] = {
    Resource.PATIENT: Patient,
    Resource.APPOINTMENT: Appointment,
    Resource.CLINICAL_REPORT: ClinicalReport,
}
