from datetime import date

from app.core.canonical import Patient, Sex
from app.core.classifier import classify, is_batch_unhealthy
from app.core.errors import ErrorCode
from app.core.idempotency import content_hash
from app.core.types import Outcome
from app.vendors.base import Criticality, MappingIssue, MapResult

PATIENT = Patient(
    vendor_id="P-1",
    mrn="M-1",
    dob=date(1985, 3, 12),
    family_name="Smith",
    given_name="John",
    sex=Sex.MALE,
)


def issue(criticality, code=ErrorCode.MISSING_OPTIONAL_FIELD):
    return MappingIssue(code, "x", criticality, "string", "absent")


def test_clean_record_is_compatible():
    assert classify(MapResult(PATIENT, "P-1"), None) is Outcome.COMPATIBLE


def test_non_blocking_issue_degrades():
    result = MapResult(PATIENT, "P-1", issues=(issue(Criticality.OPTIONAL),))
    assert classify(result, None) is Outcome.DEGRADED


def test_blocking_issue_is_incompatible_even_if_parent_missing():
    result = MapResult(None, "A-1", issues=(issue(Criticality.CRITICAL),))
    assert classify(result, False) is Outcome.INCOMPATIBLE


def test_missing_parent_is_pending_not_an_error():
    assert classify(MapResult(PATIENT, "A-1"), False) is Outcome.PENDING_DEPENDENCY


def test_batch_health_needs_a_minimum_sample():
    assert not is_batch_unhealthy(4, 1, min_records=10, max_bad_ratio=0.2)
    assert not is_batch_unhealthy(10, 2, min_records=10, max_bad_ratio=0.2)
    assert is_batch_unhealthy(10, 3, min_records=10, max_bad_ratio=0.2)


def test_content_hash_is_stable_and_sensitive():
    same = Patient(**PATIENT.model_dump())
    changed = PATIENT.model_copy(update={"phone": "555"})
    assert content_hash(PATIENT) == content_hash(same)
    assert content_hash(PATIENT) != content_hash(changed)
