"""Two vendors in one gateway: each tenant is served by its own vendor's adapter.

"Globex" is a second vendor for wiring purposes only: it reuses the Acme client and mapper
against the same mock, but has its own vendor name, so its data, baselines, breakers and
replay are kept apart from Acme's.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, TenantSeed
from app.core.types import Resource
from app.db.models import CircuitState, PatientRow, SchemaFingerprint
from app.main import build_services, create_app
from app.sync.orchestrator import SyncStatus, UnknownTenantError
from app.vendors.acme.adapter import AcmeAdapter
from app.vendors.acme.client import AcmeClient
from app.vendors.registry import build_acme_adapter
from tests.conftest import set_scenario


class GlobexAdapter(AcmeAdapter):
    vendor = "globex"


@pytest.fixture
def services(tmp_path, mock_url):
    set_scenario(mock_url, "v1")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/multi.db",
        vendor_base_url=mock_url,
        tenants=[
            TenantSeed(
                name="practice_a",
                vendor="acme",
                external_key="practice_a",
                locator={"practice_id": "practice_a"},
            ),
            TenantSeed(
                name="globex_clinic",
                vendor="globex",
                external_key="gx-77",
                locator={"practice_id": "practice_b"},
            ),
        ],
    )
    client = AcmeClient(mock_url, page_size=10, connect_timeout=1, read_timeout=1)
    adapters = {"acme": build_acme_adapter(settings), "globex": GlobexAdapter(client)}
    services = build_services(settings, adapters=adapters, sleep=lambda _: None)
    yield services
    services.close()
    set_scenario(mock_url, "v1")


def test_each_tenant_is_routed_to_its_vendors_orchestrator(services):
    assert services.orchestrator_for("practice_a").adapter.vendor == "acme"
    assert services.orchestrator_for("globex_clinic").adapter.vendor == "globex"
    with pytest.raises(UnknownTenantError):
        services.orchestrator_for("nobody")


def test_vendors_keep_separate_data_baselines_and_breakers(services):
    for tenant in ("practice_a", "globex_clinic"):
        report = services.orchestrator_for(tenant).sync(tenant, Resource.PATIENT)
        assert report.status is SyncStatus.COMPLETED and report.inserted == 25

    with services.session_factory() as session:
        rows = session.query(PatientRow).filter_by(vendor_id="P-1001").all()
        assert {row.vendor for row in rows} == {"acme", "globex"}
        baselines = session.query(SchemaFingerprint).filter_by(is_baseline=True).all()
        assert {b.vendor for b in baselines} == {"acme", "globex"}
        assert {c.vendor for c in session.query(CircuitState).all()} == {"acme", "globex"}


def test_an_orchestrator_refuses_another_vendors_tenant(services):
    with pytest.raises(UnknownTenantError):
        services.orchestrators["acme"].sync("globex_clinic", Resource.PATIENT)


def test_api_routes_sync_and_replay_by_vendor(services):
    with TestClient(create_app(services)) as client:
        response = client.post("/sync/patient", params={"tenant": "globex_clinic"})
        assert response.status_code == 200 and response.json()["inserted"] == 25

        ambiguous = client.post("/replay")
        assert ambiguous.status_code == 422, "two vendors: must say which"
        assert client.post("/replay", params={"vendor": "globex"}).status_code == 200
        assert client.post("/replay", params={"tenant": "globex_clinic"}).status_code == 200
        assert client.post("/replay", params={"vendor": "nope"}).status_code == 404


def test_startup_fails_fast_when_a_tenants_vendor_has_no_adapter(tmp_path, mock_url):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/bad.db",
        vendor_base_url=mock_url,
        tenants=[TenantSeed(name="x", vendor="initech", external_key="x", locator={})],
    )
    with pytest.raises(ValueError, match="initech"):
        build_services(settings)
