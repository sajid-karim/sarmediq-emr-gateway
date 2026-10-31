"""COMPATIBLE / DEGRADED / INCOMPATIBLE / PENDING_DEPENDENCY (ARCHITECTURE §4)."""

from app.core.types import Outcome
from app.vendors.base import MapResult


def classify(result: MapResult, parent_exists: bool | None) -> Outcome:
    """Most dangerous check first. parent_exists is None when the resource has no parent."""
    if result.record is None or result.blocking_issues:
        return Outcome.INCOMPATIBLE
    if parent_exists is False:
        return Outcome.PENDING_DEPENDENCY
    if result.issues:
        return Outcome.DEGRADED
    return Outcome.COMPATIBLE


def is_batch_unhealthy(total: int, bad: int, *, min_records: int, max_bad_ratio: float) -> bool:
    """Too many bad records on one page means the contract changed, not the data."""
    return total >= min_records and bad / total > max_bad_ratio
