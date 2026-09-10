"""Exact entity routing shared by Scienti normalizers and graph builders.

The channel/section is part of the source contract.  No substring, fuzzy or
accent-insensitive decision is made here.  The authoritative entity vocabulary
and its version come from ``Tipos_ImpactU_Definitivo.xlsx`` through the shared
``Kahi_impactu_type_catalog`` package.
"""

from __future__ import annotations

from typing import Any

from kahi_impactu_type_catalog import exact_key, get_impactu_catalog


ENTITY_ROUTER_VERSION = "scienti-exact-router-v5"
IMPACTU_CATALOG = get_impactu_catalog()

CVLAC_THESIS_TYPES = {
    exact_key("Trabajos dirigidos/Tutorías - Trabajos de grado de pregrado"): {
        "family": "undergraduate_thesis",
        "impactu_type": "Tesis de Pregrado",
    },
    exact_key("Trabajos dirigidos/Tutorías - Trabajo de grado de maestría o especialidad clínica"): {
        "family": "graduate_thesis",
        "impactu_type": "Tesis de Posgrado",
    },
    exact_key("Trabajos dirigidos/Tutorías - Tesis de doctorado"): {
        "family": "graduate_thesis",
        "impactu_type": "Tesis de Posgrado",
    },
    exact_key("Trabajos dirigidos/Tutorías - Monografía de conclusión de curso de perfeccionamiento/especialización"): {
        "family": "graduate_degree_work",
        "impactu_type": "Docencia",
    },
}
GRUPLAC_THESIS_TYPES = {
    exact_key("Trabajos de grado de pregrado"): {
        "family": "undergraduate_thesis",
        "impactu_type": "Tesis de Pregrado",
    },
    exact_key("Trabajo de grado de maestría o especialidad clínica"): {
        "family": "graduate_thesis",
        "impactu_type": "Tesis de Posgrado",
    },
    exact_key("Tesis de doctorado"): {
        "family": "graduate_thesis",
        "impactu_type": "Tesis de Posgrado",
    },
    exact_key("Monografía de conclusión de curso de perfeccionamiento/especialización"): {
        "family": "graduate_degree_work",
        "impactu_type": "Docencia",
    },
}
CVLAC_EXCLUDED_DIRECTED_TYPES = {
    exact_key("Trabajos dirigidos/Tutorías - Trabajos dirigidos/Tutorías de otro tipo"),
    exact_key("Trabajos dirigidos/Tutorías - Iniciación Científica"),
}
GRUPLAC_EXCLUDED_DIRECTED_TYPES = {
    exact_key("Trabajos dirigidos/Tutorías de otro tipo"),
    exact_key("Iniciación Científica"),
}
CVLAC_PROJECT_TYPES = {
    exact_key(value)
    for value in (
        "Investigación y desarrollo",
        "Extensión y responsabilidad social CTI",
        "Investigación, desarrollo e Innovación",
        "Investigación-Creación",
    )
}
GRUPLAC_PROJECT_TYPES = {
    exact_key(value)
    for value in (
        "Investigación y desarrollo",
        "Investigación, desarrollo e innovación",
        "Extensión y responsabilidad social CTI",
        "Investigación + Creación",
    )
}
EVENT_TYPES = {
    exact_key(value)
    for value in ("Congreso", "Encuentro", "Seminario", "Simposio", "Taller", "Otro")
}
CVLAC_PATENT_TYPES = {
    exact_key(value)
    for value in (
        "Patente",
        "Patente de invención",
        "Patente de modelo de utilidad",
        "Modelo de utilidad",
        "Otra Patente",
        "Patente de Modelo Industrial",
        "Patente de Privilegio de Innovación",
        "Patente en el Exterior",
    )
}

GRUPLAC_DIRECTED_SECTIONS = {
    exact_key("Trabajos dirigidos/turorías"),
    exact_key("Trabajos dirigidos/tutorías"),
}
GRUPLAC_PROJECT_SECTION = exact_key("Proyectos")
GRUPLAC_EVENT_SECTION = exact_key("Eventos Científicos")

# The Excel authority assigns distinctive signs to ``patents``.
GRUPLAC_PATENT_IP_TYPES = {
    (exact_key("Signos distintivos"), exact_key("Marcas")): "trademark",
    (exact_key("Signos distintivos"), exact_key("Marcas colectivas")): "trademark",
    (exact_key("Signos distintivos"), exact_key("Nombres comerciales")): "trade_name",
    (exact_key("Signos distintivos"), exact_key("Marcas de certificación")): "trademark",
    (exact_key("Signos distintivos"), exact_key("Denominaciones de origen")): "appellation_of_origin",
    (exact_key("Signos distintivos"), exact_key("Lemas comerciales")): "commercial_slogan",
    (exact_key("Signos distintivos"), exact_key("Enseñas comerciales")): "business_sign",
}

# These are intellectual-property-like source sections, but the Excel authority
# explicitly keeps them in ``works``.
GRUPLAC_WORK_IP_TYPES = {
    (exact_key("Diseños industriales"), exact_key("Diseño Industrial")): "industrial_design",
    (exact_key("Nuevas variedades vegetal"), exact_key("Variedad vegetal")): "plant_variety",
    (exact_key("Nuevas variedades animal"), exact_key("Variedad animal")): "animal_variety",
    (
        exact_key("Esquemas de trazados de circuito integrado"),
        exact_key("Esquema de circuito integrado"),
    ): "integrated_circuit_layout",
}

