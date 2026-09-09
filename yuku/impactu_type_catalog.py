"""Compatibility exports for the shared ImpactU type catalog.

New code should import these names directly from ``kahi_impactu_type_catalog``.
"""

from kahi_impactu_type_catalog import (
    CATALOG_ENTITIES,
    CATALOG_SCHEMA_VERSION,
    ImpactuTypeCatalog,
    exact_key,
    get_impactu_catalog,
)

__all__ = [
    "CATALOG_ENTITIES",
    "CATALOG_SCHEMA_VERSION",
    "ImpactuTypeCatalog",
    "exact_key",
    "get_impactu_catalog",
]
