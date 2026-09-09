"""Audited materialization of public ScienTI research groups for Kahi."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from pymongo import ASCENDING, ReplaceOne


AFFILIATION_RUNS = "scienti_affiliation_materialization_runs"
AFFILIATION_AUDITS = "scienti_affiliation_materialization_audits"
AFFILIATION_PUBLICATIONS = "scienti_affiliation_publications"
GRUPLAC_AUDITS = "scienti_gruplac_normalization_audits"
MATERIALIZER_VERSION = "scienti-affiliations-v1"
REQUIRED_GRUPLAC_PARSER_VERSION = "3.2.0"
GROUP_CODE_RE = re.compile(r"COL\d{7}")
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
COLLECTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,119}")
EMPTY_VALUES = {"", "-", "N/A", "NO REGISTRA", "NONE", "NULL"}
AFFILIATION_FIELDS = {
    "updated", "names", "aliases", "abbreviations", "types",
    "year_established", "status", "relations", "addresses",
    "external_urls", "external_ids", "subjects", "ranking", "description",
}
FINGERPRINT_PROJECTIONS = {
    "recognized": {
        "codigo_grupo": 1, "nombre_grupo": 1, "nombre_preferido": 1,
        "nombres_historicos": 1, "instituciones_historicas": 1,
        "clasificaciones_historicas": 1, "url_gruplac": 1,
        "url_perfiles": 1,
    },
    "open_data": {
        "cod_grupo_gr": 1, "nme_grupo_gr": 1, "fcreacion_gr": 1,
        "ano_convo": 1, "inst_aval": 1, "nme_clasificacion_gr": 1,
        "orden_clas_gr": 1, "nme_departamento_gr": 1,
        "nme_municipio_gr": 1, "nme_pais_gr": 1, "cod_dane_gr": 1,
        "id_area_con_gr": 1, "nme_gran_area_gr": 1, "nme_area_gr": 1,
        "nme_area_esp_gr": 1, "nme_prog_colc1_gr": 1,
        "nme_prog_colc2_gr": 1,
    },
    "gruplac": {
        "group_code": 1, "group_name": 1, "nro": 1, "url_gruplac": 1,
        "basic": 1, "institutions": 1, "strategic_plan": 1,
        "research_lines": 1, "parser": 1, "source.fetched_at": 1,
        "source.content_sha256": 1,
    },
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    return "" if text.upper() in EMPTY_VALUES else text


def normalized_text(value: Any) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", clean_text(value))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def group_code(value: Any) -> str:
    value = clean_text(value).upper()
    return value if GROUP_CODE_RE.fullmatch(value) else ""


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc)
    value = clean_text(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)
    except ValueError:
        match = re.search(r"(?:19|20)\d{2}", value)
        return (
            datetime(int(match.group()), 1, 1, tzinfo=timezone.utc)
            if match else None
        )


def timestamp(value: Any) -> int:
    parsed = parse_datetime(value)
    return int(parsed.timestamp()) if parsed else 0


def year(value: Any) -> int | None:
    parsed = parse_datetime(value)
    return parsed.year if parsed else None


def append_unique(values: list, value: Any, key=lambda item: item) -> None:
    if value and key(value) not in {key(item) for item in values}:
        values.append(value)


def clean_group_name(value: Any) -> str:
    value = clean_text(value)
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        value = clean_text(value[1:-1])
    return value


def normalize_rank(value: Any) -> str:
    value = clean_text(value)
    match = re.match(r"^(A1|A|B|C|D|00)\b", value, flags=re.I)
    value = match.group(1).upper() if match else value
    return "Reconocido" if value == "00" else value


def extract_nro(*urls: Any) -> str:
    for value in urls:
        query = parse_qs(urlparse(clean_text(value)).query)
        nro = clean_text((query.get("nro") or [""])[0])
        if nro.isdigit():
            return nro
    return ""


def _address(row: dict[str, Any], basic: dict[str, Any]) -> dict[str, Any] | None:
    state = clean_text(row.get("nme_departamento_gr"))
    city = clean_text(row.get("nme_municipio_gr"))
    country = clean_text(row.get("nme_pais_gr"))
    if not state and not city:
        location = clean_text(basic.get("Departamento - Ciudad"))
        if " - " in location:
            state, city = [clean_text(item) for item in location.split(" - ", 1)]
    if not any((state, city, country)):
        return None
    return {
        "lat": "", "lng": "", "postcode": "", "state": state,
        "city": city, "country": country,
        "country_code": "CO" if normalized_text(country) == "colombia" else "",
    }


def _subject(level: int, name: Any, identifier: Any = "") -> dict[str, Any] | None:
    name = clean_text(name)
    if not name:
        return None
    identifier = clean_text(identifier)
    return {
        "level": level, "name": name, "id": "",
        "external_ids": (
            [{"source": "OECD", "id": identifier}] if identifier else []
        ),
    }


def _subjects(row: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    area_id = clean_text(row.get("id_area_con_gr"))
    hierarchy = [
        _subject(0, row.get("nme_gran_area_gr"), area_id[:1]),
        _subject(1, row.get("nme_area_gr"), area_id),
        _subject(2, row.get("nme_area_esp_gr")),
    ]
    hierarchy = [item for item in hierarchy if item]
    if hierarchy:
        output.append({
            "provenance": "minciencias", "source": "OECD",
            "subjects": hierarchy,
        })
    programs = [
        _subject(0, row.get("nme_prog_colc1_gr")),
        _subject(1, row.get("nme_prog_colc2_gr")),
    ]
    programs = [item for item in programs if item]
    if programs:
        output.append({
            "provenance": "minciencias", "source": "minciencias",
            "subjects": programs,
        })
    lines = [
        _subject(0, item) for item in profile.get("research_lines", [])
    ]
    lines = [item for item in lines if item]
    if lines:
        output.append({"source": "scienti", "subjects": lines})
    return output


def _rankings(rows: list[dict[str, Any]], recognized: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for row in sorted(rows, key=lambda item: timestamp(item.get("ano_convo"))):
        rank = normalize_rank(row.get("nme_clasificacion_gr"))
        date = timestamp(row.get("ano_convo"))
        if not rank or (date, rank) in seen:
            continue
        item = {"source": "minciencias", "rank": rank, "date": date}
        order = clean_text(row.get("orden_clas_gr")).removesuffix(".0")
        if order:
            item["order"] = order
        output.append(item)
        seen.add((date, rank))
    for item in recognized.get("clasificaciones_historicas", []) or []:
        rank = normalize_rank(item.get("clasificacion"))
        date = timestamp(item.get("año"))
        if rank and (date, rank) not in seen:
            output.append({"source": "minciencias", "rank": rank, "date": date})
            seen.add((date, rank))
    return sorted(output, key=lambda item: (item.get("date", 0), item["rank"]))


def build_group_affiliation(
    code: str,
    recognized: dict[str, Any],
    rows: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    built_at: datetime,
) -> dict[str, Any]:
    """Build one Kahi-compatible group without resolving Kahi-owned relations."""
    rows = sorted(rows, key=lambda item: timestamp(item.get("ano_convo")), reverse=True)
    latest = rows[0] if rows else {}
    names = [
        profile.get("group_name"), recognized.get("nombre_preferido"),
        recognized.get("nombre_grupo"), latest.get("nme_grupo_gr"),
    ]
    names.extend(
        item.get("nombre")
        for item in (recognized.get("nombres_historicos") or [])
    )
    names.extend(row.get("nme_grupo_gr") for row in rows)
    unique_names: list[str] = []
    for value in names:
        append_unique(unique_names, clean_group_name(value), normalized_text)
    canonical_name = unique_names[0] if unique_names else ""

    relations: list[dict[str, Any]] = []
    institution_names: list[str] = []
    for item in profile.get("institutions", []) or []:
        append_unique(institution_names, clean_text(item.get("name")), normalized_text)
    for row in rows:
        for value in clean_text(row.get("inst_aval")).split("|"):
            append_unique(institution_names, clean_text(value), normalized_text)
    historical = sorted(
        recognized.get("instituciones_historicas", []) or [],
        key=lambda item: int(item.get("año") or 0), reverse=True,
    )
    for item in historical:
        append_unique(
            institution_names, clean_text(item.get("institucion")), normalized_text
        )
    for name in institution_names:
        relations.append({"id": "", "name": name, "types": []})

    profile_time = timestamp((profile.get("source") or {}).get("fetched_at"))
    minciencias_time = timestamp(latest.get("ano_convo")) or int(built_at.timestamp())
    external_urls: list[dict[str, str]] = []
    for source, value in (
        ("scienti", profile.get("url_gruplac") or recognized.get("url_gruplac")),
        ("minciencias", recognized.get("url_perfiles")),
    ):
        value = clean_text(value)
        if value:
            append_unique(
                external_urls,
                {"provenance": "scienti", "source": source, "url": value},
                lambda item: item["url"],
            )
    external_ids = [{"source": "minciencias", "id": code}]
    nro = clean_text(profile.get("nro")) or extract_nro(
        profile.get("url_gruplac"), recognized.get("url_gruplac")
    )
    if nro:
        external_ids.append({"source": "scienti", "id": nro})

    basic = profile.get("basic") or {}
    established = year(basic.get("Año y mes de formación"))
    if established is None:
        years = [year(row.get("fcreacion_gr")) for row in rows]
        established = min(item for item in years if item) if any(years) else None
    address = _address(latest, basic)
    description = profile.get("strategic_plan") or {}
    updated = [{"source": "minciencias", "time": minciencias_time}]
    if profile:
        updated.insert(0, {
            "source": "scienti",
            "time": profile_time or int(built_at.timestamp()),
        })
    return {
        "_id": code,
        "updated": updated,
        "names": ([{"source": "scienti" if profile else "minciencias",
                    "lang": "es", "name": canonical_name}] if canonical_name else []),
        "aliases": unique_names[1:],
        "abbreviations": [],
        "types": [{"source": "scienti" if profile else "minciencias", "type": "group"}],
        "year_established": established,
        "status": [],
        "relations": relations,
        "addresses": [address] if address else [],
        "external_urls": external_urls,
        "external_ids": external_ids,
        "subjects": _subjects(latest, profile),
        "ranking": _rankings(rows, recognized),
        "description": (
            [{"source": "scienti", "description": deepcopy(description)}]
            if description else []
        ),
    }


def _canonical_json(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document, sort_keys=True, ensure_ascii=False, default=str,
        separators=(",", ":"),
    ).encode("utf-8")


def collection_fingerprint(collection, projection: dict[str, int]) -> dict[str, Any]:
    digest = sha256()
    count = 0
    for document in collection.find({}, projection).sort("_id", ASCENDING):
        digest.update(_canonical_json(document))
        digest.update(b"\n")
        count += 1
    return {"documents": count, "sha256": digest.hexdigest()}


def _bulk_replace(collection, documents: Iterable[dict[str, Any]]) -> None:
    documents = list(documents)
    if not documents:
        return
    operations = [ReplaceOne({"_id": item["_id"]}, item, upsert=True) for item in documents]
    try:
        collection.bulk_write(operations, ordered=False)
    except TypeError:  # Compatibility with older mongomock/PyMongo adapters.
        for item in documents:
            collection.replace_one({"_id": item["_id"]}, item, upsert=True)


class ScientiAffiliationMaterializer:
    """Create, audit and atomically publish a reproducible group snapshot."""

    def __init__(
        self,
        db,
        *,
        run_name: str,
        recognized_collection: str,
        open_data_collection: str,
        gruplac_collection: str,
        gruplac_audit_name: str,
        target_collection: str,
        batch_size: int = 500,
        progress_every: int = 1000,
        expected_groups: int = 0,
    ):
        if not NAME_RE.fullmatch(run_name or ""):
            raise ValueError("invalid affiliation materialization run name")
        for value in (
            recognized_collection, open_data_collection, gruplac_collection,
            target_collection,
        ):
            if not COLLECTION_RE.fullmatch(value or "") or value.startswith("system."):
                raise ValueError("invalid affiliation collection name")
        if target_collection in {
            recognized_collection, open_data_collection, gruplac_collection
        }:
            raise ValueError("affiliation target must differ from every source")
        if batch_size < 1 or progress_every < 1 or expected_groups < 0:
            raise ValueError("invalid affiliation materialization limits")
        self.db = db
        self.run_name = run_name
        self.recognized_collection = recognized_collection
        self.open_data_collection = open_data_collection
        self.gruplac_collection = gruplac_collection
        self.gruplac_audit_name = gruplac_audit_name
        self.target_collection = target_collection
        self.temp_collection = f"__yuku_{run_name}_affiliations"
        self.batch_size = batch_size
        self.progress_every = progress_every
        self.expected_groups = expected_groups
        self.runs = db[AFFILIATION_RUNS]
        self.config = {
            "materializer_version": MATERIALIZER_VERSION,
            "recognized_collection": recognized_collection,
            "open_data_collection": open_data_collection,
            "gruplac_collection": gruplac_collection,
            "gruplac_audit_name": gruplac_audit_name,
            "target_collection": target_collection,
            "expected_groups": expected_groups,
        }
        self.config_hash = sha256(_canonical_json(self.config)).hexdigest()

    def _validate_source_audit(self) -> None:
        audit = self.db[GRUPLAC_AUDITS].find_one(
            {"_id": self.gruplac_audit_name}
        ) or {}
        critical = int(
            audit.get("critical_anomalies")
            or (audit.get("summary") or {}).get("critical_anomalies")
            or 0
        )
        config = audit.get("config") or {}
        if audit.get("status") != "passed" or critical:
            raise RuntimeError("GrupLAC source audit is not passed")
        if config.get("normalized_collection") != self.gruplac_collection:
            raise RuntimeError("GrupLAC audit does not prove the selected collection")
        if config.get("expected_parser_version") != REQUIRED_GRUPLAC_PARSER_VERSION:
            raise RuntimeError("GrupLAC audit does not prove the required parser version")
        audited_recognized = config.get("recognized_groups_collection")
        if audited_recognized and audited_recognized != self.recognized_collection:
            raise RuntimeError("GrupLAC audit used another recognized-groups source")

    def _fingerprints(self) -> dict[str, dict[str, Any]]:
        return {
            "recognized": collection_fingerprint(
                self.db[self.recognized_collection], FINGERPRINT_PROJECTIONS["recognized"]
            ),
            "open_data": collection_fingerprint(
                self.db[self.open_data_collection], FINGERPRINT_PROJECTIONS["open_data"]
            ),
            "gruplac": collection_fingerprint(
                self.db[self.gruplac_collection], FINGERPRINT_PROJECTIONS["gruplac"]
            ),
        }

    def _prepare(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        missing = sorted(
            {self.recognized_collection, self.open_data_collection,
             self.gruplac_collection} - set(self.db.list_collection_names())
        )
        if missing:
            raise RuntimeError(f"affiliation source collections are missing: {missing}")
        self._validate_source_audit()
        fingerprints = self._fingerprints()
        if any(item["documents"] < 1 for item in fingerprints.values()):
            raise RuntimeError("affiliation source collection is empty")
        previous = self.runs.find_one({"_id": self.run_name})
        if previous and previous.get("config_hash") != self.config_hash:
            raise ValueError("affiliation run exists with another configuration")
        if previous and previous.get("source_fingerprints") != fingerprints:
            raise RuntimeError("affiliation sources changed since the run started")
        if previous and previous.get("status") == "complete":
            return previous, fingerprints
        if not previous:
            if self.target_collection in self.db.list_collection_names():
                raise RuntimeError("immutable affiliation target already exists")
            self.db[self.temp_collection].drop()
            previous = {
                "_id": self.run_name, "status": "pending",
                "config": deepcopy(self.config), "config_hash": self.config_hash,
                "source_fingerprints": deepcopy(fingerprints),
                "created_at": utc_now(), "last_group_code": "",
            }
            self.runs.insert_one(previous)
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"status": "running", "started_at": utc_now()},
             "$inc": {"attempts": 1}, "$unset": {"error": ""}},
        )
        return previous, fingerprints

    def _load_sources(self):
        recognized: dict[str, dict[str, Any]] = {}
        rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        profiles: dict[str, dict[str, Any]] = {}
        invalid = defaultdict(int)
        for item in self.db[self.recognized_collection].find({}):
            code = group_code(item.get("codigo_grupo"))
            if not code:
                invalid["recognized_invalid_group_code"] += 1
            elif code in recognized:
                invalid["recognized_duplicate_group_code"] += 1
            else:
                recognized[code] = item
        for item in self.db[self.open_data_collection].find({}):
            code = group_code(item.get("cod_grupo_gr"))
            if not code:
                invalid["open_data_invalid_group_code"] += 1
            else:
                rows[code].append(item)
        for item in self.db[self.gruplac_collection].find({}, FINGERPRINT_PROJECTIONS["gruplac"]):
            code = group_code(item.get("group_code") or item.get("_id"))
            if not code:
                invalid["gruplac_invalid_group_code"] += 1
            elif (item.get("parser") or {}).get("version") != REQUIRED_GRUPLAC_PARSER_VERSION:
                invalid["gruplac_parser_version_mismatch"] += 1
            elif code in profiles:
                invalid["gruplac_duplicate_group_code"] += 1
            else:
                profiles[code] = item
        return recognized, rows, profiles, dict(invalid)

    def _audit(
        self,
        codes: list[str],
        source_issues: dict[str, int],
        fingerprints: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        output = self.db[self.temp_collection]
        actual_ids: set[str] = set()
        checks = defaultdict(int, source_issues)
        examples: dict[str, list[str]] = defaultdict(list)
        relations = addresses = ranked = profiled = 0
        for item in output.find({}):
            code = str(item.get("_id") or "")
            actual_ids.add(code)
            unknown = set(item) - ({"_id"} | AFFILIATION_FIELDS)
            group_ids = {
                ext.get("id") for ext in item.get("external_ids", [])
                if ext.get("source") == "minciencias"
            }
            validations = {
                "invalid_output_group_code": not GROUP_CODE_RE.fullmatch(code),
                "missing_name": not item.get("names"),
                "missing_group_type": not any(
                    value.get("type") == "group" for value in item.get("types", [])
                ),
                "external_group_id_mismatch": code not in group_ids,
                "unexpected_destination_fields": bool(unknown),
                "invalid_relation": any(
                    not clean_text(value.get("name"))
                    or clean_text(value.get("id"))
                    for value in item.get("relations", [])
                ),
            }
            for name, failed in validations.items():
                if failed:
                    checks[name] += 1
                    if len(examples[name]) < 20:
                        examples[name].append(code)
            relations += int(bool(item.get("relations")))
            addresses += int(bool(item.get("addresses")))
            ranked += int(bool(item.get("ranking")))
            profiled += int(any(
                value.get("source") == "scienti" for value in item.get("updated", [])
            ))
        expected_ids = set(codes)
        checks["missing_source_groups"] += len(expected_ids - actual_ids)
        checks["unexpected_output_groups"] += len(actual_ids - expected_ids)
        checks["document_count_mismatch"] += int(len(actual_ids) != len(expected_ids))
        if self.expected_groups:
            checks["expected_group_count_mismatch"] += int(
                len(expected_ids) != self.expected_groups
            )
        ending_fingerprints = self._fingerprints()
        checks["source_changed_during_materialization"] += int(
            ending_fingerprints != fingerprints
        )
        critical = sum(checks.values())
        audit = {
            "_id": f"{self.run_name}_audit",
            "run_name": self.run_name,
            "status": "passed" if not critical else "failed",
            "audited_at": utc_now(),
            "collection": self.target_collection,
            "documents": len(actual_ids),
            "source_group_union": len(expected_ids),
            "source_fingerprints": deepcopy(fingerprints),
            "critical_counts": dict(sorted(checks.items())),
            "critical_anomalies": critical,
            "examples": dict(examples),
            "coverage": {
                "with_relations": relations, "with_addresses": addresses,
                "with_ranking": ranked, "with_gruplac_profile": profiled,
            },
        }
        self.db[AFFILIATION_AUDITS].replace_one(
            {"_id": audit["_id"]}, audit, upsert=True
        )
        return audit

    def _publish(self, summary: dict[str, Any], audit: dict[str, Any]) -> None:
        publications = self.db[AFFILIATION_PUBLICATIONS]
        current = publications.find_one({"_id": "current"}) or {}
        publication = {
            "_id": self.run_name, "status": "published",
            "published_at": utc_now(), "audit": audit["_id"],
            "collection": self.target_collection,
            "documents": summary["documents"],
            "source_fingerprints": deepcopy(audit["source_fingerprints"]),
        }
        publications.replace_one({"_id": self.run_name}, publication, upsert=True)
        publications.replace_one(
            {"_id": "current"},
            {"_id": "current", "current_run": self.run_name,
             "previous_run": clean_text(current.get("current_run")),
             "collection": self.target_collection, "audit": audit["_id"],
             "published_at": publication["published_at"]},
            upsert=True,
        )

    def run(self) -> dict[str, Any]:
        previous, fingerprints = self._prepare()
        if previous.get("status") == "complete":
            if self.target_collection not in self.db.list_collection_names():
                raise RuntimeError("completed affiliation target is missing")
            summary = deepcopy(previous.get("summary") or {})
            if self.db[self.target_collection].count_documents({}) != summary.get("documents"):
                raise RuntimeError("completed affiliation target count changed")
            return summary
        try:
            recognized, rows, profiles, source_issues = self._load_sources()
            codes = sorted(set(recognized) | set(rows))
            last_group = clean_text(previous.get("last_group_code"))
            built_at = previous.get("created_at") or utc_now()
            batch: list[dict[str, Any]] = []
            processed = 0
            for position, code in enumerate(codes, start=1):
                if last_group and code <= last_group:
                    continue
                batch.append(build_group_affiliation(
                    code, recognized.get(code, {}), rows.get(code, []),
                    profiles.get(code, {}), built_at=built_at,
                ))
                if len(batch) >= self.batch_size or position == len(codes):
                    _bulk_replace(self.db[self.temp_collection], batch)
                    checkpoint = batch[-1]["_id"]
                    processed += len(batch)
                    batch.clear()
                    self.runs.update_one(
                        {"_id": self.run_name},
                        {"$set": {"last_group_code": checkpoint,
                                  "last_progress_at": utc_now()}},
                    )
                if position % self.progress_every == 0:
                    print(
                        f"INFO: affiliation materialization {position}/{len(codes)} "
                        f"last={code}", flush=True,
                    )
            audit = self._audit(codes, source_issues, fingerprints)
            if audit["critical_anomalies"]:
                raise RuntimeError(
                    f"affiliation materialization audit failed: {audit['critical_counts']}"
                )
            target = self.db[self.temp_collection]
            target.create_index("external_ids.id")
            target.create_index("names.name")
            target.create_index("types.type")
            target.rename(self.target_collection, dropTarget=False)
            summary = {
                "status": "complete", "collection": self.target_collection,
                "documents": len(codes), "processed_this_attempt": processed,
                "audit": audit["_id"], "coverage": deepcopy(audit["coverage"]),
                "finished_at": utc_now(),
            }
            self.runs.update_one(
                {"_id": self.run_name},
                {"$set": {"status": "complete", "summary": summary,
                          "finished_at": summary["finished_at"]}},
            )
            summary = deepcopy(
                (self.runs.find_one({"_id": self.run_name}) or {}).get("summary")
                or summary
            )
            self._publish(summary, audit)
            return summary
        except Exception as error:
            self.runs.update_one(
                {"_id": self.run_name},
                {"$set": {"status": "failed", "failed_at": utc_now(),
                          "error": f"{type(error).__name__}: {error}"}},
            )
            raise
