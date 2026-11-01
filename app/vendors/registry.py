"""Which adapter serves which vendor. Adding a vendor = one factory here + its package."""

from collections.abc import Callable

from app.config import Settings
from app.vendors.acme.adapter import AcmeAdapter
from app.vendors.acme.client import AcmeClient
from app.vendors.base import VendorAdapter


def build_acme_adapter(settings: Settings, mapping_version: int | None = None) -> AcmeAdapter:
    client = AcmeClient(
        settings.vendor_base_url,
        page_size=settings.page_size,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
    )
    return AcmeAdapter(client, mapping_version or settings.acme_mapping_version)


ADAPTER_FACTORIES: dict[str, Callable[[Settings], VendorAdapter]] = {
    "acme": build_acme_adapter,
}
