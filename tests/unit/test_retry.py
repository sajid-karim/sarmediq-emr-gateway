from datetime import UTC, datetime

from app.core.errors import ErrorCode
from app.core.retry import RetryPolicy, backoff_delay, fetch_with_retry, parse_retry_after
from app.vendors.base import RawResponse, VendorTransportError

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
POLICY = RetryPolicy(max_attempts=5, backoff_base=0.5, backoff_cap=30, retry_after_cap=10)


def response(status=200, body=b"[]", headers=None):
    return RawResponse(status, headers or {}, body, "http://vendor/x")


def run(responses, policy=POLICY):
    queue = list(responses)
    stored, sleeps = [], []

    def fetch():
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def store(resp):
        stored.append(resp)
        return len(stored)

    result = fetch_with_retry(
        fetch, store, policy, sleep=sleeps.append, now=lambda: NOW, rand=lambda: 1.0
    )
    return result, stored, sleeps


def test_backoff_is_exponential_capped_and_jittered():
    assert [backoff_delay(n, 0.5, 30, rand=lambda: 1.0) for n in range(8)] == [
        0.5,
        1,
        2,
        4,
        8,
        16,
        30,
        30,
    ]
    assert backoff_delay(3, 0.5, 30, rand=lambda: 0.25) == 1.0
    assert backoff_delay(3, 0.5, 30, rand=lambda: 0.0) == 0.0


def test_retry_after_seconds_and_http_date():
    assert parse_retry_after("7", NOW) == 7.0
    assert parse_retry_after("Sun, 01 Nov 2026 12:00:30 GMT", NOW) == 30.0
    assert parse_retry_after("Sun, 01 Nov 2026 11:00:00 GMT", NOW) == 0.0
    assert parse_retry_after("soon", NOW) is None
    assert parse_retry_after(None, NOW) is None


def test_success_first_time():
    result, stored, sleeps = run([response(body=b'[{"id": "1"}]')])
    assert result.ok and result.payload == [{"id": "1"}] and result.attempts == 1
    assert len(stored) == 1 and sleeps == []


def test_5xx_then_success():
    result, stored, sleeps = run([response(503), response(500), response()])
    assert result.ok and result.attempts == 3
    assert sleeps == [0.5, 1.0]
    assert len(stored) == 1, "error bodies of transient failures are not evidence"


def test_429_honours_retry_after_but_caps_it():
    result, _, sleeps = run(
        [
            response(429, headers={"retry-after": "3"}),
            response(429, headers={"retry-after": "999"}),
            response(),
        ]
    )
    assert result.ok and sleeps == [3.0, 10.0]


def test_timeout_is_retried():
    result, _, _ = run([VendorTransportError("timeout"), response()])
    assert result.ok and result.attempts == 2


def test_malformed_body_is_stored_then_retried():
    result, stored, _ = run([response(body=b"<html>502</html>"), response(body=b"[]")])
    assert result.ok and len(stored) == 2 and stored[0].body.startswith(b"<html>")


def test_malformed_every_time_gives_up_with_evidence():
    result, stored, sleeps = run([response(body=b"") for _ in range(5)])
    assert result.failure is ErrorCode.MALFORMED_RESPONSE
    assert result.attempts == 5 and len(stored) == 5 and len(sleeps) == 4
    assert result.raw_payload_id == 5


def test_4xx_is_not_retried():
    result, stored, sleeps = run([response(401, body=b'{"error":"unauthorized"}')])
    assert result.failure is ErrorCode.CONTRACT_OR_AUTH
    assert result.attempts == 1 and sleeps == [] and len(stored) == 1


def test_exhausted_transient_failures():
    result, _, sleeps = run([response(503)] * 5)
    assert result.failure is ErrorCode.UPSTREAM_UNAVAILABLE and len(sleeps) == 4
