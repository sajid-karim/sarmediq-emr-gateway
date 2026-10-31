import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import ops_router, read_router, sync_router
from app.config import Settings
from app.db.session import init_db, make_engine, make_session_factory, seed_tenants
from app.logging import configure_logging
from app.sync.orchestrator import SyncOrchestrator
from app.vendors.acme.adapter import AcmeAdapter
from app.vendors.acme.client import AcmeClient
from app.vendors.base import VendorAdapter


@dataclass
class Services:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    orchestrator: SyncOrchestrator

    def close(self) -> None:
        self.orchestrator.adapter.close()
        self.engine.dispose()


def build_acme_adapter(settings: Settings, mapping_version: int | None = None) -> AcmeAdapter:
    client = AcmeClient(
        settings.vendor_base_url,
        page_size=settings.page_size,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
    )
    return AcmeAdapter(client, mapping_version or settings.acme_mapping_version)


def build_services(
    settings: Settings,
    *,
    adapter: VendorAdapter | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Services:
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    seed_tenants(session_factory, settings.tenants)
    orchestrator = SyncOrchestrator(
        session_factory, adapter or build_acme_adapter(settings), settings, sleep=sleep
    )
    return Services(settings, engine, session_factory, orchestrator)


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = services.settings if services else Settings()
        configure_logging(settings.log_level)
        app.state.services = services or build_services(settings)
        yield
        if services is None:
            app.state.services.close()

    app = FastAPI(title="EMR Integration Gateway", lifespan=lifespan)
    app.include_router(sync_router.router)
    app.include_router(ops_router.router)
    app.include_router(read_router.router)
    return app


app = create_app()
