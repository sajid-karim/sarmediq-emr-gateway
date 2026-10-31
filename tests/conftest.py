import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, TenantSeed
from app.main import Services, build_acme_adapter, build_services
from app.sync.orchestrator import SyncOrchestrator
from mock_emr.app import app as mock_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def mock_url() -> Iterator[str]:
    """The mock vendor as a real HTTP server, so timeouts and 429s are real network behaviour."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(mock_app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock server did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def set_scenario(mock_url: str, scenario: str, slow_seconds: float = 1.0) -> None:
    response = httpx.post(
        f"{mock_url}/_admin/scenario", json={"scenario": scenario, "slow_seconds": slow_seconds}
    )
    response.raise_for_status()


@dataclass
class Gateway:
    services: Services
    mock_url: str

    @property
    def sessions(self) -> sessionmaker[Session]:
        return self.services.session_factory

    @property
    def orchestrator(self) -> SyncOrchestrator:
        return self.services.orchestrator

    def scenario(self, name: str, slow_seconds: float = 1.0) -> None:
        set_scenario(self.mock_url, name, slow_seconds)

    def with_mapping_version(self, version: int) -> SyncOrchestrator:
        """Simulates deploying a fixed mapper: same DB, same settings, new adapter."""
        adapter = build_acme_adapter(self.services.settings, mapping_version=version)
        return SyncOrchestrator(
            self.sessions, adapter, self.services.settings, sleep=lambda _: None
        )

    def count(self, model: Any, *where: Any) -> int:
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(model).where(*where)) or 0

    def all(self, model: Any, *where: Any) -> list[Any]:
        with self.sessions() as session:
            return list(session.scalars(select(model).where(*where).order_by(model.id)).all())


def test_settings(tmp_path, mock_url: str) -> Settings:
    tenants = [
        TenantSeed(name=p, vendor="acme", external_key=p, locator={"practice_id": p})
        for p in ("practice_a", "practice_b", "practice_zzz")
    ]
    return Settings(
        database_url=f"sqlite:///{tmp_path}/gateway.db",
        vendor_base_url=mock_url,
        connect_timeout=1.0,
        read_timeout=0.5,
        backoff_base=0.001,
        backoff_cap=0.01,
        retry_after_cap=0.01,
        tenants=tenants,
    )


@pytest.fixture
def gateway(tmp_path, mock_url: str) -> Iterator[Gateway]:
    set_scenario(mock_url, "v1")
    services = build_services(test_settings(tmp_path, mock_url), sleep=lambda _: None)
    yield Gateway(services, mock_url)
    services.close()
    set_scenario(mock_url, "v1")
