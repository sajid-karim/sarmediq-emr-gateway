from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TenantSeed(BaseModel):
    name: str
    vendor: str
    external_key: str
    locator: dict[str, Any]


def _default_tenants() -> list[TenantSeed]:
    return [
        TenantSeed(name=p, vendor="acme", external_key=p, locator={"practice_id": p})
        for p in ("practice_a", "practice_b")
    ]


class Settings(BaseSettings):
    """All runtime knobs. Override with EMR_<NAME> environment variables."""

    model_config = SettingsConfigDict(env_prefix="EMR_", extra="ignore")

    database_url: str = "sqlite:///./gateway.db"
    vendor_base_url: str = "http://localhost:9000"
    acme_mapping_version: int = 1
    page_size: int = 10

    connect_timeout: float = 3.0
    read_timeout: float = 10.0
    max_attempts: int = 5
    backoff_base: float = 0.5
    backoff_cap: float = 30.0
    retry_after_cap: float = 30.0

    batch_min_records: int = 10
    batch_max_bad_ratio: float = 0.2
    breaker_failure_threshold: int = 3
    pending_max_attempts: int = 5
    sync_lock_ttl_seconds: int = 900

    log_level: str = "INFO"
    tenants: list[TenantSeed] = Field(default_factory=_default_tenants)
