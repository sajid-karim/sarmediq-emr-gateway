from datetime import UTC, date, datetime

import pytest

from app.core.canonical import AppointmentStatus, Patient, ReportStatus
from app.core.errors import ErrorCode
from app.core.types import Resource
from app.vendors.acme.mapper import AcmeMapper
from mock_emr.data import records
from mock_emr.scenarios import transform


@pytest.fixture
def v1():
    return AcmeMapper(1)


def patient(**overrides):
    row = records("practice_a", "patients")[0]
    row.update(overrides)
    return row


def codes(result):
    return {(i.code, i.field) for i in result.issues}


def test_v1_patient_maps_to_canonical(v1):
    result = v1.map_record(Resource.PATIENT, patient())
    assert isinstance(result.record, Patient)
    assert result.record.vendor_id == "P-1001"
    assert isinstance(result.record.dob, date)
    assert result.issues == ()


def test_every_v1_fixture_maps_cleanly(v1):
    pairs = [
        (Resource.PATIENT, "patients"),
        (Resource.APPOINTMENT, "appointments"),
        (Resource.CLINICAL_REPORT, "reports"),
    ]
    for practice in ("practice_a", "practice_b"):
        for resource, endpoint in pairs:
            for row in records(practice, endpoint):
                result = v1.map_record(resource, row)
                assert result.record is not None and result.issues == (), (practice, row)


def test_appointment_status_is_translated_to_fhir_words(v1):
    row = records("practice_a", "appointments")[3]
    result = v1.map_record(Resource.APPOINTMENT, row)
    assert result.record.status is AppointmentStatus.NOSHOW
    assert result.record.start_time.tzinfo == UTC


def test_epoch_dob_is_rejected_not_converted(v1):
    result = v1.map_record(Resource.PATIENT, patient(dob=479433600))
    assert result.record is None
    assert (ErrorCode.TYPE_CHANGED, "dob") in codes(result)


def test_bad_date_format(v1):
    result = v1.map_record(Resource.PATIENT, patient(dob="12/03/1985"))
    assert (ErrorCode.BAD_FORMAT, "dob") in codes(result)


def test_unknown_enum_is_never_mapped_to_a_neighbour(v1):
    row = records("practice_a", "appointments")[0]
    row["status"] = "checked_in_virtual"
    result = v1.map_record(Resource.APPOINTMENT, row)
    assert result.record is None
    assert (ErrorCode.UNKNOWN_ENUM_VALUE, "status") in codes(result)


def test_datetime_without_timezone_is_bad_format(v1):
    row = records("practice_a", "appointments")[0]
    row["start_time"] = "2026-11-02T09:00:00"
    result = v1.map_record(Resource.APPOINTMENT, row)
    assert (ErrorCode.BAD_FORMAT, "start_time") in codes(result)


def test_breaking_rename_collects_all_issues_in_v1(v1):
    row = transform("breaking", "patients", records("practice_a", "patients"))[0]
    result = v1.map_record(Resource.PATIENT, row)
    assert result.record is None
    assert {
        (ErrorCode.MISSING_CRITICAL_FIELD, "dob"),
        (ErrorCode.MISSING_CRITICAL_FIELD, "mrn"),
    } <= codes(result)
    assert result.vendor_id == "P-1001"


def test_v2_reads_the_approved_aliases():
    row = transform("breaking", "patients", records("practice_a", "patients"))[0]
    original = records("practice_a", "patients")[0]
    result = AcmeMapper(2).map_record(Resource.PATIENT, row)
    assert result.record is not None
    assert result.record.dob == date.fromisoformat(original["dob"])
    assert result.record.mrn == original["mrn"]


def test_missing_optional_field_degrades_and_is_reported_absent(v1):
    row = patient()
    del row["name"]["middle"]
    result = v1.map_record(Resource.PATIENT, row)
    assert result.record is not None
    assert (ErrorCode.MISSING_OPTIONAL_FIELD, "name.middle") in codes(result)
    assert result.absent_fields == {"middle_name"}


def test_null_optional_field_is_fine(v1):
    row = patient()
    row["name"]["middle"] = None
    result = v1.map_record(Resource.PATIENT, row)
    assert result.issues == () and result.record.middle_name is None


def test_null_critical_field_blocks(v1):
    result = v1.map_record(Resource.PATIENT, patient(mrn=None))
    assert result.record is None
    assert (ErrorCode.MISSING_CRITICAL_FIELD, "mrn") in codes(result)


def test_wrong_type_on_important_field_degrades(v1):
    row = patient()
    row["contact"]["phone"] = 5550101
    result = v1.map_record(Resource.PATIENT, row)
    assert result.record is not None and result.record.phone is None
    assert (ErrorCode.TYPE_CHANGED, "contact.phone") in codes(result)


def test_non_object_element_is_malformed(v1):
    result = v1.map_record(Resource.PATIENT, None)
    assert codes(result) == {(ErrorCode.MALFORMED_RECORD, "$")}


def test_report_status_amended_is_known(v1):
    row = records("practice_a", "reports")[0]
    row["status"] = "amended"
    result = v1.map_record(Resource.CLINICAL_REPORT, row)
    assert result.record.status is ReportStatus.AMENDED


def test_report_units_moved_is_missing_critical(v1):
    row = transform("report_change", "reports", records("practice_a", "reports"))[1]
    result = v1.map_record(Resource.CLINICAL_REPORT, row)
    assert (ErrorCode.MISSING_CRITICAL_FIELD, "units") in codes(result)


def test_unknown_mapping_version_rejected():
    with pytest.raises(ValueError):
        AcmeMapper(99)


def test_issued_at_is_normalised_to_utc(v1):
    row = records("practice_a", "reports")[0]
    row["issued_at"] = "2026-10-15T13:00:00+05:00"
    result = v1.map_record(Resource.CLINICAL_REPORT, row)
    assert result.record.issued_at == datetime(2026, 10, 15, 8, tzinfo=UTC)
