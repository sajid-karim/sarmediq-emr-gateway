"""Transport behaviour against the real mock server: 429, 503, timeouts, HTML bodies."""

from app.core.errors import ErrorCode
from app.core.types import BreakerState, DlqStatus, IncidentStatus, QuarantineStatus, Resource
from app.db.models import CircuitState, Incident, PatientRow, Quarantine, RawPayload, RequestDLQ
from app.sync.orchestrator import SyncStatus


def test_rate_limit_is_waited_out_without_losing_data(gateway):
    gateway.scenario("rate_limit")
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert report.status is SyncStatus.COMPLETED
    assert report.inserted == 25 and report.retries >= 1
    assert gateway.count(Incident) == 0


def test_flaky_503_and_timeout_are_retried_without_duplicates(gateway):
    gateway.scenario("flaky", slow_seconds=1.0)  # read timeout in tests is 0.5s
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert report.status is SyncStatus.COMPLETED
    assert report.inserted == 25 and report.retries >= 3
    assert gateway.count(PatientRow) == 25


def test_transient_html_body_is_kept_retried_and_null_element_quarantined(gateway):
    gateway.scenario("malformed")
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)

    assert report.status is SyncStatus.COMPLETED
    assert (report.inserted, report.quarantined) == (24, 1)
    html = gateway.all(RawPayload, RawPayload.content_type.like("text/html%"))
    assert len(html) == 1, "the malformed body was stored before parsing"
    assert html[0].body.startswith(b"<html>")
    [row] = gateway.all(Quarantine)
    assert row.error_codes == [ErrorCode.MALFORMED_RECORD] and row.record_index == 3


def test_persistent_html_goes_to_request_dlq_and_is_refetched_later(gateway):
    gateway.scenario("malformed_always")
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)

    assert report.status is SyncStatus.HALTED
    assert report.halt_reason.startswith(ErrorCode.MALFORMED_RESPONSE)
    [dlq] = gateway.all(RequestDLQ)
    assert (dlq.failure_code, dlq.attempts, dlq.status) == (
        ErrorCode.MALFORMED_RESPONSE,
        5,
        DlqStatus.OPEN,
    )
    assert dlq.raw_payload_id is not None
    assert gateway.count(RawPayload) == 5, "every attempt's bytes are evidence"
    breaker = gateway.all(CircuitState)[0]
    assert breaker.state == BreakerState.CLOSED and breaker.consecutive_failures == 1

    gateway.scenario("v1")
    recovered = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert recovered.status is SyncStatus.COMPLETED and recovered.inserted == 25
    assert gateway.all(RequestDLQ)[0].status == DlqStatus.REFETCHED
    assert gateway.all(CircuitState)[0].consecutive_failures == 0
    assert gateway.all(Incident)[0].status == IncidentStatus.CLOSED


def test_repeated_upstream_failures_eventually_open_the_breaker(gateway):
    gateway.scenario("malformed_always")
    for _ in range(3):
        gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert gateway.all(CircuitState)[0].state == BreakerState.OPEN
    [incident] = gateway.all(Incident)
    assert incident.count == 3


def test_non_retryable_4xx_is_not_retried_and_opens_breaker(gateway):
    report = gateway.orchestrator.sync("practice_zzz", Resource.PATIENT)
    assert report.status is SyncStatus.HALTED and report.retries == 0
    [dlq] = gateway.all(RequestDLQ)
    assert (dlq.failure_code, dlq.http_status, dlq.attempts) == (ErrorCode.CONTRACT_OR_AUTH, 404, 1)
    assert gateway.all(CircuitState)[0].state == BreakerState.OPEN


def test_pagination_loop_is_detected(gateway):
    gateway.scenario("pagination")
    report = gateway.orchestrator.sync("practice_a", Resource.PATIENT)
    assert report.status is SyncStatus.HALTED and report.halt_reason == ErrorCode.PAGINATION_LOOP
    assert report.pages == 2
    assert gateway.count(PatientRow) == 20, "pages 1 and 2 written once each"
    assert gateway.count(Quarantine, Quarantine.status == QuarantineStatus.QUARANTINED) == 0
    assert gateway.all(CircuitState)[0].state == BreakerState.OPEN
