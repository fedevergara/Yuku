from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import re
from time import time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from pymongo import ASCENDING, ReplaceOne

from yuku.cvlac_related_works import norm_text
from yuku.cvlac_work_graph import (
    GRAPH_VERSION,
    WORK_GRAPH_PUBLICATIONS,
    is_generic_title,
    normalize_title,
    type_family,
)
from yuku.scienti_routing import IMPACTU_CATALOG, route_minciencias


MEASUREMENT_SCHEMA_VERSION = "kahi-works-minciencias-measurement-v1"
MEASUREMENT_LINK_VERSION = "minciencias-measurement-link-v1"
MEASUREMENT_RUNS = "minciencias_measurement_runs"
BOGOTA = ZoneInfo("America/Bogota")
RUN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
PRODUCT_OWNER_RE = re.compile(r"(\d{9,11})-(\d{1,7})$")


def _validate_name(value: str, label: str) -> None:
    if not RUN_NAME_RE.fullmatch(value or ""):
        raise ValueError(f"{label} must contain only letters, numbers and underscores")


def _validate_collection(value: str, label: str) -> None:
    if not value or value.startswith("system."):
        raise ValueError(f"{label} is invalid")


def source_datetime(value: Any) -> datetime | None:
    """Parse a Socrata calendar date using the timezone used by Kahi."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BOGOTA)
    return parsed


def source_timestamp(value: Any) -> int | None:
    parsed = source_datetime(value)
    return int(parsed.timestamp()) if parsed else None


def source_year(value: Any) -> int | None:
    parsed = source_datetime(value)
    return parsed.year if parsed else None


def normalize_convocatoria_id(value: Any) -> str:
    value = str(value or "").strip()
    return value[:-2] if re.fullmatch(r"\d+\.0", value) else value


def empty_kahi_work() -> dict[str, Any]:
    """Return the stable pre-postcalculation shape used by Kahi works."""
    return {
        "updated": [],
        "titles": [],
        "doi": "",
        "abstracts": [],
        "keywords": [],
        "types": [],
        "external_ids": [],
        "external_urls": [],
        "date_published": None,
        "year_published": None,
        "bibliographic_info": {},
        "open_access": {},
        "apc": {"paid": {}},
        "references_count": None,
        "references": [],
        "citations_count": [],
        "citations": [],
        "author_count": 0,
        "authors": [],
        "source": {},
        "ranking": [],
        "subjects": [],
        "citations_by_year": [],
        "groups": [],
        "rights": [],
        "primary_topic": {},
        "topics": [],
    }


def _unique_dicts(values: Iterable[dict[str, Any]], key) -> list[dict[str, Any]]:
    found: dict[Any, dict[str, Any]] = {}
    for value in values:
        value_key = key(value)
        if value_key not in found:
            found[value_key] = value
    return list(found.values())


def _measurement_key(value: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        value.get(field)
        for field in (
            "convocatoria_id",
            "convocatoria",
            "convocatoria_date",
            "presentation_date",
            "group_id",
            "group_name",
            "owner_id",
            "product_class",
            "measurement_type",
            "typology",
            "type_code",
            "category",
            "reported_title",
        )
    )


def measurement_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "convocatoria_id": normalize_convocatoria_id(row.get("id_convocatoria")),
        "convocatoria": str(row.get("nme_convocatoria") or "").strip(),
        "convocatoria_date": source_timestamp(row.get("ano_convo")),
        "presentation_date": source_timestamp(row.get("fcreacion_pd")),
        "presentation_year": source_year(row.get("fcreacion_pd")),
        "group_id": str(row.get("cod_grupo_gr") or "").strip().upper(),
        "group_name": str(row.get("nme_grupo_gr") or "").strip(),
        "owner_id": str(row.get("id_persona_pd") or "").strip(),
        "product_class": str(row.get("nme_clase_pd") or "").strip(),
        "measurement_type": str(row.get("nme_tipo_medicion_pd") or "").strip(),
        "typology": str(row.get("nme_tipologia_pd") or "").strip(),
        "type_code": str(row.get("id_tipo_pd_med") or "").strip(),
        "category": str(row.get("nme_categoria_pd") or "").strip(),
        "reported_title": str(row.get("nme_producto_pd") or "").strip(),
    }


def target_entity(measurements: Iterable[dict[str, Any]]) -> tuple[str, list[str]]:
    routes = [
        route_minciencias(value.get("product_class"), value.get("typology"))
        for value in measurements
    ]
    if not routes or any(route is None for route in routes):
        return "unmapped", ["unmapped"]
    targets = {route["entity"] for route in routes if route}
    if targets == {"works"}:
        return "works", []
    ordered = sorted(targets)
    return (ordered[0] if len(ordered) == 1 else "mixed"), ordered


def official_product_families(product: dict[str, Any]) -> set[str]:
    """Return only clear bibliographic families; unknown is non-blocking."""
    product_id = str(product.get("_id") or "").upper()
    prefix = product_id.split("-", 1)[0]
    prefix_map = {
        "ART": {"article", "editorial"},
        "LIB": {"book"},
        "CAP_LIB": {"book_chapter"},
        "TP": {"undergraduate_thesis"},
        "TM": {"graduate_thesis"},
        "TD": {"graduate_thesis"},
    }
    families = set(prefix_map.get(prefix, set()))
    minciencias = (product.get("bibliographic_info") or {}).get("minciencias") or {}
    for value in minciencias.get("measurements") or []:
        typology = norm_text(value.get("typology", ""))
        if "capitulo" in typology and "libro" in typology:
            families.add("book_chapter")
        elif "libro" in typology:
            families.add("book")
        elif "tesis de pregrado" in typology:
            families.add("undergraduate_thesis")
        elif "tesis" in typology:
            families.add("graduate_thesis")
        elif "articulo" in typology:
            families.update({"article", "editorial"})
    return families


def normalize_measured_product(
    rows: list[dict[str, Any]], *, normalized_at: int | None = None
) -> dict[str, Any]:
    if not rows:
        raise ValueError("at least one source row is required")
    product_ids = {str(row.get("id_producto_pd") or "").strip() for row in rows}
    product_ids.discard("")
    if len(product_ids) != 1:
        raise ValueError("source rows must contain exactly one id_producto_pd")
    product_id = next(iter(product_ids))
    normalized_at = int(time()) if normalized_at is None else int(normalized_at)

    measurements = _unique_dicts(
        (measurement_from_row(row) for row in rows), _measurement_key
    )
    measurements.sort(
        key=lambda value: (
            -(value.get("convocatoria_date") or 0),
            -(value.get("presentation_date") or 0),
            value.get("group_id") or "",
            norm_text(value.get("reported_title") or ""),
        )
    )
    titles: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for measurement in measurements:
        title = measurement.get("reported_title") or ""
        title_key = normalize_title(title)
        if title_key and title_key not in seen_titles:
            titles.append({"title": title, "lang": "", "source": "minciencias"})
            seen_titles.add(title_key)

    owners = sorted(
        {
            value.get("owner_id")
            for value in measurements
            if value.get("owner_id") and value.get("owner_id") != "0000000000"
        }
    )
    groups = _unique_dicts(
        (
            {"id": value["group_id"], "name": value.get("group_name") or ""}
            for value in measurements
            if value.get("group_id")
        ),
        lambda value: value["id"],
    )

    types = []
    for value in measurements:
        if value.get("typology"):
            types.append(
                {
                    "provenance": "minciencias",
                    "source": "minciencias",
                    "type": value["typology"],
                    "level": 1,
                    "parent": value.get("product_class") or None,
                }
            )
        if value.get("product_class"):
            types.append(
                {
                    "provenance": "minciencias",
                    "source": "minciencias",
                    "type": value["product_class"],
                    "level": 0,
                    "parent": None,
                }
            )
    types = _unique_dicts(
        types,
        lambda value: (
            value.get("source"),
            value.get("type"),
            value.get("level"),
            value.get("parent"),
        ),
    )
    impactu_types = {
        route["impactu_type"]
        for value in measurements
        if (
            route := route_minciencias(
                value.get("product_class"), value.get("typology")
            )
        )
        and route.get("impactu_type")
    }
    types.extend(
        {
            "provenance": "minciencias",
            "source": "impactu",
            "type": value,
            "level": 0,
            "parent": None,
        }
        for value in sorted(impactu_types)
    )

    ranking = []
    for value in measurements:
        for field, level in (
            ("type_code", None),
            ("measurement_type", 0),
            ("category", 1),
        ):
            if not value.get(field):
                continue
            item = {
                "provenance": "minciencias",
                "date": value.get("convocatoria_date"),
                "rank": value[field],
                "source": "minciencias",
            }
            if level is not None:
                item["level"] = level
            ranking.append(item)
    ranking = _unique_dicts(
        ranking,
        lambda value: (
            value.get("date"), value.get("rank"), value.get("level")
        ),
    )

    external_ids = [
        {
            "provenance": "minciencias",
            "source": "minciencias",
            "id": product_id,
        }
    ]
    product_match = PRODUCT_OWNER_RE.search(product_id)
    if product_match:
        external_ids.append(
            {
                "provenance": "minciencias",
                "source": "scienti",
                "id": {
                    "COD_RH": product_match.group(1),
                    "COD_PRODUCTO": product_match.group(2),
                },
            }
        )

    entity, excluded_targets = target_entity(measurements)
    entry = empty_kahi_work()
    entry.update(
        {
            "_id": product_id,
            "updated": [{"source": "minciencias", "time": normalized_at}],
            "titles": titles,
            "types": types,
            "external_ids": external_ids,
            "ranking": ranking,
            "groups": groups,
            # Ownership in the measurement dataset is evidence, not authorship.
            "authors": [],
            "author_count": 0,
            "bibliographic_info": {
                "minciencias": {
                    "schema_version": MEASUREMENT_SCHEMA_VERSION,
                    "type_catalog_version": IMPACTU_CATALOG.version,
                    "type_catalog_sha256": IMPACTU_CATALOG.source_sha256,
                    "source": "gruplac_production_data",
                    "product_ids": [product_id],
                    "owner_ids": owners,
                    "source_rows": len(rows),
                    "target_entity": entity,
                    "excluded_targets": excluded_targets,
                    "eligible_for_works": bool(titles) and entity == "works",
                    "measurements": measurements,
                }
            },
        }
    )
    return entry


def graph_work_families(work: dict[str, Any]) -> set[str]:
    families = set()
    for value in work.get("types") or []:
        if not isinstance(value, dict):
            continue
        family = type_family(str(value.get("type") or ""))
        if family and family != "unknown":
            families.add(family)
    return families


def graph_link_index_document(work: dict[str, Any]) -> dict[str, Any]:
    return {
        "_id": work["_id"],
        "title_keys": sorted(
            {
                title_key
                for value in work.get("titles") or []
                if isinstance(value, dict)
                and (title_key := normalize_title(value.get("title") or ""))
            }
        ),
        "year": work.get("year_published"),
        "author_ids": sorted(
            {
                str(value.get("id") or "")
                for value in work.get("authors") or []
                if isinstance(value, dict) and value.get("id")
            }
        ),
        "group_ids": sorted(
            {
                str(value.get("id") or "").upper()
                for value in work.get("groups") or []
                if isinstance(value, dict) and value.get("id")
            }
        ),
        "families": sorted(graph_work_families(work)),
    }


def product_linkage_terms(product: dict[str, Any]) -> dict[str, Any]:
    minciencias = (product.get("bibliographic_info") or {}).get("minciencias") or {}
    return {
        "title_keys": sorted(
            {
                title_key
                for value in product.get("titles") or []
                if isinstance(value, dict)
                and (title_key := normalize_title(value.get("title") or ""))
            }
        ),
        "years": sorted(
            {
                value.get("presentation_year")
                for value in minciencias.get("measurements") or []
                if isinstance(value.get("presentation_year"), int)
            }
        ),
        "owner_ids": sorted(set(minciencias.get("owner_ids") or [])),
        "group_ids": sorted(
            {
                str(value.get("id") or "").upper()
                for value in product.get("groups") or []
                if value.get("id")
            }
        ),
        "families": sorted(official_product_families(product)),
    }


def resolve_measured_product_link(
    product: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    graph_collection: str,
    oversized: bool = False,
) -> dict[str, Any]:
    """Resolve a product without turning weak title similarity into identity."""
    terms = product_linkage_terms(product)
    owners = set(terms["owner_ids"])
    groups = set(terms["group_ids"])
    product_families = set(terms["families"])
    generic = all(is_generic_title(value) for value in terms["title_keys"])
    evaluated = []
    admissible = []
    for candidate in candidates:
        candidate_owners = set(candidate.get("author_ids") or [])
        candidate_groups = set(candidate.get("group_ids") or [])
        candidate_families = set(candidate.get("families") or [])
        owner_overlap = sorted(owners & candidate_owners)
        group_overlap = sorted(groups & candidate_groups)
        compatible_type = bool(
            not product_families
            or not candidate_families
            or product_families & candidate_families
        )
        accepted = compatible_type and bool(owner_overlap or group_overlap)
        if generic:
            accepted = accepted and bool(owner_overlap and group_overlap)
        evidence = {
            "work_id": candidate["_id"],
            "owner_overlap": owner_overlap,
            "group_overlap": group_overlap,
            "compatible_type": compatible_type,
        }
        evaluated.append(evidence)
        if accepted:
            admissible.append(evidence)

    status = "unlinked"
    work_id = ""
    rule = "no_title_year_candidate"
    if oversized:
        status = "ambiguous"
        rule = "oversized_title_year_candidates"
    elif len(admissible) == 1:
        status = "linked"
        work_id = admissible[0]["work_id"]
        owner = bool(admissible[0]["owner_overlap"])
        group = bool(admissible[0]["group_overlap"])
        rule = "title_year_owner_group" if owner and group else (
            "title_year_owner" if owner else "title_year_group"
        )
    elif len(admissible) > 1:
        status = "ambiguous"
        rule = "multiple_anchored_candidates"
    elif candidates:
        status = "ambiguous"
        rule = "title_year_without_safe_anchor"

    return {
        "_id": product["_id"],
        "schema_version": MEASUREMENT_LINK_VERSION,
        "status": status,
        "work_id": work_id,
        "rule": rule,
        "graph_collection": graph_collection,
        "terms": terms,
        "candidates": evaluated[:25],
        "candidate_count": len(candidates),
        "generic_title": generic,
    }


def _append_unique(target: list[Any], values: Iterable[Any], key) -> None:
    existing = {key(value) for value in target}
    for value in values:
        value_key = key(value)
        if value_key not in existing:
            target.append(deepcopy(value))
            existing.add(value_key)


def enrich_graph_work(
    work: dict[str, Any],
    products: list[dict[str, Any]],
    links: list[dict[str, Any]],
    *,
    enriched_at: int | None = None,
) -> dict[str, Any]:
    """Attach official measurement evidence without changing authorship."""
    output = deepcopy(work)
    enriched_at = int(time()) if enriched_at is None else int(enriched_at)
    original_authors = deepcopy(output.get("authors") or [])
    output.setdefault("titles", [])
    output.setdefault("external_ids", [])
    output.setdefault("types", [])
    output.setdefault("ranking", [])
    output.setdefault("groups", [])
    output.setdefault("bibliographic_info", {})
    output.setdefault("updated", [])

    _append_unique(
        output["titles"],
        (value for product in products for value in product.get("titles") or []),
        lambda value: (value.get("source"), normalize_title(value.get("title") or "")),
    )
    _append_unique(
        output["external_ids"],
        (
            value
            for product in products
            for value in product.get("external_ids") or []
        ),
        lambda value: (value.get("source"), repr(value.get("id"))),
    )
    _append_unique(
        output["types"],
        (value for product in products for value in product.get("types") or []),
        lambda value: (
            value.get("source"), value.get("type"), value.get("level"), value.get("parent")
        ),
    )
    _append_unique(
        output["ranking"],
        (value for product in products for value in product.get("ranking") or []),
        lambda value: (
            value.get("source"), value.get("date"), value.get("rank"), value.get("level")
        ),
    )
    _append_unique(
        output["groups"],
        (value for product in products for value in product.get("groups") or []),
        lambda value: str(value.get("id") or ""),
    )

    measured_products = []
    for product in products:
        minciencias = (product.get("bibliographic_info") or {}).get("minciencias") or {}
        measured_products.append(
            {
                "product_id": product["_id"],
                "owner_ids": deepcopy(minciencias.get("owner_ids") or []),
                "measurements": deepcopy(minciencias.get("measurements") or []),
            }
        )
    link_by_product = {value["_id"]: value for value in links}
    minciencias_output = {
        "schema_version": MEASUREMENT_SCHEMA_VERSION,
        "source": "gruplac_production_data",
        "product_ids": sorted(product["_id"] for product in products),
        "measured_products": measured_products,
        "links": [
            {
                "product_id": product["_id"],
                "rule": (link_by_product.get(product["_id"]) or {}).get("rule", ""),
                "graph_collection": (
                    link_by_product.get(product["_id"]) or {}
                ).get("graph_collection", ""),
            }
            for product in products
        ],
    }
    existing_minciencias = output["bibliographic_info"].get("minciencias")
    if isinstance(existing_minciencias, dict):
        merged = deepcopy(existing_minciencias)
        merged.update(minciencias_output)
        minciencias_output = merged
    output["bibliographic_info"]["minciencias"] = minciencias_output
    output["updated"] = [
        value for value in output["updated"] if value.get("source") != "minciencias"
    ]
    output["updated"].append({"source": "minciencias", "time": enriched_at})
    output["authors"] = original_authors
    output["author_count"] = len(original_authors)
    return output


def _bulk_replace(collection, documents: list[dict[str, Any]]) -> None:
    if not documents:
        return
    operations = [ReplaceOne({"_id": value["_id"]}, value, upsert=True) for value in documents]
    try:
        collection.bulk_write(operations, ordered=False)
    except (TypeError, NotImplementedError):
        # Compatibility with older mongomock releases used by the test suite.
        for value in documents:
            collection.replace_one({"_id": value["_id"]}, value, upsert=True)


class MincienciasMeasurementPipeline:
    """Checkpointed official-measurement normalization and graph enrichment."""

    def __init__(self, db):
        self.db = db
        self.runs = db[MEASUREMENT_RUNS]

    def _prepare_run(self, run_name: str, kind: str, config: dict[str, Any]) -> dict[str, Any]:
        _validate_name(run_name, "run_name")
        existing = self.runs.find_one({"_id": run_name})
        if existing:
            if existing.get("kind") != kind or (existing.get("config") or {}) != config:
                raise ValueError(f"measurement run {run_name!r} exists with a different config")
            return existing
        document = {
            "_id": run_name,
            "kind": kind,
            "status": "created",
            "created_at": int(time()),
            "config": config,
            "metrics": {},
        }
        self.runs.insert_one(document)
        return document

    def normalize(
        self,
        *,
        run_name: str,
        source_collection: str,
        destination_collection: str,
        batch_size: int = 500,
        progress_every: int = 10000,
        limit: int = 0,
        replace: bool = False,
    ) -> dict[str, Any]:
        for value, label in (
            (source_collection, "source_collection"),
            (destination_collection, "destination_collection"),
        ):
            _validate_collection(value, label)
        if batch_size < 1 or progress_every < 1 or limit < 0:
            raise ValueError("batch_size and progress_every must be positive; limit cannot be negative")
        if source_collection not in self.db.list_collection_names():
            raise ValueError(f"source collection {source_collection!r} was not found")
        if source_collection == destination_collection:
            raise ValueError("normalization source and destination must be different")
        config = {
            "schema_version": MEASUREMENT_SCHEMA_VERSION,
            "source_collection": source_collection,
            "destination_collection": destination_collection,
            "batch_size": batch_size,
            "limit": limit,
        }
        existing_run = self.runs.find_one({"_id": run_name})
        if replace:
            self.db[destination_collection].drop()
            self.runs.delete_one({"_id": run_name})
            existing_run = None
        elif (
            existing_run is None
            and destination_collection in self.db.list_collection_names()
            and self.db[destination_collection].count_documents({})
        ):
            raise ValueError(
                f"normalization destination {destination_collection!r} already contains data, "
                "resume its run or use a new versioned destination"
            )
        run = self._prepare_run(run_name, "normalize", config)
        if run.get("status") == "complete":
            if destination_collection not in self.db.list_collection_names():
                raise RuntimeError("completed normalization destination is missing")
            return run.get("summary") or {}
        destination = self.db[destination_collection]
        last_product_id = str(run.get("last_product_id") or "")
        query: dict[str, Any] = {
            "id_producto_pd": {"$type": "string", "$ne": ""}
        }
        if last_product_id:
            query["id_producto_pd"]["$gt"] = last_product_id
        cursor = self.db[source_collection].find(query).sort("id_producto_pd", ASCENDING)
        cursor.batch_size(max(batch_size, 1000))
        buffer: list[dict[str, Any]] = []
        current_id = ""
        rows: list[dict[str, Any]] = []
        processed = int(run.get("processed_products") or 0)
        source_rows = int(run.get("source_rows") or 0)
        normalized_at = int(run.get("normalized_at") or time())
        self.runs.update_one(
            {"_id": run_name},
            {"$set": {"status": "running", "started_at": int(time()), "normalized_at": normalized_at}},
        )

        def flush() -> None:
            nonlocal buffer, last_product_id
            if not buffer:
                return
            _bulk_replace(destination, buffer)
            last_product_id = str(buffer[-1]["_id"])
            self.runs.update_one(
                {"_id": run_name},
                {
                    "$set": {
                        "last_product_id": last_product_id,
                        "processed_products": processed,
                        "source_rows": source_rows,
                        "last_checkpoint_at": int(time()),
                    }
                },
            )
            buffer = []

        def append_product(product_rows: list[dict[str, Any]]) -> bool:
            nonlocal processed, source_rows
            if not product_rows or (limit and processed >= limit):
                return False
            buffer.append(
                normalize_measured_product(product_rows, normalized_at=normalized_at)
            )
            processed += 1
            source_rows += len(product_rows)
            if len(buffer) >= batch_size:
                flush()
            if processed % progress_every == 0:
                print(
                    f"INFO: measurement normalization run={run_name} products={processed} rows={source_rows}.",
                    flush=True,
                )
            return True

        try:
            for row in cursor:
                product_id = str(row.get("id_producto_pd") or "")
                if current_id and product_id != current_id:
                    if not append_product(rows):
                        break
                    rows = []
                if limit and processed >= limit:
                    break
                current_id = product_id
                rows.append(row)
            else:
                if rows and (not limit or processed < limit):
                    append_product(rows)
            flush()
            destination.create_index("titles.title")
            destination.create_index([("external_ids.source", ASCENDING), ("external_ids.id", ASCENDING)])
            destination.create_index("groups.id")
            destination.create_index("bibliographic_info.minciencias.owner_ids")
            destination.create_index("bibliographic_info.minciencias.eligible_for_works")
            destination.create_index("bibliographic_info.minciencias.measurements.group_id")
            summary = {
                "run_name": run_name,
                "status": "complete",
                "source_collection": source_collection,
                "destination_collection": destination_collection,
                "products": destination.count_documents({}),
                "processed_products": processed,
                "source_rows": source_rows,
                "eligible_for_works": destination.count_documents(
                    {"bibliographic_info.minciencias.eligible_for_works": True}
                ),
                "missing_titles": destination.count_documents({"titles.0": {"$exists": False}}),
            }
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"status": "complete", "finished_at": int(time()), "summary": summary}},
            )
            return summary
        except Exception as error:
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"status": "failed", "failed_at": int(time()), "error": f"{type(error).__name__}: {error}"}},
            )
            raise
        finally:
            cursor.close()

    def _build_graph_index(
        self,
        *,
        run_name: str,
        graph_collection: str,
        index_collection: str,
        batch_size: int,
        progress_every: int,
    ) -> int:
        index = self.db[index_collection]
        index.drop()
        buffer = []
        processed = 0
        projection = {
            "titles": 1,
            "year_published": 1,
            "authors.id": 1,
            "groups.id": 1,
            "types": 1,
        }
        for work in self.db[graph_collection].find({}, projection).batch_size(batch_size):
            buffer.append(graph_link_index_document(work))
            processed += 1
            if len(buffer) >= batch_size:
                _bulk_replace(index, buffer)
                buffer = []
            if processed % progress_every == 0:
                print(
                    f"INFO: measurement links run={run_name} graph_index={processed}.",
                    flush=True,
                )
        _bulk_replace(index, buffer)
        index.create_index([("title_keys", ASCENDING), ("year", ASCENDING)])
        index.create_index("author_ids")
        index.create_index("group_ids")
        return processed

    def link(
        self,
        *,
        run_name: str,
        measured_collection: str,
        graph_collection: str,
        links_collection: str,
        batch_size: int = 500,
        progress_every: int = 10000,
        max_candidates: int = 100,
        limit: int = 0,
        replace: bool = False,
    ) -> dict[str, Any]:
        for value, label in (
            (measured_collection, "measured_collection"),
            (graph_collection, "graph_collection"),
            (links_collection, "links_collection"),
        ):
            _validate_collection(value, label)
        if batch_size < 1 or progress_every < 1 or max_candidates < 1 or limit < 0:
            raise ValueError("link batch/progress/candidate values must be positive")
        missing = sorted(
            {measured_collection, graph_collection} - set(self.db.list_collection_names())
        )
        if missing:
            raise ValueError(f"link source collections are missing: {missing}")
        if links_collection in {measured_collection, graph_collection}:
            raise ValueError("links destination must differ from both source collections")
        config = {
            "link_version": MEASUREMENT_LINK_VERSION,
            "measured_collection": measured_collection,
            "graph_collection": graph_collection,
            "links_collection": links_collection,
            "batch_size": batch_size,
            "max_candidates": max_candidates,
            "limit": limit,
        }
        existing_run = self.runs.find_one({"_id": run_name})
        if replace:
            self.db[links_collection].drop()
            self.runs.delete_one({"_id": run_name})
            existing_run = None
        elif (
            existing_run is None
            and links_collection in self.db.list_collection_names()
            and self.db[links_collection].count_documents({})
        ):
            raise ValueError(
                f"links destination {links_collection!r} already contains data, "
                "resume its run or use a new versioned destination"
            )
        run = self._prepare_run(run_name, "link", config)
        if run.get("status") == "complete":
            if links_collection not in self.db.list_collection_names():
                raise RuntimeError("completed measurement links collection is missing")
            return run.get("summary") or {}
        index_collection = f"__yuku_{links_collection}_{run_name}_graph_index"
        graph_indexed = int(run.get("graph_indexed") or 0)
        if run.get("graph_index_status") != "complete" or index_collection not in self.db.list_collection_names():
            graph_indexed = self._build_graph_index(
                run_name=run_name,
                graph_collection=graph_collection,
                index_collection=index_collection,
                batch_size=batch_size,
                progress_every=progress_every,
            )
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"graph_index_status": "complete", "graph_indexed": graph_indexed}},
            )

        links = self.db[links_collection]
        graph_index = self.db[index_collection]
        last_product_id = str(run.get("last_product_id") or "")
        query = {"_id": {"$gt": last_product_id}} if last_product_id else {}
        cursor = self.db[measured_collection].find(query).sort("_id", ASCENDING)
        buffer = []
        processed = int(run.get("processed_products") or 0)
        counters = Counter(run.get("status_counts") or {})
        self.runs.update_one(
            {"_id": run_name}, {"$set": {"status": "running", "started_at": int(time())}}
        )

        def flush() -> None:
            nonlocal buffer, last_product_id
            if not buffer:
                return
            _bulk_replace(links, buffer)
            last_product_id = str(buffer[-1]["_id"])
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"last_product_id": last_product_id, "processed_products": processed, "status_counts": dict(counters), "last_checkpoint_at": int(time())}},
            )
            buffer = []

        try:
            for product in cursor:
                if limit and processed >= limit:
                    break
                terms = product_linkage_terms(product)
                found: dict[Any, dict[str, Any]] = {}
                oversized = False
                for title_key in terms["title_keys"]:
                    for year in terms["years"]:
                        matches = list(
                            graph_index.find(
                                {"title_keys": title_key, "year": year}
                            ).limit(max_candidates + 1)
                        )
                        if len(matches) > max_candidates:
                            oversized = True
                            matches = matches[:max_candidates]
                        for match in matches:
                            found[match["_id"]] = match
                link = resolve_measured_product_link(
                    product,
                    list(found.values()),
                    graph_collection=graph_collection,
                    oversized=oversized,
                )
                buffer.append(link)
                processed += 1
                counters[link["status"]] += 1
                if len(buffer) >= batch_size:
                    flush()
                if processed % progress_every == 0:
                    print(
                        f"INFO: measurement links run={run_name} products={processed} status={dict(counters)}.",
                        flush=True,
                    )
            flush()
            links.create_index([("status", ASCENDING), ("work_id", ASCENDING)])
            links.create_index("rule")
            expected = min(
                limit or self.db[measured_collection].count_documents({}),
                self.db[measured_collection].count_documents({}),
            )
            actual = links.count_documents({})
            critical = int(actual != expected)
            summary = {
                "run_name": run_name,
                "status": "complete" if not critical else "failed",
                "measured_collection": measured_collection,
                "graph_collection": graph_collection,
                "links_collection": links_collection,
                "graph_works_indexed": graph_indexed,
                "products": actual,
                "expected_products": expected,
                "status_counts": dict(counters),
                "critical_anomalies": critical,
            }
            status = "complete" if not critical else "failed"
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"status": status, "finished_at": int(time()), "summary": summary}},
            )
            if critical:
                raise RuntimeError(f"measurement link audit failed: {summary}")
            graph_index.drop()
            return summary
        except Exception as error:
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"status": "failed", "failed_at": int(time()), "error": f"{type(error).__name__}: {error}"}},
            )
            raise
        finally:
            cursor.close()

    def materialize(
        self,
        *,
        run_name: str,
        graph_collection: str,
        measured_collection: str,
        links_collection: str,
        target_collection: str,
        batch_size: int = 500,
        progress_every: int = 10000,
        replace: bool = False,
    ) -> dict[str, Any]:
        for value, label in (
            (graph_collection, "graph_collection"),
            (measured_collection, "measured_collection"),
            (links_collection, "links_collection"),
            (target_collection, "target_collection"),
        ):
            _validate_collection(value, label)
        if batch_size < 1 or progress_every < 1:
            raise ValueError("batch_size and progress_every must be positive")
        missing = sorted(
            {graph_collection, measured_collection, links_collection}
            - set(self.db.list_collection_names())
        )
        if missing:
            raise ValueError(f"materialization source collections are missing: {missing}")
        link_graphs = self.db[links_collection].distinct("graph_collection")
        if link_graphs and set(link_graphs) != {graph_collection}:
            raise RuntimeError(
                f"links were built for {link_graphs}, not {graph_collection!r}"
            )
        if target_collection in {
            graph_collection,
            measured_collection,
            links_collection,
        }:
            raise ValueError("enriched target must differ from every source collection")
        config = {
            "schema_version": MEASUREMENT_SCHEMA_VERSION,
            "graph_collection": graph_collection,
            "measured_collection": measured_collection,
            "links_collection": links_collection,
            "target_collection": target_collection,
            "batch_size": batch_size,
        }
        if replace:
            if target_collection in self.db.list_collection_names():
                raise ValueError(
                    "published/versioned graph targets are immutable; choose a new target name"
                )
            self.runs.delete_one({"_id": run_name})
        run = self._prepare_run(run_name, "materialize", config)
        if run.get("status") == "complete":
            if target_collection not in self.db.list_collection_names():
                raise RuntimeError("completed enriched graph collection is missing")
            return run.get("summary") or {}
        if target_collection in self.db.list_collection_names():
            if run.get("rename_started_at"):
                return self._finalize_enriched_publication(
                    run_name=run_name,
                    target_collection=target_collection,
                    previous_collection=str(run.get("previous_collection") or ""),
                    enriched=int(run.get("enriched_works") or 0),
                    attached_products=int(run.get("attached_products") or 0),
                    links_collection=links_collection,
                )
            raise ValueError(
                f"versioned enriched graph {target_collection!r} already exists"
            )
        temporary_collection = f"__yuku_{target_collection}_{run_name}_build"
        output = self.db[temporary_collection]
        last_work_id = run.get("last_work_id")
        query = {"_id": {"$gt": last_work_id}} if last_work_id is not None else {}
        cursor = self.db[graph_collection].find(query).sort("_id", ASCENDING)
        processed = int(run.get("processed_works") or 0)
        enriched = int(run.get("enriched_works") or 0)
        attached_products = int(run.get("attached_products") or 0)
        enriched_at = int(run.get("enriched_at") or time())
        self.runs.update_one(
            {"_id": run_name},
            {"$set": {"status": "running", "started_at": int(time()), "enriched_at": enriched_at, "temporary_collection": temporary_collection}},
        )

        def process_batch(batch: list[dict[str, Any]]) -> None:
            nonlocal processed, enriched, attached_products, last_work_id
            if not batch:
                return
            ids = [value["_id"] for value in batch]
            link_documents = list(
                self.db[links_collection].find(
                    {"status": "linked", "work_id": {"$in": ids}}
                )
            )
            links_by_work: dict[Any, list[dict[str, Any]]] = {}
            for value in link_documents:
                links_by_work.setdefault(value["work_id"], []).append(value)
            product_ids = [value["_id"] for value in link_documents]
            products = {
                value["_id"]: value
                for value in self.db[measured_collection].find(
                    {"_id": {"$in": product_ids}}
                )
            }
            documents = []
            for work in batch:
                work_links = links_by_work.get(work["_id"], [])
                work_products = [
                    products[value["_id"]]
                    for value in work_links
                    if value["_id"] in products
                ]
                if work_products:
                    work = enrich_graph_work(
                        work, work_products, work_links, enriched_at=enriched_at
                    )
                    enriched += 1
                    attached_products += len(work_products)
                documents.append(work)
            _bulk_replace(output, documents)
            processed += len(documents)
            last_work_id = documents[-1]["_id"]
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"last_work_id": last_work_id, "processed_works": processed, "enriched_works": enriched, "attached_products": attached_products, "last_checkpoint_at": int(time())}},
            )
            if processed % progress_every < len(documents):
                print(
                    f"INFO: measurement graph run={run_name} works={processed} enriched={enriched} products={attached_products}.",
                    flush=True,
                )

        try:
            batch = []
            for work in cursor:
                batch.append(work)
                if len(batch) >= batch_size:
                    process_batch(batch)
                    batch = []
            process_batch(batch)
            source_count = self.db[graph_collection].count_documents({})
            output_count = output.count_documents({})
            linked_products = self.db[links_collection].count_documents({"status": "linked"})
            critical_counts = {
                "graph_coverage": abs(source_count - output_count),
                "linked_product_coverage": abs(linked_products - attached_products),
            }
            critical = sum(critical_counts.values())
            if critical:
                raise RuntimeError(
                    f"enriched graph audit failed: {critical_counts}"
                )
            output.create_index("year_published")
            output.create_index("titles.title")
            output.create_index("authors.id")
            output.create_index("groups.id")
            output.create_index([("external_ids.source", ASCENDING), ("external_ids.id", ASCENDING)])
            output.create_index("bibliographic_info.minciencias.product_ids")
            publication = self.db[WORK_GRAPH_PUBLICATIONS]
            previous = publication.find_one({"_id": "current"}) or {}
            self.runs.update_one(
                {"_id": run_name},
                {
                    "$set": {
                        "rename_started_at": int(time()),
                        "previous_collection": previous.get("current_collection", ""),
                    }
                },
            )
            output.rename(target_collection, dropTarget=False)
            self.runs.update_one(
                {"_id": run_name}, {"$set": {"renamed_at": int(time())}}
            )
            return self._finalize_enriched_publication(
                run_name=run_name,
                target_collection=target_collection,
                previous_collection=str(previous.get("current_collection") or ""),
                enriched=enriched,
                attached_products=attached_products,
                links_collection=links_collection,
            )
        except Exception as error:
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"status": "failed", "failed_at": int(time()), "error": f"{type(error).__name__}: {error}"}},
            )
            raise
        finally:
            cursor.close()

    def _finalize_enriched_publication(
        self,
        *,
        run_name: str,
        target_collection: str,
        previous_collection: str,
        enriched: int,
        attached_products: int,
        links_collection: str,
    ) -> dict[str, Any]:
        """Finish the small post-rename window safely after a process crash."""
        if target_collection not in self.db.list_collection_names():
            raise RuntimeError("cannot publish a missing enriched graph")
        works = self.db[target_collection].count_documents({})
        published_at = int(time())
        publication = self.db[WORK_GRAPH_PUBLICATIONS]
        publication.replace_one(
            {"_id": run_name},
            {
                "_id": run_name,
                "status": "published",
                "graph_version": f"{GRAPH_VERSION}-measurements-v1",
                "collection": target_collection,
                "previous_collection": previous_collection,
                "works": works,
                "enriched_works": enriched,
                "official_products": attached_products,
                "published_at": published_at,
            },
            upsert=True,
        )
        publication.replace_one(
            {"_id": "current"},
            {
                "_id": "current",
                "current_collection": target_collection,
                "current_run_name": run_name,
                "previous_collection": previous_collection,
                "published_at": published_at,
            },
            upsert=True,
        )
        summary = {
            "run_name": run_name,
            "status": "complete",
            "source_graph": (self.runs.find_one({"_id": run_name}) or {}).get(
                "config", {}
            ).get("graph_collection", ""),
            "collection": target_collection,
            "works": works,
            "enriched_works": enriched,
            "official_products": attached_products,
            "unlinked_official_products": self.db[links_collection].count_documents(
                {"status": {"$ne": "linked"}}
            ),
            "critical_anomalies": 0,
        }
        self.runs.update_one(
            {"_id": run_name},
            {
                "$set": {
                    "status": "complete",
                    "finished_at": published_at,
                    "summary": summary,
                }
            },
        )
        return summary
