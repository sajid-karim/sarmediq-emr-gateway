"""What we expect from Acme, written as data (see mock_emr/CONTRACT.md)."""

from app.core.canonical import AppointmentStatus, ReportStatus, Sex
from app.core.types import Resource
from app.vendors.base import ContractSpec, Criticality, FieldKind, FieldSpec

CRIT, IMP, OPT = Criticality.CRITICAL, Criticality.IMPORTANT, Criticality.OPTIONAL

SEX_MAP = {
    "male": Sex.MALE,
    "female": Sex.FEMALE,
    "other": Sex.OTHER,
    "unknown": Sex.UNKNOWN,
}

APPOINTMENT_STATUS_MAP = {
    "scheduled": AppointmentStatus.BOOKED,
    "completed": AppointmentStatus.FULFILLED,
    "cancelled": AppointmentStatus.CANCELLED,
    "no_show": AppointmentStatus.NOSHOW,
}

REPORT_STATUS_MAP = {
    "preliminary": ReportStatus.PRELIMINARY,
    "final": ReportStatus.FINAL,
    "amended": ReportStatus.AMENDED,
    "cancelled": ReportStatus.CANCELLED,
}

PATIENT = ContractSpec(
    resource=Resource.PATIENT,
    fields=(
        FieldSpec("vendor_id", "id", CRIT),
        FieldSpec("mrn", "mrn", CRIT),
        FieldSpec("dob", "dob", CRIT, FieldKind.DATE),
        FieldSpec("family_name", "name.family", CRIT),
        FieldSpec("given_name", "name.given", CRIT),
        FieldSpec("middle_name", "name.middle", OPT),
        FieldSpec("sex", "sex", CRIT, FieldKind.ENUM, SEX_MAP),
        FieldSpec("phone", "contact.phone", IMP),
        FieldSpec("email", "contact.email", OPT),
        FieldSpec("address", "address", IMP),
        FieldSpec("language", "language", OPT),
    ),
)

APPOINTMENT = ContractSpec(
    resource=Resource.APPOINTMENT,
    fields=(
        FieldSpec("vendor_id", "id", CRIT),
        FieldSpec("vendor_patient_id", "patient_id", CRIT),
        FieldSpec("start_time", "start_time", CRIT, FieldKind.DATETIME),
        FieldSpec("status", "status", CRIT, FieldKind.ENUM, APPOINTMENT_STATUS_MAP),
        FieldSpec("provider_id", "provider_id", IMP),
        FieldSpec("location", "location", IMP),
        FieldSpec("notes", "notes", OPT),
        FieldSpec("reason", "reason", OPT),
    ),
    parent=Resource.PATIENT,
    parent_field="vendor_patient_id",
)

CLINICAL_REPORT = ContractSpec(
    resource=Resource.CLINICAL_REPORT,
    fields=(
        FieldSpec("vendor_id", "id", CRIT),
        FieldSpec("vendor_patient_id", "patient_id", CRIT),
        FieldSpec("type", "type", CRIT),
        FieldSpec("status", "status", CRIT, FieldKind.ENUM, REPORT_STATUS_MAP),
        FieldSpec("body", "body", CRIT),
        FieldSpec("units", "units", CRIT),
        FieldSpec("author", "author", IMP),
        FieldSpec("issued_at", "issued_at", IMP, FieldKind.DATETIME),
        FieldSpec("summary", "summary", OPT),
    ),
    parent=Resource.PATIENT,
    parent_field="vendor_patient_id",
)

CONTRACTS = {
    Resource.PATIENT: PATIENT,
    Resource.APPOINTMENT: APPOINTMENT,
    Resource.CLINICAL_REPORT: CLINICAL_REPORT,
}

ENDPOINTS = {
    Resource.PATIENT: "patients",
    Resource.APPOINTMENT: "appointments",
    Resource.CLINICAL_REPORT: "reports",
}
