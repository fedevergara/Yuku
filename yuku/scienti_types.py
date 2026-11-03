"""Minciencias types: official measurements and a separate scraping catalog.

Provider labels do not select a vocabulary. Complete native context is matched
against the official catalog and the reviewable CvLAC/GrupLAC catalog.
"""

from functools import lru_cache

from kahi_impactu_type_catalog import exact_key, get_impactu_catalog
from yuku.scraping_type_catalog import (
    ALIASES, POLICY_VERSION, get_scraping_catalog, label_key, path_labels,
)

TYPE_POLICY_VERSION = POLICY_VERSION
CATALOG = get_impactu_catalog()
IMPACTU_TYPES = {exact_key(value): value for value in CATALOG.payload["impactu_types"]}
ENTITY_TYPES = {
    entity: {m["type_impactu"] for m in CATALOG.payload["mappings"] if m["entity"] == entity}
    for entity in CATALOG.payload["entities"]
}
MINCIENCIAS_CLASSES = {
    exact_key(m["type"].split(": ", 1)[0]): m["type"].split(": ", 1)[0]
    for m in CATALOG.payload["mappings"] if m["source"] == "minciencias"
}


@lru_cache(maxsize=4096)
def native_type(value, section=""):
    return get_scraping_catalog().resolve(value, section)


def classify_native(value, section=""):
    result = native_type(value, section) if value else native_type(section, section)
    return result["mapping"]["type_impactu"] if result["mapping"] else ""


def validate_types(entries, *, entity="works", normalize_provider=True):
    """Normalize each source's own vocabulary; return valid types and findings."""
    if entries is not None and not isinstance(entries, list):
        return [], [{"reason": "invalid_type_container", "raw_type": entries}]
    accepted, findings = [], []
    for entry in entries or []:
        if not isinstance(entry, dict):
            findings.append({"reason": "invalid_type_shape", "raw_type": entry})
            continue
        source, value = entry.get("source"), entry.get("type")
        if (not isinstance(source, str) or not isinstance(value, str) or not value.strip()
                or entry.get("parent") is not None and not isinstance(entry["parent"], str)):
            findings.append({"reason": "invalid_type_shape", "raw_type": entry})
            continue
        cleaned = dict(entry)
        if normalize_provider and cleaned.get("provenance") in {"scienti", "cvlac", "gruplac"}:
            cleaned["provenance"] = "minciencias"
        reason = None
        if source == "impactu":
            canonical = IMPACTU_TYPES.get(exact_key(value))
            if canonical is None:
                canonical = IMPACTU_TYPES.get(label_key(value))
            if canonical is None:
                reason = "unmapped_impactu_type"
            elif canonical not in ENTITY_TYPES[entity]:
                reason = "type_belongs_to_other_entity"
            else:
                cleaned["type"] = canonical
        elif source in {"cvlac", "gruplac", "scienti", "minciencias"}:
            # Legacy Yuku labels are accepted as input only. The catalog's
            # evidence source never becomes the provider of a scraped value.
            cleaned["source"] = "minciencias" if normalize_provider else source
            if normalize_provider and "provenance" in cleaned:
                cleaned["provenance"] = "minciencias"
            if source == "minciencias" and entry.get("level") == 0:
                if exact_key(value) in MINCIENCIAS_CLASSES:
                    cleaned["type"] = MINCIENCIAS_CLASSES[exact_key(value)]
                else:
                    reason = "unmapped_minciencias_class"
            else:
                result = native_type(value, entry.get("parent") or "")
                official = CATALOG.classify_minciencias(entry.get("parent"), value) if source == "minciencias" else None
                mapping = official or result["mapping"]
                if official and result["mapping"] and any(
                    official[k] != result["mapping"][k] for k in ("entity", "type_impactu")
                ):
                    reason = "ambiguous_minciencias_type"
                elif mapping and mapping["entity"] != entity:
                    reason = "type_belongs_to_other_entity"
                elif not official and not result["native"]:
                    reason = "unmapped_native_type"
                elif not official and result["status"] == "ambiguous":
                    reason = "ambiguous_native_type"
                elif not official:
                    cleaned["type"] = result["native"]
        else:
            mapping = CATALOG.lookup(source, value)
            if not mapping or mapping["entity"] != entity:
                reason = "unmapped_type_source"
        if reason:
            findings.append({"reason": reason, "source": source, "raw_type": value,
                             "parent": entry.get("parent"), "level": entry.get("level")})
        elif cleaned not in accepted:
            accepted.append(cleaned)
    return accepted, findings


def materialize_types(document, *, entity="works"):
    accepted, findings = validate_types(document.get("types"), entity=entity)
    document["types"] = accepted
    if findings:
        audit = document.setdefault("type_audit", {})
        audit.update(policy=TYPE_POLICY_VERSION, catalog_version=CATALOG.version,
                     catalog_sha256=CATALOG.source_sha256,
                     scraping_catalog_version=get_scraping_catalog().version,
                     scraping_catalog_sha256=get_scraping_catalog().fingerprint)
        evidence = audit.setdefault("findings", [])
        for finding in findings:
            if finding not in evidence:
                evidence.append(finding)
    return document


def type_anomalies(entries, *, entity="works"):
    accepted, findings = validate_types(entries, entity=entity)
    return len(findings) + int(accepted != (entries or []))