# Compatibility export used by audits and existing callers.  It intentionally
# contains only routes whose target is the patents collection.
GRUPLAC_IP_TYPES = GRUPLAC_PATENT_IP_TYPES

SPECIALIZED_IMPACTU_TYPES = {
    exact_key("Proyecto"): "projects",
    exact_key("Proyectos"): "projects",
    exact_key("Patente"): "patents",
    exact_key("Evento"): "events",
}


def _route(
    entity: str,
    family: str,
    rule: str,
    *,
    impactu_type: Any = "",
    ip_namespace: str = "",
) -> dict[str, str]:
    result = {
        "entity": entity,
        "family": family,
        "impactu_type": str(impactu_type or "").strip(),
        "rule": rule,
        "catalog_version": IMPACTU_CATALOG.version,
        "catalog_sha256": IMPACTU_CATALOG.source_sha256,
    }
    if ip_namespace:
        result["ip_namespace"] = ip_namespace
    return result


def route_cvlac(channel: str, record: dict[str, Any]) -> dict[str, str] | None:
    """Route a CVLAC occurrence through exact channel/type contracts."""
    channel = str(channel or "").strip()
    if channel == "production":
        product_key = exact_key(record.get("product_type"))
        thesis = CVLAC_THESIS_TYPES.get(product_key)
        if thesis:
            return _route("works", thesis["family"], "cvlac_exact_thesis_type", impactu_type=thesis["impactu_type"])
        specialized = SPECIALIZED_IMPACTU_TYPES.get(exact_key(record.get("type_impactu")))
        if specialized:
            return _route(
                specialized,
                specialized[:-1] if specialized.endswith("s") else specialized,
                "cvlac_exact_impactu_specialized_type",
                impactu_type=record.get("type_impactu"),
                ip_namespace="patent" if specialized == "patents" else "",
            )
        return _route(
            "works", "work", "cvlac_production_channel",
            impactu_type=record.get("type_impactu"),
        )
    if channel == "projects" and exact_key(record.get("project_type")) in CVLAC_PROJECT_TYPES:
        return _route("projects", "project", "cvlac_exact_project_type", impactu_type="Proyecto")
    if channel == "events" and exact_key(record.get("event_type")) in EVENT_TYPES:
        return _route("events", "event", "cvlac_exact_event_type", impactu_type="Evento")
    if channel == "patents" and exact_key(record.get("product_type")) in CVLAC_PATENT_TYPES:
        return _route(
            "patents", "patent", "cvlac_exact_patent_type",
            impactu_type="Patente", ip_namespace="patent",
        )
    return None


def route_gruplac(record: dict[str, Any]) -> dict[str, str] | None:
    """Route a GrupLAC occurrence; unknown values in special sections are held."""
    section = exact_key(record.get("source_section"))
    product_type = exact_key(record.get("product_type"))
    if section in GRUPLAC_DIRECTED_SECTIONS:
        thesis = GRUPLAC_THESIS_TYPES.get(product_type)
        if thesis:
            return _route("works", thesis["family"], "gruplac_exact_thesis_type", impactu_type=thesis["impactu_type"])
        return _route(
            "works", "work", "gruplac_directed_work_channel",
            impactu_type=record.get("type_impactu") or "Docencia",
        )
    if section == GRUPLAC_PROJECT_SECTION:
        if exact_key(record.get("project_type")) in GRUPLAC_PROJECT_TYPES:
            return _route("projects", "project", "gruplac_exact_project_type", impactu_type="Proyecto")
        return None
    if section == GRUPLAC_EVENT_SECTION:
        if product_type in EVENT_TYPES:
            return _route("events", "event", "gruplac_exact_event_type", impactu_type="Evento")
        return None
    patent_namespace = GRUPLAC_PATENT_IP_TYPES.get((section, product_type))
    if patent_namespace:
        return _route(
            "patents", "intellectual_property", "gruplac_exact_patent_ip_type",
            impactu_type="Patente", ip_namespace=patent_namespace,
        )
    work_namespace = GRUPLAC_WORK_IP_TYPES.get((section, product_type))
    if work_namespace:
        return _route(
            "works", "work", "gruplac_exact_work_ip_type",
            impactu_type=record.get("type_impactu"), ip_namespace=work_namespace,
        )
    if section in {value[0] for value in GRUPLAC_PATENT_IP_TYPES | GRUPLAC_WORK_IP_TYPES}:
        return None
    return _route(
        "works", "work", "gruplac_production_channel",
        impactu_type=record.get("type_impactu"),
    )


def route_minciencias(product_class: Any, typology: Any) -> dict[str, str] | None:
    """Route one official measured-product type through the Excel catalog."""
    mapping = IMPACTU_CATALOG.classify_minciencias(product_class, typology)
    if not mapping:
        return None
    return _route(
        mapping["entity"], mapping["entity"].rstrip("s"),
        "impactu_catalog_exact_minciencias_type",
        impactu_type=mapping["type_impactu"],
    )
