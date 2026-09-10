"""Strict normalization of non-bibliographic Scienti entities for Kahi.

The existing profile normalizers are the bronze/silver source.  This module
routes only exact, explicitly catalogued CVLAC and GrupLAC types into four
Kahi-compatible collections: degree works/theses, projects, patents and
events.  It deliberately contains no fuzzy matching.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from time import time
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from pymongo import ASCENDING, ReplaceOne

from yuku.gruplac_related_works import enrich_exact_section
from yuku.minciencias_measurements import empty_kahi_work
from yuku.scienti_routing import (
    ENTITY_ROUTER_VERSION,
    GRUPLAC_DIRECTED_SECTIONS,
    GRUPLAC_PATENT_IP_TYPES,
    GRUPLAC_WORK_IP_TYPES,
    exact_key,
    route_cvlac,
    route_gruplac,
)


ENTITY_SCHEMA_VERSION = "kahi-scienti-entities-v1"
ENTITY_NORMALIZER_VERSION = "scienti-entity-normalizer-v4"
ENTITY_RUNS = "scienti_entity_normalization_runs"
ENTITY_AUDITS = "scienti_entity_normalization_audits"
BOGOTA = ZoneInfo("America/Bogota")
RUN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
COLLECTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,119}")

ENTITY_KEYS = ("works", "projects", "patents", "events")
THESIS_FAMILIES = {
    "undergraduate_thesis", "graduate_thesis", "graduate_degree_work",
}

MONTH_NUMBERS = {
    "enero": 1, "january": 1,
    "febrero": 2, "february": 2,
    "marzo": 3, "march": 3,
    "abril": 4, "april": 4,
    "mayo": 5, "may": 5,
    "junio": 6, "june": 6,
    "julio": 7, "july": 7,
    "agosto": 8, "august": 8,
    "septiembre": 9, "setiembre": 9, "september": 9,
    "octubre": 10, "october": 10,
    "noviembre": 11, "november": 11,
    "diciembre": 12, "december": 12,
}


EVENT_SCOPES = {
    exact_key("Nacional"): "Nacional",
    exact_key("Internacional"): "Internacional",
}
GENERIC_REGISTRATION_TOKENS = {
    "0", "00", "000", "n a", "na", "no aplica", "no disponible",
    "no informado", "pendiente", "pending", "por asignar", "sin asignar",
    "sin numero", "sin número", "en tramite", "en trámite", "solicitud",
}


def registration_key(value: Any) -> str:
    """Return a usable exact registration key or empty for placeholders."""
    key = re.sub(r"\s+", "", exact_key(value))
    token = re.sub(r"[^\w]+", " ", exact_key(value), flags=re.UNICODE).strip()
    if not key or token in GENERIC_REGISTRATION_TOKENS:
        return ""
    if not re.search(r"\d", key):
        return ""
    if len(re.sub(r"\W", "", key, flags=re.UNICODE)) < 3:
        return ""
    return key


def prepare_gruplac_record(record: dict[str, Any]) -> dict[str, Any]:
    """Backfill structured fields from v2 normalized rows using exact labels."""
    prepared = deepcopy(record)
    enrich_exact_section(prepared, str(prepared.get("source_section") or ""))
    return prepared


def _title(record: dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", str(record.get("title") or "")).strip(" ,.;:-")


def _year(value: Any) -> int | None:
    if isinstance(value, int) and 1900 <= value <= 2100:
        return value
    match = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", str(value or ""))
    return int(match.group(1)) if match else None


def _timestamp(value: Any) -> int | None:
    if isinstance(value, datetime):
        parsed = value.astimezone(BOGOTA) if value.tzinfo else value.replace(tzinfo=BOGOTA)
        return int(parsed.timestamp())
    text = str(value or "").strip()
    if not text:
        return None
    parsed: datetime | None = None
    for pattern in ("%Y-%m-%d", "%Y/%m", "%Y/%m/%d", "%Y-%m"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=BOGOTA)
            break
        except ValueError:
            pass
    if parsed is None:
        normalized = exact_key(text).strip(" ,.;")
        month_names = "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True))
        match = re.fullmatch(
            rf"({month_names})\s+(19\d{{2}}|20\d{{2}})", normalized
        )
        if match:
            parsed = datetime(
                int(match.group(2)), MONTH_NUMBERS[match.group(1)], 1, tzinfo=BOGOTA
            )
        else:
            match = re.fullmatch(
                rf"(19\d{{2}}|20\d{{2}})\s+({month_names})", normalized
            )
            if match:
                parsed = datetime(
                    int(match.group(1)), MONTH_NUMBERS[match.group(2)], 1, tzinfo=BOGOTA
                )
            elif re.fullmatch(r"19\d{2}|20\d{2}", normalized):
                parsed = datetime(int(normalized), 1, 1, tzinfo=BOGOTA)
    return int(parsed.timestamp()) if parsed else None


def _valid_event_date(value: Any) -> str | None:
    """Return a strict calendar date; malformed source values become null."""
    text = str(value or "").strip()
    if not re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", text):
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _event_scope(value: Any) -> str | None:
    """Recover only explicit national/international GrupLAC scope labels."""
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ,.;:-")
    canonical = EVENT_SCOPES.get(exact_key(text))
    if canonical:
        return canonical
    match = re.match(r"^(Nacional|Internacional)\b(?P<tail>.+)$", text, re.IGNORECASE)
    if not match:
        return None
    # Some GrupLAC rows repeat structural event text inside the value captured
    # after "Ámbito".  Strip it only when an explicit structural marker proves
    # that the tail is parser contamination, never from a merely similar word.
    tail = exact_key(match.group("tail"))
    if not re.search(
        r"\b(?:ámbito|tipo de evento|realizado|desde)\b|(?:19|20)\d{2}-\d{2}-\d{2}",
        tail,
    ):
        return None
    return EVENT_SCOPES[exact_key(match.group(1))]


def _sanitize_event_record(record: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Null unsafe event values while retaining reason codes for audit."""
    output = deepcopy(record)
    findings: list[dict[str, str]] = []
    raw_start = str(output.get("start_date") or "").strip()
    raw_end = str(output.get("end_date") or "").strip()
    start = _valid_event_date(raw_start)
    end = _valid_event_date(raw_end)
    if raw_start and not start:
        findings.append({"kind": "source_invalid_event_start_date", "field": "start_date"})
    if raw_end and not end:
        findings.append({"kind": "source_invalid_event_end_date", "field": "end_date"})
    if start and end and end < start:
        findings.append({"kind": "source_event_end_before_start", "field": "end_date"})
        end = None
    output["start_date"] = start
    output["end_date"] = end

    raw_scope = str(output.get("scope") or "").strip()
    scope = _event_scope(raw_scope)
    if raw_scope and not scope:
        findings.append({"kind": "source_invalid_event_scope", "field": "scope"})
    elif scope and exact_key(raw_scope.strip(" ,.;:-")) != exact_key(scope):
        findings.append({"kind": "source_event_scope_normalized", "field": "scope"})
    output["scope"] = scope
    return output, findings


