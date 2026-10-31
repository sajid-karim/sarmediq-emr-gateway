from collections.abc import Iterable

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import TenantSeed
from app.db.models import Base, Tenant


def make_engine(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    # FastAPI runs `def` endpoints in a threadpool, so connections cross threads.
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            cursor.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; avoids an fsync per commit
            cursor.execute("PRAGMA busy_timeout=5000")  # a second writer waits instead of failing
            cursor.execute("PRAGMA foreign_keys=ON")  # SQLite ignores FKs unless asked
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def seed_tenants(session_factory: sessionmaker[Session], tenants: Iterable[TenantSeed]) -> None:
    with session_factory() as session:
        for seed in tenants:
            existing = session.scalar(select(Tenant).where(Tenant.name == seed.name))
            if existing is None:
                session.add(
                    Tenant(
                        name=seed.name,
                        vendor=seed.vendor,
                        external_key=seed.external_key,
                        locator=seed.locator,
                    )
                )
        session.commit()
