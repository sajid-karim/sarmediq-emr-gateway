"""Vendor-agnostic fetch retry policy (ARCHITECTURE §5).

The adapter makes exactly one HTTP attempt; this module decides whether to try again.
Every 2xx body is stored *before* we try to parse it, so a malformed body is never lost.
"""

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from app.core.clock import utcnow
from app.core.errors import ErrorCode
from app.vendors.base import RawResponse, VendorTransportError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_base: float
    backoff_cap: float
    retry_after_cap: float


@dataclass(frozen=True)
class FetchResult:
    payload: Any = None
    response: RawResponse | None = None
    raw_payload_id: int | None = None
    attempts: int = 0
    failure: ErrorCode | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def backoff_delay(
    attempt: int, base: float, cap: float, rand: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with full jitter: random(0, min(cap, base * 2^attempt))."""
    return rand() * min(cap, base * (2**attempt))


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date (RFC 9110 §10.2.3)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


def fetch_with_retry(
    fetch: Callable[[], RawResponse],
    store: Callable[[RawResponse], int],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utcnow,
    rand: Callable[[], float] = random.random,
    on_retry: Callable[[int, str, float], None] | None = None,
) -> FetchResult:
    last_code, last_detail = ErrorCode.UPSTREAM_UNAVAILABLE, "no attempt made"
    last_response: RawResponse | None = None
    last_raw_id: int | None = None

    for attempt in range(policy.max_attempts):
        delay = backoff_delay(attempt, policy.backoff_base, policy.backoff_cap, rand)
        try:
            response = fetch()
        except VendorTransportError as exc:
            last_code, last_detail, last_response, last_raw_id = (
                ErrorCode.UPSTREAM_UNAVAILABLE,
                str(exc),
                None,
                None,
            )
        else:
            status = response.status_code
            if 200 <= status < 300:
                raw_id = store(response)
                try:
                    payload = json.loads(response.body)
                except ValueError:  # JSONDecodeError and UnicodeDecodeError
                    content_type = response.headers.get("content-type", "?")
                    last_code = ErrorCode.MALFORMED_RESPONSE
                    last_detail = f"HTTP {status} body is not JSON (content-type: {content_type})"
                    last_response, last_raw_id = response, raw_id
                else:
                    return FetchResult(
                        payload=payload,
                        response=response,
                        raw_payload_id=raw_id,
                        attempts=attempt + 1,
                    )
            elif status == 429:
                last_code, last_detail = ErrorCode.UPSTREAM_UNAVAILABLE, "HTTP 429 rate limited"
                last_response, last_raw_id = response, None
                retry_after = parse_retry_after(response.headers.get("retry-after"), now())
                if retry_after is not None:
                    delay = min(retry_after, policy.retry_after_cap)
            elif status >= 500 or status == 408:
                last_code, last_detail = ErrorCode.UPSTREAM_UNAVAILABLE, f"HTTP {status}"
                last_response, last_raw_id = response, None
            else:
                # 400/401/403/404/422...: the same request will fail the same way. Needs a human.
                raw_id = store(response)
                return FetchResult(
                    response=response,
                    raw_payload_id=raw_id,
                    attempts=attempt + 1,
                    failure=ErrorCode.CONTRACT_OR_AUTH,
                    detail=f"HTTP {status} is not retryable",
                )

        if attempt + 1 < policy.max_attempts:
            if on_retry:
                on_retry(attempt + 1, last_detail, delay)
            sleep(delay)

    return FetchResult(
        response=last_response,
        raw_payload_id=last_raw_id,
        attempts=policy.max_attempts,
        failure=last_code,
        detail=last_detail,
    )