def _is_generic(title: str) -> bool:
    tokens = [value for value in re.findall(r"\w+", exact_key(title)) if len(value) > 2]
    return len(tokens) < 4 or len(exact_key(title)) < 20


def _person(name: str, identifier: str = "", role: str = "") -> dict[str, Any] | None:
    name = re.sub(r"\s+", " ", str(name or "")).strip(" ,.;:-")
    identifier = str(identifier or "").strip()
    if not name and not identifier:
        return None
    value: dict[str, Any] = {"id": identifier, "full_name": name, "affiliations": []}
    if role:
        value["type"] = role
    return value


def _member_index(group: dict[str, Any]) -> dict[str, tuple[str, str]]:
    candidates: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for member in group.get("members") or []:
        name = str(member.get("full_name") or "").strip()
        code = str(member.get("cod_rh") or "").strip()
        if name:
            candidates[exact_key(name)].append((code, name))
    return {
        key: values[0]
        for key, values in candidates.items()
        if len({value[0] for value in values}) == 1
    }


def _resolve_person(name: str, members: dict[str, tuple[str, str]], role: str = "") -> dict[str, Any] | None:
    matched = members.get(exact_key(name))
    return _person(matched[1], matched[0], role) if matched else _person(name, "", role)


def _unique_dicts(values: Iterable[dict[str, Any]], key) -> list[dict[str, Any]]:
    output: dict[Any, dict[str, Any]] = {}
    for value in values:
        value_key = key(value)
        if value_key not in output:
            output[value_key] = deepcopy(value)
    return list(output.values())


