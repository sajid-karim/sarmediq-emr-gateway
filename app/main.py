import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api import ops_router, read_router, sync_router
from app.config import Settings
from app.db.models import Tenant
from app.db.session import init_db, make_engine, make_session_factory, seed_tenants
from app.logging import configure_logging
from app.sync.orchestrator import SyncOrchestrator, UnknownTenantError
from app.vendors.base import VendorAdapter
from app.vendors.registry import ADAPTER_FACTORIES


@dataclass
class Services:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    orchestrators: dict[str, SyncOrchestrator]  # one per vendor, keyed by vendor name

    def orchestrator_for(self, tenant_name: str) -> SyncOrchestrator:
        """A tenant belongs to one vendor; that vendor's adapter does the talking."""
        with self.session_factory() as session:
            vendor = session.scalar(select(Tenant.vendor).where(Tenant.name == tenant_name))
        if vendor is None:
            raise UnknownTenantError(tenant_name)
        if vendor not in self.orchestrators:
            raise RuntimeError(f"tenant {tenant_name!r} uses vendor {vendor!r}, no adapter loaded")
        return self.orchestrators[vendor]

    def close(self) -> None:
        for orchestrator in self.orchestrators.values():
            orchestrator.adapter.close()
        self.engine.dispose()


def build_services(
    settings: Settings,
    *,
    adapters: Mapping[str, VendorAdapter] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Services:
    vendors = {tenant.vendor for tenant in settings.tenants}
    if adapters is None:
        unsupported = vendors - ADAPTER_FACTORIES.keys()
        if unsupported:
            raise ValueError(f"no adapter registered for vendor(s): {sorted(unsupported)}")
        adapters = {vendor: ADAPTER_FACTORIES[vendor](settings) for vendor in sorted(vendors)}
    elif missing := vendors - adapters.keys():
        raise ValueError(f"no adapter supplied for vendor(s): {sorted(missing)}")

    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    seed_tenants(session_factory, settings.tenants)
    orchestrators = {
        vendor: SyncOrchestrator(session_factory, adapter, settings, sleep=sleep)
        for vendor, adapter in adapters.items()
    }
    return Services(settings, engine, session_factory, orchestrators)


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
