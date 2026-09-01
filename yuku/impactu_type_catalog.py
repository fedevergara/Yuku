"""Runtime loader for the exact, versioned ImpactU entity catalog."""

from __future__ import annotations

import csv
from functools import lru_cache
import html
from importlib import resources
from io import StringIO
import re
from typing import Any
import unicodedata


CATALOG_RESOURCE = "data/tipos_impactu_v1.csv"
CATALOG_SCHEMA_VERSION = "impactu-type-routing-v1"
CATALOG_ENTITIES = frozenset({"works", "projects", "patents", "events"})


def exact_key(value: Any) -> str:
    """Normalize presentation only; accents and semantic words remain significant."""
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", text).strip()


class ImpactuTypeCatalog:
    def __init__(self, payload: dict[str, Any]):
        if payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise ValueError("unsupported ImpactU catalog schema")
        mappings = payload.get("mappings")
        if not isinstance(mappings, list) or not mappings:
            raise ValueError("ImpactU catalog has no mappings")
        declared_rows = int((payload.get("source") or {}).get("rows") or 0)
        if declared_rows != len(mappings):
            raise ValueError("ImpactU catalog row count does not match its metadata")

        index: dict[tuple[str, str], dict[str, str]] = {}
        for position, raw in enumerate(mappings, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"invalid ImpactU mapping at position {position}")
            mapping = {
                "source": str(raw.get("source") or "").strip(),
                "type": str(raw.get("type") or "").strip(),
                "type_impactu": str(raw.get("type_impactu") or "").strip(),
                "entity": str(raw.get("entity") or "").strip(),
            }
            if not all(mapping.values()) or mapping["entity"] not in CATALOG_ENTITIES:
                raise ValueError(f"invalid ImpactU mapping at position {position}")
            key = (exact_key(mapping["source"]), exact_key(mapping["type"]))
            if key in index:
                raise ValueError(f"duplicate ImpactU mapping: {key!r}")
            index[key] = mapping

        self.payload = payload
        self._index = index
        self.version = str(payload.get("catalog_version") or "")
        self.source_sha256 = str((payload.get("source") or {}).get("sha256") or "")

    def lookup(self, source: Any, native_type: Any) -> dict[str, str] | None:
        mapping = self._index.get((exact_key(source), exact_key(native_type)))
        return dict(mapping) if mapping else None

    def classify_minciencias(
        self, product_class: Any, typology: Any
    ) -> dict[str, str] | None:
        native_type = f"{str(product_class or '').strip()}: {str(typology or '').strip()}"
        return self.lookup("minciencias", native_type)

    def __len__(self) -> int:
        return len(self._index)


@lru_cache(maxsize=1)
def get_impactu_catalog() -> ImpactuTypeCatalog:
    package_root = resources.files("yuku")
    text = package_root.joinpath(CATALOG_RESOURCE).read_text(encoding="utf-8")
    metadata: dict[str, str] = {}
    csv_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("# "):
            key, separator, value = line[2:].partition("=")
            if separator:
                metadata[key.strip()] = value.strip()
        elif line.strip():
            csv_lines.append(line)
    reader = csv.DictReader(StringIO("\n".join(csv_lines)))
    mappings = [dict(row) for row in reader]
    payload = {
        "schema_version": metadata.get("schema_version", ""),
        "catalog_version": metadata.get("catalog_version", ""),
        "source": {
            "workbook": metadata.get("workbook", ""),
            "sheet": metadata.get("sheet", ""),
            "sha256": metadata.get("sha256", ""),
            "rows": int(metadata.get("rows", "0")),
        },
        "entities": sorted(CATALOG_ENTITIES),
        "mappings": mappings,
    }
    return ImpactuTypeCatalog(payload)