def _merge_authors(values: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for value in values:
        grouped[(exact_key(value.get("full_name")), str(value.get("type") or ""))].append(value)
    authors: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for (name_key, role), candidates in sorted(grouped.items()):
        identifiers = sorted({str(value.get("id") or "") for value in candidates if value.get("id")})
        if len(identifiers) > 1:
            conflicts.append({"name": name_key, "role": role, "ids": identifiers})
            for value in candidates:
                authors.append(deepcopy(value))
            continue
        selected = max(candidates, key=lambda value: (bool(value.get("id")), len(str(value.get("full_name") or ""))))
        selected = deepcopy(selected)
        if identifiers:
            selected["id"] = identifiers[0]
        authors.append(selected)
    authors = _unique_dicts(
        authors,
        lambda value: (
            str(value.get("id") or ""),
            exact_key(value.get("full_name")),
            str(value.get("type") or ""),
        ),
    )
    authors.sort(key=lambda value: (value.get("type") == "advisor", exact_key(value.get("full_name"))))
    return authors, conflicts


def _resolve_thesis_roles(
    authors: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give a verified student role precedence over the exact same advisor."""
    values = [deepcopy(value) for value in authors]
    student_keys = {
        exact_key(value.get("full_name"))
        for value in values
        if value.get("type") != "advisor" and value.get("full_name")
    }
    kept: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    for value in values:
        name_key = exact_key(value.get("full_name"))
        if value.get("type") == "advisor" and name_key and name_key in student_keys:
            resolutions.append(
                {
                    "rule": "student_precedence_exact_name",
                    "name": str(value.get("full_name") or ""),
                    "discarded_advisor_id": str(value.get("id") or ""),
                }
            )
            continue
        kept.append(value)
    return kept, _unique_dicts(resolutions, _dict_key)


def empty_kahi_project() -> dict[str, Any]:
    return {
        "titles": [], "updated": [], "abstract": "", "types": [],
        "external_ids": [], "external_urls": [], "date_init": None,
        "date_end": None, "year_init": None, "year_end": None,
        "author_count": 0, "authors": [], "ranking": [], "groups": [],
    }


def empty_kahi_event() -> dict[str, Any]:
    return {
        "titles": [], "updated": [], "abstract": "", "types": [],
        "external_ids": [], "external_urls": [], "date_held": None,
        "year_held": None, "author_count": 0, "authors": [],
        "ranking": [], "groups": [],
    }


def empty_kahi_patent() -> dict[str, Any]:
    return {
        "titles": [], "updated": [], "types": [], "external_ids": [],
        "external_urls": [], "author_count": 0, "authors": [],
        "ranking": [], "groups": [],
    }


def _identity(
    route: dict[str, str], record: dict[str, Any], source: dict[str, Any], authors: list[dict[str, Any]]
) -> tuple[str, str]:
    entity = route["entity"]
    title = _title(record)
    title_key = exact_key(title)
    year = _year(record.get("year") or record.get("start_date"))
    source_scope = f"{source['kind']}|{source['id']}|{source['index']}"
    if entity == "works" and route.get("family") in THESIS_FAMILIES:
        students = sorted(
            exact_key(value.get("full_name"))
            for value in authors
            if value.get("type") != "advisor" and value.get("full_name")
        )
        if title_key and year and students and not _is_generic(title):
            key = f"thesis|{route['family']}|{title_key}|{year}|{'|'.join(students)}"
            return key, "exact_title_year_family_students"
    elif entity == "projects":
        project_type = exact_key(record.get("project_type"))
        if title_key and year and project_type and not _is_generic(title):
            return f"project|{project_type}|{title_key}|{year}", "exact_type_title_year"
    elif entity == "events":
        event_type = exact_key(record.get("event_type") or record.get("product_type"))
        start = exact_key(record.get("start_date"))
        if title_key and event_type and start and not _is_generic(title):
            return f"event|{event_type}|{title_key}|{start}", "exact_type_title_start_date"
    elif entity == "patents":
        namespace = route.get("ip_namespace") or "unknown_ip"
        registration = registration_key(record.get("registration_number"))
        country = exact_key(record.get("country"))
        if registration and title_key and not _is_generic(title):
            return (
                f"ip|{namespace}|registration|{registration}|country|"
                f"{country or 'unknown'}|title|{title_key}",
                "exact_namespace_registration_country_title",
            )
        product_type = exact_key(record.get("product_type"))
        if title_key and year and product_type and not _is_generic(title):
            return (
                f"ip|{namespace}|title|{title_key}|{year}|{country}",
                "exact_namespace_title_year_country",
            )
        return (
            f"ip|{namespace}|source|{source_scope}|{title_key}",
            "source_scoped_insufficient_ip_anchors",
        )
    return f"source|{entity}|{source_scope}|{title_key}", "source_scoped_insufficient_anchors"


def _occurrence_metadata(entity: str, record: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "works": (
            "status", "affiliation", "institution", "orientation_type",
            "academic_program", "pages", "assessment", "start_date", "end_date",
        ),
        "projects": (
            "project_type", "start_date", "projected_end_date", "end_date",
            "duration", "summary",
        ),
        "patents": (
            "registration_number", "presentation_date", "request_route",
            "industrial_publication_gazette", "applicant", "holder",
            "availability", "funding_institution", "cycle_type", "website",
            "administrative_act", "country", "affiliation", "year",
        ),
        "events": (
            "event_type", "scope", "start_date", "end_date", "location", "city",
            "venue", "participation_types", "associated_products",
            "associated_institutions", "participants",
        ),
    }[entity]
    output = {
        field: deepcopy(record.get(field))
        for field in fields
        if record.get(field) not in (None, "", [])
    }
    if entity == "events":
        # A malformed or contradictory source date is represented explicitly
        # as null in the normalized occurrence, never copied as a valid value.
        output["start_date"] = deepcopy(record.get("start_date"))
        output["end_date"] = deepcopy(record.get("end_date"))
        if "scope" in record:
            output["scope"] = deepcopy(record.get("scope"))
    return output


def normalize_occurrence(
    route: dict[str, str],
    record: dict[str, Any],
    *,
    source_kind: str,
    source_id: str,
    source_collection: str,
    source_url: str,
    record_index: int,
    group: dict[str, Any] | None = None,
    members: dict[str, tuple[str, str]] | None = None,
    normalized_at: int | None = None,
) -> dict[str, Any] | None:
    """Convert one exactly-routed occurrence into a Kahi-compatible document."""
    record = deepcopy(record)
    normalization_findings: list[dict[str, str]] = []
    if route["entity"] == "events":
        record, normalization_findings = _sanitize_event_record(record)
    title = _title(record)
    if not title:
        return None
    normalized_at = int(time()) if normalized_at is None else int(normalized_at)
    entity = route["entity"]
    profile_id = str(record.get("profile_id") or source_id if source_kind == "cvlac" else "")
    profile_name = str(record.get("profile_name") or "").strip()
    members = members if members is not None else _member_index(group or {})
    authors: list[dict[str, Any]] = []

    is_thesis = entity == "works" and route.get("family") in THESIS_FAMILIES
    if is_thesis:
        students = record.get("students") or record.get("oriented_people") or []
        for name in students:
            person = _resolve_person(str(name), members) if source_kind == "gruplac" else _person(str(name))
            if person:
                authors.append(person)
        if source_kind == "cvlac":
            if not profile_name:
                candidates = record.get("authors") or []
                profile_name = str(candidates[0]) if candidates else ""
            advisor = _person(profile_name, profile_id, "advisor")
            if advisor:
                authors.append(advisor)
        else:
            for name in record.get("advisors") or []:
                advisor = _resolve_person(str(name), members, "advisor")
                if advisor:
                    authors.append(advisor)
    elif entity == "works" and source_kind == "cvlac":
        for value in record.get("authors") or []:
            name = str(
                value.get("full_name") or value.get("name") or ""
            ) if isinstance(value, dict) else str(value)
            person = _person(
                name,
                profile_id if exact_key(name) == exact_key(profile_name) else "",
            )
            if person:
                authors.append(person)
        owner = _person(profile_name, profile_id)
        if owner:
            authors.append(owner)
    elif entity == "works":
        for value in record.get("authors") or []:
            name = str(
                value.get("full_name") or value.get("name") or ""
            ) if isinstance(value, dict) else str(value)
            person = _resolve_person(name, members)
            if person:
                authors.append(person)
    elif source_kind == "cvlac":
        owner = _person(profile_name, profile_id)
        if owner:
            authors.append(owner)
        if entity == "events":
            for participant in record.get("participants") or []:
                name = str(participant.get("name") or "") if isinstance(participant, dict) else str(participant)
                person = _person(name, profile_id if exact_key(name) == exact_key(profile_name) else "")
                if person:
                    authors.append(person)
    else:
        for name in record.get("authors") or []:
            person = _resolve_person(str(name), members)
            if person:
                authors.append(person)

    authors, author_conflicts = _merge_authors(authors)
    role_resolutions: list[dict[str, Any]] = []
    if is_thesis:
        authors, role_resolutions = _resolve_thesis_roles(authors)
        normalization_findings.extend(
            {
                "kind": "advisor_removed_exact_student_match",
                "field": "authors",
            }
            for _ in role_resolutions
        )
    source = {
        "kind": source_kind,
        "id": source_id,
        "index": int(record_index),
    }
    identity_key, identity_rule = _identity(route, record, source, authors)
    entity_id = sha256(
        f"{ENTITY_SCHEMA_VERSION}|{entity}|{identity_key}".encode("utf-8")
    ).hexdigest()
    occurrence_id = sha256(
        f"{source_kind}|{source_collection}|{source_id}|{record_index}|{exact_key(title)}".encode("utf-8")
    ).hexdigest()
    occurrence = {
        "id": occurrence_id,
        "source_kind": source_kind,
        "source_collection": source_collection,
        "source_id": source_id,
        "record_index": int(record_index),
        "source_section": str(record.get("source_section") or ""),
        "product_type": str(record.get("product_type") or record.get("project_type") or record.get("event_type") or ""),
        "route_rule": route["rule"],
        "metadata": _occurrence_metadata(entity, record),
    }
    if route.get("ip_namespace"):
        occurrence["identity_namespace"] = route["ip_namespace"]
    if source_url:
        occurrence["url"] = source_url
    if "validated" in record:
        occurrence["validated"] = bool(record.get("validated"))
    if normalization_findings:
        occurrence["normalization_findings"] = normalization_findings

    group_value = []
    if group and group.get("group_code"):
        group_value = [{"id": str(group["group_code"]), "name": str(group.get("group_name") or "")}]

    if entity == "works":
        document = empty_kahi_work()
        document["year_published"] = _year(record.get("year"))
        document["date_published"] = _timestamp(record.get("year"))
    elif entity == "projects":
        document = empty_kahi_project()
        document["abstract"] = str(record.get("summary") or "")
        document["date_init"] = _timestamp(record.get("start_date"))
        document["date_end"] = _timestamp(record.get("end_date"))
        document["year_init"] = _year(record.get("start_date") or record.get("year"))
        document["year_end"] = _year(record.get("end_date"))
    elif entity == "events":
        document = empty_kahi_event()
        document["date_held"] = _timestamp(record.get("start_date"))
        document["year_held"] = _year(record.get("start_date"))
    else:
        document = empty_kahi_patent()

    native_type = str(record.get("product_type") or record.get("project_type") or record.get("event_type") or "")
    document.update(
        {
            "_id": entity_id,
            "titles": [{"title": title, "lang": "es", "source": "scienti"}],
            "updated": [{"source": "scienti", "time": normalized_at}],
            "types": [
                {
                    "provenance": "scienti",
                    "source": "scienti",
                    "type": native_type,
                    "level": 1,
                    "parent": str(record.get("source_section") or "") or None,
                }
            ],
            "external_ids": [
                {
                    "provenance": "scienti",
                    "source": "scienti",
                    "id": {
                        "source_kind": source_kind,
                        "source_id": source_id,
                        "occurrence_id": occurrence_id,
                    },
                }
            ],
            "external_urls": [
                {"source": "scienti", "url": source_url}
            ] if source_url else [],
            "authors": authors,
            "author_count": len(authors),
            "groups": group_value,
            "source_metadata": {
                "schema_version": ENTITY_SCHEMA_VERSION,
                "router_version": ENTITY_ROUTER_VERSION,
                "normalizer_version": ENTITY_NORMALIZER_VERSION,
                "type_catalog_version": route.get("catalog_version", ""),
                "type_catalog_sha256": route.get("catalog_sha256", ""),
                "target_entity": entity,
                "family": route["family"],
                "identity_key": identity_key,
                "identity_rule": identity_rule,
                "occurrences": [occurrence],
                "author_identity_conflicts": author_conflicts,
                "role_resolutions": role_resolutions,
            },
        }
    )
    if route.get("impactu_type"):
        document["types"].append(
            {
                "provenance": "scienti",
                "source": "impactu",
                "type": route["impactu_type"],
            }
        )
    return document


def _unmaterialized_detail(
    *,
    source_kind: str,
    source_id: str,
    channel: str,
    record_index: int,
    record: dict[str, Any],
    route: dict[str, str],
    reason: str,
) -> dict[str, Any]:
    """Keep bounded evidence for routed rows that cannot form an entity."""
    return {
        "outcome": "excluded_incomplete",
        "reason": reason,
        "source_kind": source_kind,
        "source_id": source_id,
        "channel": channel,
        "record_index": int(record_index),
        "target_entity": route["entity"],
        "route_rule": route["rule"],
        "source_section": str(record.get("source_section") or ""),
        "product_type": str(
            record.get("product_type")
            or record.get("project_type")
            or record.get("event_type")
            or ""
        ),
        "title": str(record.get("title") or ""),
        "raw_text": str(record.get("raw_text") or "")[:2000],
    }


def _dict_key(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _prefer_scalar(left: Any, right: Any) -> Any:
    if left in (None, "", [], {}):
        return deepcopy(right)
    if right in (None, "", [], {}):
        return deepcopy(left)
    if isinstance(left, str) and isinstance(right, str):
        return max((left, right), key=lambda value: (len(value), value))
    return deepcopy(left)


def merge_entity_documents(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    if left["_id"] != right["_id"]:
        raise ValueError("only documents with the same identity can be merged")
    output = deepcopy(left)
    for field in ("titles", "updated", "types", "external_ids", "external_urls", "ranking", "groups"):
        output[field] = _unique_dicts(
            list(output.get(field) or []) + list(right.get(field) or []), _dict_key
        )
    authors, author_conflicts = _merge_authors(
        list(output.get("authors") or []) + list(right.get("authors") or [])
    )
    role_resolutions: list[dict[str, Any]] = []
    left_family = str((output.get("source_metadata") or {}).get("family") or "")
    right_family = str((right.get("source_metadata") or {}).get("family") or "")
    if left_family in {"undergraduate_thesis", "graduate_thesis", "graduate_degree_work"}:
        if right_family != left_family:
            raise ValueError("only thesis documents from the same family can be merged")
        authors, role_resolutions = _resolve_thesis_roles(authors)
    output["authors"] = authors
    output["author_count"] = len(authors)
    for field in (
        "abstract", "date_init", "date_end", "year_init", "year_end",
        "date_held", "year_held", "date_published", "year_published",
    ):
        if field in output or field in right:
            output[field] = _prefer_scalar(output.get(field), right.get(field))
    left_meta = output.setdefault("source_metadata", {})
    right_meta = right.get("source_metadata") or {}
    left_meta["occurrences"] = _unique_dicts(
        list(left_meta.get("occurrences") or []) + list(right_meta.get("occurrences") or []),
        lambda value: value.get("id"),
    )
    left_meta["author_identity_conflicts"] = _unique_dicts(
        list(left_meta.get("author_identity_conflicts") or [])
        + list(right_meta.get("author_identity_conflicts") or [])
        + author_conflicts,
        _dict_key,
    )
    left_meta["role_resolutions"] = _unique_dicts(
        list(left_meta.get("role_resolutions") or [])
        + list(right_meta.get("role_resolutions") or [])
        + role_resolutions,
        _dict_key,
    )
    return output


def iter_cvlac_entities(
    profile: dict[str, Any], source_collection: str, normalized_at: int
) -> Iterator[tuple[str, dict[str, Any] | None, dict[str, Any]]]:
    source_id = str(profile.get("_id") or "")
    source_url = str(profile.get("url_persona") or (profile.get("source") or {}).get("url") or "")
    profile_name = str(profile.get("profile_name") or "")
    for channel in ("production", "projects", "patents", "events"):
        for index, original in enumerate(profile.get(channel) or []):
            record = deepcopy(original)
            record.setdefault("profile_name", profile_name)
            route = route_cvlac(channel, record)
            if not route:
                detail = {"source_kind": "cvlac", "source_id": source_id, "channel": channel, "product_type": record.get("product_type") or record.get("project_type") or record.get("event_type")}
                if channel != "production" or exact_key(record.get("source_section")) == exact_key("Trabajos dirigidos/tutorias"):
                    yield "unmapped", None, detail
                continue
            document = normalize_occurrence(
                route, record, source_kind="cvlac", source_id=source_id,
                source_collection=source_collection, source_url=source_url,
                record_index=index, normalized_at=normalized_at,
            )
            if document:
                yield route["entity"], document, {"route": route["rule"]}
            else:
                yield "excluded_incomplete", None, _unmaterialized_detail(
                    source_kind="cvlac",
                    source_id=source_id,
                    channel=channel,
                    record_index=index,
                    record=record,
                    route=route,
                    reason="missing_meaningful_title",
                )


def iter_gruplac_entities(
    group: dict[str, Any], source_collection: str, normalized_at: int
) -> Iterator[tuple[str, dict[str, Any] | None, dict[str, Any]]]:
    source_id = str(group.get("group_code") or group.get("_id") or "")
    source_url = str(group.get("url_gruplac") or (group.get("source") or {}).get("url") or "")
    scoped_sections = {
        *GRUPLAC_DIRECTED_SECTIONS, exact_key("Proyectos"),
        exact_key("Eventos Científicos"),
        *(section for section, _ in GRUPLAC_PATENT_IP_TYPES),
        *(section for section, _ in GRUPLAC_WORK_IP_TYPES),
    }
    members = _member_index(group)
    for index, original in enumerate(group.get("production") or []):
        record = prepare_gruplac_record(original)
        route = route_gruplac(record)
        if not route:
            detail = {"source_kind": "gruplac", "source_id": source_id, "section": record.get("source_section"), "product_type": record.get("product_type"), "project_type": record.get("project_type")}
            if exact_key(record.get("source_section")) in scoped_sections:
                yield "unmapped", None, detail
            continue
        document = normalize_occurrence(
            route, record, source_kind="gruplac", source_id=source_id,
            source_collection=source_collection, source_url=source_url,
            record_index=index, group=group, members=members,
            normalized_at=normalized_at,
        )
        if document:
            yield route["entity"], document, {"route": route["rule"]}
        else:
            yield "excluded_incomplete", None, _unmaterialized_detail(
                source_kind="gruplac",
                source_id=source_id,
                channel=str(record.get("source_section") or "production"),
                record_index=index,
                record=record,
                route=route,
                reason="missing_meaningful_title",
            )


class ScientiEntityNormalizationRun:
    """Checkpointed exact normalizer that writes only four final collections."""

    def __init__(
        self,
        db,
        *,
        run_name: str,
        cvlac_collection: str,
        gruplac_collection: str,
        destinations: dict[str, str],
        batch_size: int = 500,
        progress_every: int = 1000,
        replace: bool = False,
        limit_sources: int = 0,
    ):
        if not RUN_NAME_RE.fullmatch(run_name or ""):
            raise ValueError("run_name must contain only letters, numbers and underscores")
        if set(destinations) != set(ENTITY_KEYS):
            raise ValueError(f"destinations must define {ENTITY_KEYS}")
        for label, value in {
            "cvlac_collection": cvlac_collection,
            "gruplac_collection": gruplac_collection,
            **destinations,
        }.items():
            if not COLLECTION_RE.fullmatch(value or "") or value.startswith("system."):
                raise ValueError(f"invalid collection name for {label}")
        if len(set(destinations.values())) != len(destinations):
            raise ValueError("entity destination collections must be distinct")
        self.db = db
        self.run_name = run_name
        self.cvlac_collection = cvlac_collection
        self.gruplac_collection = gruplac_collection
        self.destinations = deepcopy(destinations)
        self.batch_size = max(1, int(batch_size))
        self.progress_every = max(1, int(progress_every))
        self.replace = bool(replace)
        self.limit_sources = max(0, int(limit_sources))
        self.runs = db[ENTITY_RUNS]
        self.normalized_at = int(time())
        self.config = {
            "schema_version": ENTITY_SCHEMA_VERSION,
            "router_version": ENTITY_ROUTER_VERSION,
            "normalizer_version": ENTITY_NORMALIZER_VERSION,
            "cvlac_collection": cvlac_collection,
            "gruplac_collection": gruplac_collection,
            "destinations": deepcopy(destinations),
            "batch_size": self.batch_size,
            "limit_sources": self.limit_sources,
        }
        self.config_hash = sha256(
            json.dumps(self.config, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _prepare(self) -> dict[str, Any]:
        existing_names = set(self.db.list_collection_names())
        for source in (self.cvlac_collection, self.gruplac_collection):
            if source not in existing_names:
                raise ValueError(f"source collection {source!r} was not found")
        previous = self.runs.find_one({"_id": self.run_name})
        if self.replace:
            for name in self.destinations.values():
                self.db[name].drop()
            self.runs.delete_one({"_id": self.run_name})
            previous = None
        if previous and previous.get("config_hash") != self.config_hash:
            raise ValueError("the run name already exists with another configuration")
        if not previous:
            nonempty = {
                name: self.db[name].estimated_document_count()
                for name in self.destinations.values()
                if name in existing_names and self.db[name].estimated_document_count()
            }
            if nonempty:
                raise ValueError(f"destination collections are not empty: {nonempty}")
            self.runs.insert_one(
                {
                    "_id": self.run_name,
                    "status": "pending",
                    "config": self.config,
                    "config_hash": self.config_hash,
                    "normalized_at": self.normalized_at,
                    "created_at": datetime.now(timezone.utc),
                    "checkpoints": {},
                    "counters": {},
                    "routing_examples": [],
                }
            )
            previous = self.runs.find_one({"_id": self.run_name}) or {}
        self.normalized_at = int(previous.get("normalized_at") or self.normalized_at)
        return previous

    def _flush(self, pending: dict[str, list[dict[str, Any]]]) -> None:
        for entity, documents in pending.items():
            if not documents:
                continue
            grouped: dict[str, dict[str, Any]] = {}
            for document in documents:
                current = grouped.get(document["_id"])
                grouped[document["_id"]] = document if current is None else merge_entity_documents(current, document)
            collection = self.db[self.destinations[entity]]
            existing = {
                value["_id"]: value
                for value in collection.find({"_id": {"$in": list(grouped)}})
            }
            operations = []
            for entity_id, document in grouped.items():
                if entity_id in existing:
                    document = merge_entity_documents(existing[entity_id], document)
                operations.append(ReplaceOne({"_id": entity_id}, document, upsert=True))
            if operations:
                try:
                    collection.bulk_write(operations, ordered=False)
                except TypeError:
                    # mongomock versions paired with PyMongo 4 do not support
                    # ReplaceOne's ``sort`` argument; production MongoDB uses
                    # the bulk path above.
                    for document in grouped.values():
                        if document["_id"] in existing:
                            document = merge_entity_documents(
                                existing[document["_id"]], document
                            )
                        collection.replace_one(
                            {"_id": document["_id"]}, document, upsert=True
                        )
            documents.clear()

    def _process_source(self, kind: str, collection_name: str, iterator) -> Counter:
        run = self.runs.find_one({"_id": self.run_name}) or {}
        checkpoint = ((run.get("checkpoints") or {}).get(kind) or {})
        if checkpoint.get("status") == "complete":
            return Counter()
        last_id = str(checkpoint.get("last_id") or "")
        query = {"_id": {"$gt": last_id}} if last_id else {}
        cursor = self.db[collection_name].find(query).sort("_id", ASCENDING)
        if self.limit_sources:
            cursor = cursor.limit(self.limit_sources)
        pending = {entity: [] for entity in ENTITY_KEYS}
        counters: Counter = Counter()
        examples: list[dict[str, Any]] = []
        processed_sources = 0
        current_id = last_id
        for source in cursor:
            current_id = str(source["_id"])
            processed_sources += 1
            counters["sources"] += 1
            for outcome, document, detail in iterator(
                source, collection_name, self.normalized_at
            ):
                counters[outcome] += 1
                if document:
                    pending[outcome].append(document)
                elif outcome in {"unmapped", "excluded_incomplete", "invalid"} and len(examples) < 50:
                    examples.append({"outcome": outcome, **detail})
            pending_count = sum(len(values) for values in pending.values())
            should_report = processed_sources % self.progress_every == 0
            if pending_count >= self.batch_size or should_report:
                self._flush(pending)
                self.runs.update_one(
                    {"_id": self.run_name},
                    {
                        "$set": {
                            "status": "running",
                            f"checkpoints.{kind}.last_id": current_id,
                            f"checkpoints.{kind}.processed_sources": processed_sources,
                            "last_progress_at": datetime.now(timezone.utc),
                        },
                        "$inc": {f"counters.{kind}.{key}": value for key, value in counters.items()},
                        "$push": {"routing_examples": {"$each": examples, "$slice": 100}},
                    },
                )
                counters.clear()
                examples.clear()
                if should_report:
                    print(
                        f"{datetime.now(timezone.utc).isoformat()} INFO: entity normalization "
                        f"source={kind} profiles={processed_sources} last={current_id}",
                        flush=True,
                    )
        self._flush(pending)
        update: dict[str, Any] = {
            "$set": {
                "status": "running",
                f"checkpoints.{kind}.last_id": current_id,
                f"checkpoints.{kind}.processed_sources": processed_sources,
                f"checkpoints.{kind}.status": "complete",
                "last_progress_at": datetime.now(timezone.utc),
            }
        }
        if counters:
            update["$inc"] = {f"counters.{kind}.{key}": value for key, value in counters.items()}
        if examples:
            update["$push"] = {"routing_examples": {"$each": examples, "$slice": 100}}
        self.runs.update_one({"_id": self.run_name}, update)
        return counters

    def _indexes(self) -> None:
        for entity, name in self.destinations.items():
            collection = self.db[name]
            collection.create_index("titles.title")
            collection.create_index("authors.id")
            collection.create_index("groups.id")
            collection.create_index("source_metadata.identity_key", unique=True)
            collection.create_index("source_metadata.occurrences.id")

    def audit(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        occurrences: dict[str, int] = {}
        anomaly_counts: Counter = Counter()
        examples: list[dict[str, Any]] = []
        run = self.runs.find_one({"_id": self.run_name}) or {}
        for source_kind in ("cvlac", "gruplac"):
            if (((run.get("checkpoints") or {}).get(source_kind) or {}).get("status") != "complete"):
                anomaly_counts[f"run.incomplete_{source_kind}_checkpoint"] += 1
        invalid = sum(
            int(((run.get("counters") or {}).get(source_kind) or {}).get("invalid", 0))
            for source_kind in ("cvlac", "gruplac")
        )
        excluded_incomplete = {
            source_kind: int(
                ((run.get("counters") or {}).get(source_kind) or {}).get(
                    "excluded_incomplete", 0
                )
            )
            for source_kind in ("cvlac", "gruplac")
        }
        if invalid:
            anomaly_counts["run.invalid_routed_occurrences"] += invalid
        for entity, name in self.destinations.items():
            collection = self.db[name]
            counts[entity] = collection.count_documents({})
            result = list(
                collection.aggregate(
                    [
                        {"$project": {"n": {"$size": {"$ifNull": ["$source_metadata.occurrences", []]}}}},
                        {"$group": {"_id": None, "n": {"$sum": "$n"}}},
                    ]
                )
            )
            occurrences[entity] = int(result[0]["n"]) if result else 0
            checks = {
                "missing_title": {"titles.0.title": {"$exists": False}},
                "missing_identity": {"source_metadata.identity_key": {"$in": [None, ""]}},
                "missing_occurrence": {"source_metadata.occurrences.0": {"$exists": False}},
                "wrong_target_entity": {"source_metadata.target_entity": {"$ne": entity}},
            }
            for anomaly, query in checks.items():
                count = collection.count_documents(query)
                if count:
                    anomaly_counts[f"{entity}.{anomaly}"] += count
                    if len(examples) < 50:
                        examples.extend(
                            {"entity": entity, "anomaly": anomaly, "_id": str(value["_id"])}
                            for value in collection.find(query, {"_id": 1}).limit(3)
                        )
            if entity == "patents":
                for document in collection.find(
                    {},
                    {
                        "source_metadata.identity_key": 1,
                        "source_metadata.occurrences.identity_namespace": 1,
                    },
                ):
                    identity_key = str(
                        ((document.get("source_metadata") or {}).get("identity_key"))
                        or ""
                    )
                    namespaces = {
                        str(value.get("identity_namespace") or "")
                        for value in (
                            (document.get("source_metadata") or {}).get("occurrences")
                            or []
                        )
                    }
                    namespaces.discard("")
                    anomaly = ""
                    if not identity_key.startswith("ip|"):
                        anomaly = "legacy_or_invalid_identity_namespace"
                    elif len(namespaces) != 1:
                        anomaly = "mixed_identity_namespaces"
                    elif "|registration|" in identity_key:
                        encoded_registration = identity_key.split(
                            "|registration|", 1
                        )[1].split("|", 1)[0]
                        if not registration_key(encoded_registration):
                            anomaly = "generic_registration_identity"
                    if anomaly:
                        anomaly_counts[f"patents.{anomaly}"] += 1
                        if len(examples) < 50:
                            examples.append(
                                {
                                    "entity": "patents",
                                    "anomaly": anomaly,
                                    "_id": str(document["_id"]),
                                    "identity_key": identity_key,
                                    "namespaces": sorted(namespaces),
                                }
                            )
        critical = sum(anomaly_counts.values())
        status = "passed" if critical == 0 else "failed"
        summary = {
            "status": status,
            "schema_version": ENTITY_SCHEMA_VERSION,
            "router_version": ENTITY_ROUTER_VERSION,
            "collection_counts": counts,
            "occurrence_counts": occurrences,
            "excluded_incomplete_occurrences": excluded_incomplete,
            "anomaly_counts": dict(sorted(anomaly_counts.items())),
            "critical_anomalies": critical,
            "examples": examples[:50],
        }
        self.db[ENTITY_AUDITS].replace_one(
            {"_id": self.run_name},
            {"_id": self.run_name, "audited_at": datetime.now(timezone.utc), **summary},
            upsert=True,
        )
        return summary

    def run(self) -> dict[str, Any]:
        previous = self._prepare()
        if previous.get("status") == "complete":
            return deepcopy(previous.get("summary") or {})
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"status": "running", "started_at": datetime.now(timezone.utc)}},
        )
        try:
            self._process_source("cvlac", self.cvlac_collection, iter_cvlac_entities)
            self._process_source("gruplac", self.gruplac_collection, iter_gruplac_entities)
            self._indexes()
            audit = self.audit()
            if audit["status"] != "passed":
                raise RuntimeError(f"entity normalization audit failed: {audit}")
            run = self.runs.find_one({"_id": self.run_name}) or {}
            summary = {
                "run_name": self.run_name,
                "status": "complete",
                "destinations": deepcopy(self.destinations),
                "counters": deepcopy(run.get("counters") or {}),
                "audit": audit,
            }
            self.runs.update_one(
                {"_id": self.run_name},
                {"$set": {"status": "complete", "summary": summary, "finished_at": datetime.now(timezone.utc)}},
            )
            return summary
        except Exception as error:
            self.runs.update_one(
                {"_id": self.run_name},
                {"$set": {"status": "failed", "error": f"{type(error).__name__}: {error}", "failed_at": datetime.now(timezone.utc)}},
            )
            raise
