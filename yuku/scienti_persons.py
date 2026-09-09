"""Audited, resumable materialization of public ScienTI people for Kahi."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import calendar
import json
import re
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse

from pymongo import ASCENDING, ReplaceOne, UpdateOne

from yuku.cvlac_related_works import normalize_doi
from yuku.scienti_affiliations import collection_fingerprint


PERSON_RUNS = "scienti_person_materialization_runs"
PERSON_AUDITS = "scienti_person_materialization_audits"
PERSON_PUBLICATIONS = "scienti_person_publications"
CVLAC_RUNS = "scienti_cvlac_priority_runs"
CVLAC_AUDITS = "scienti_cvlac_normalization_audits"
GRUPLAC_AUDITS = "scienti_gruplac_normalization_audits"
AFFILIATION_PUBLICATIONS = "scienti_affiliation_publications"
AFFILIATION_AUDITS = "scienti_affiliation_materialization_audits"
FINAL_RELEASES = "scienti_final_release_publications"
FINAL_RELEASE_AUDITS = "scienti_final_release_audits"
MATERIALIZER_VERSION = "scienti-persons-v2"
REQUIRED_CVLAC_PARSER_VERSION = "3.2.0"
REQUIRED_GRUPLAC_PARSER_VERSION = "3.2.0"
COD_RH_RE = re.compile(r"\d{10}")
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
COLLECTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,119}")
ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", re.I)
CANONICAL_DOI_RE = re.compile(
    r"^https://doi\.org/10\.\d{4,9}/[^\s<>\"{}|\\^`\[\]]+$", re.I
)
PERSON_FIELDS = {
    "updated", "full_name", "first_names", "last_names", "initials",
    "aliases", "affiliations", "keywords", "external_ids", "sex",
    "marital_status", "ranking", "birthplace", "birthdate", "degrees",
    "subjects", "citations_count", "products_count", "related_works",
}
MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
    "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12,
}
SOURCE_PROJECTIONS = {
    "directory": {
        "cod_rh": 1, "nombre_completo": 1, "nivel_formacion": 1,
        "nacionalidad": 1,
    },
    "recognized": {
        "cod_rh": 1, "nombre_completo": 1, "categorias_historicas": 1,
        "consulta_scienti.coincidencias": 1,
    },
    "open_data": {
        "id_persona_pr": 1, "ano_convo": 1, "id_clas_pr": 1,
        "nme_clasificacion_pr": 1, "orden_clas_pr": 1,
        "id_area_con_pr": 1, "nme_gran_area_pr": 1, "nme_area_pr": 1,
        "nme_esp_area_pr": 1, "nme_genero_pr": 1,
        "nme_niv_form_pr": 1, "nme_pais_nac_pr": 1,
        "nme_departamento_nac_pr": 1, "nme_municipio_nac_pr": 1,
    },
    "cvlac": {
        "id_persona_pr": 1, "profile_name": 1, "profile": 1,
        "profile_status": 1, "parser.version": 1,
        "source.content_sha256": 1, "source.fetched_at": 1,
    },
    "gruplac": {
        "group_code": 1, "group_name": 1, "members": 1,
        "parser.version": 1, "source.content_sha256": 1,
    },
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").strip().split())


def normalized_text(value: Any) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", clean_text(value))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def cod_rh(value: Any) -> str:
    value = clean_text(value)
    return value if COD_RH_RE.fullmatch(value) and value != "0000000000" else ""


def canonical_doi(value: Any) -> str:
    value = clean_text(value)
    if not value:
        return ""
    value = normalize_doi(value)
    return value if CANONICAL_DOI_RE.fullmatch(value) else ""


def source_time(value: Any, fallback: datetime) -> int:
    if isinstance(value, datetime):
        return int(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp())
    value = clean_text(value)
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp())
        except ValueError:
            pass
    return int(fallback.timestamp())


def clean_person_name(value: Any) -> str:
    value = re.sub(r"\s*\.\s*", " ", clean_text(value))
    return " ".join(part.capitalize() if part.isupper() else part for part in value.split())


def split_person_name(value: Any) -> tuple[list[str], list[str], str]:
    words = clean_person_name(value).split()
    if len(words) < 2:
        return words, [], "".join(item[:1].upper() for item in words)
    if len(words) == 2:
        first, last = words[:1], words[1:]
    elif len(words) == 3:
        first, last = words[:1], words[1:]
    else:
        first, last = words[:-2], words[-2:]
    return first, last, "".join(item[:1].upper() for item in first)


def _valid_orcid_checksum(value: str) -> bool:
    total = 0
    for character in value.replace("-", "")[:15]:
        total = (total + int(character)) * 2
    result = (12 - total % 11) % 11
    expected = "X" if result == 10 else str(result)
    return value[-1].upper() == expected


def profile_external_id(item: dict[str, Any]) -> dict[str, Any] | None:
    """Return only validated public identifiers in the established Kahi shape."""
    label = normalized_text(item.get("label"))
    url = clean_text(item.get("url"))
    parsed = urlparse(url)
    host = parsed.hostname.casefold() if parsed.hostname else ""
    if "orcid" in label or host.endswith("orcid.org"):
        match = ORCID_RE.search(url.upper())
        if not match or not _valid_orcid_checksum(match.group()):
            return None
        source = "orcid"
        url = "https://orcid.org/" + match.group().upper()
    elif "scopus" in label or "scopus.com" in host:
        identifier = (parse_qs(parsed.query).get("authorId") or [""])[0]
        if not re.fullmatch(r"\d{8,12}", identifier) or set(identifier) == {"0"}:
            return None
        source = "scopus"
        url = "https://www.scopus.com/authid/detail.uri?authorId=" + identifier
    elif "scholar" in label or "scholar.google" in host:
        identifier = (parse_qs(parsed.query).get("user") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,20}", identifier):
            return None
        source = "scholar"
        url = "https://scholar.google.com/citations?user=" + identifier
    elif "researchgate" in label or "researchgate.net" in host:
        source = "researchgate"
    elif "linkedin" in label or "linkedin.com" in host:
        source = "linkedin"
    else:
        return None
    if not parsed.scheme.startswith("http"):
        return None
    return {"provenance": "scienti", "source": source, "id": url}


def normalize_sex(value: Any) -> str:
    value = normalized_text(value)
    if value in {"masculino", "hombre", "male"}:
        return "Hombre"
    if value in {"femenino", "mujer", "female"}:
        return "Mujer"
    if value in {"intersexual", "intersex"}:
        return "Intersexual"
    return ""


def _epoch(year: int, month: int = 1, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def parse_membership_period(value: Any) -> tuple[int, int]:
    value = clean_text(value)
    match = re.search(
        r"(?P<sy>\d{4})(?:/(?P<sm>\d{1,2}))?\s*-\s*"
        r"(?:(?P<actual>Actual)|(?P<ey>\d{4})(?:/(?P<em>\d{1,2}))?)",
        value,
        flags=re.I,
    )
    if not match:
        return -1, -1
    start = _epoch(int(match.group("sy")), int(match.group("sm") or 1))
    if match.group("actual"):
        return start, -1
    end_year = int(match.group("ey"))
    end_month = int(match.group("em") or 12)
    end = _epoch(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    return start, end


def parse_profile_period(value: Any) -> tuple[int, int]:
    """Parse the explicit Spanish month/year range printed by public CvLAC."""
    value = normalized_text(value)
    dates = [
        (MONTHS[month], int(year))
        for month, year in re.findall(
            r"(" + "|".join(MONTHS) + r")\s+de\s+(\d{4})", value
        )
    ]
    if not dates:
        return -1, -1
    start_month, start_year = dates[0]
    start = _epoch(start_year, start_month)
    if "actual" in value:
        return start, -1
    if len(dates) < 2:
        return start, -1
    end_month, end_year = dates[1]
    return start, _epoch(
        end_year, end_month, calendar.monthrange(end_year, end_month)[1]
    )


def empty_person(code: str) -> dict[str, Any]:
    return {
        "_id": code, "updated": [], "full_name": "", "first_names": [],
        "last_names": [], "initials": "", "aliases": [], "affiliations": [],
        "keywords": [], "external_ids": [{
            "provenance": "minciencias", "source": "scienti",
            "id": {"COD_RH": code},
        }],
        "sex": "", "marital_status": None, "ranking": [], "birthplace": {},
        "birthdate": -1, "degrees": [], "subjects": [],
        "citations_count": [], "products_count": 0, "related_works": [],
    }


def _subject(level: int, name: Any, identifier: Any = "") -> dict[str, Any] | None:
    name = clean_text(name)
    if not name or normalized_text(name) in {"no registra", "no disponible"}:
        return None
    identifier = clean_text(identifier)
    return {
        "level": level, "name": name, "id": "",
        "external_ids": ([{"source": "OECD", "id": identifier}] if identifier else []),
    }


def open_data_subject(row: dict[str, Any]) -> dict[str, Any] | None:
    identifier = clean_text(row.get("id_area_con_pr"))
    values = [
        _subject(0, row.get("nme_gran_area_pr"), identifier[:1]),
        _subject(1, row.get("nme_area_pr"), identifier),
        _subject(2, row.get("nme_esp_area_pr")),
    ]
    values = [item for item in values if item]
    return (
        {"provenance": "minciencias", "source": "OECD", "subjects": values}
        if values else None
    )


def degree(value: Any) -> dict[str, Any] | None:
    value = clean_text(value)
    if not value or normalized_text(value) in {"no registra", "no disponible"}:
        return None
    return {
        "provenance": "minciencias", "source": "nivel_academico",
        "degree": value, "id": "", "date": -1, "institutions": [],
    }


def birthplace(row: dict[str, Any]) -> dict[str, str]:
    output = {
        "country": clean_text(row.get("nme_pais_nac_pr")),
        "state": clean_text(row.get("nme_departamento_nac_pr")),
        "city": clean_text(row.get("nme_municipio_nac_pr")),
    }
    return {
        key: value for key, value in output.items()
        if value and normalized_text(value) not in {"no registra", "no disponible"}
    }


def profile_degree(item: dict[str, Any]) -> dict[str, Any] | None:
    output = degree(item.get("level"))
    if output:
        output["provenance"] = "scienti"
        output["source"] = "scienti"
    return output


def experience_affiliation(item: dict[str, Any]) -> dict[str, Any] | None:
    name = clean_text(item.get("institution"))
    if not name:
        return None
    start, end = parse_profile_period(item.get("period"))
    return {
        "id": "", "name": name, "types": [], "start_date": start,
        "end_date": end, "position": "",
    }


def group_affiliation(group: dict[str, Any], member: dict[str, Any]) -> dict[str, Any] | None:
    code = clean_text(group.get("group_code") or group.get("_id")).upper()
    if not re.fullmatch(r"COL\d{7}", code):
        return None
    start, end = parse_membership_period(member.get("period"))
    return {
        "id": code, "name": clean_text(group.get("group_name")),
        "types": [{"source": "scienti", "type": "group"}],
        "start_date": start, "end_date": end,
        "position": clean_text(member.get("role")),
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str,
        separators=(",", ":"),
    ).encode("utf-8")


def _bulk_updates(collection, values: Iterable[tuple[dict, dict, bool]]) -> None:
    values = list(values)
    if not values:
        return
    operations = [UpdateOne(query, update, upsert=upsert) for query, update, upsert in values]
    try:
        collection.bulk_write(operations, ordered=False)
    except TypeError:  # Compatibility with older mongomock/PyMongo adapters.
        for query, update, upsert in values:
            collection.update_one(query, update, upsert=upsert)


def _bulk_replace(collection, documents: Iterable[dict[str, Any]]) -> None:
    documents = list(documents)
    if not documents:
        return
    operations = [ReplaceOne({"_id": item["_id"]}, item, upsert=True) for item in documents]
    try:
        collection.bulk_write(operations, ordered=False)
    except TypeError:
        for item in documents:
            collection.replace_one({"_id": item["_id"]}, item, upsert=True)


def _add_to_set(**fields: list[Any]) -> dict[str, Any]:
    return {
        key: {"$each": [item for item in values if item]}
        for key, values in fields.items() if any(values)
    }


def _deduplicate(values: list[Any], key: Callable[[Any], Any]) -> list[Any]:
    output = []
    seen = set()
    for item in values:
        identity = key(item)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(item)
    return output


def normalize_person(document: dict[str, Any]) -> dict[str, Any]:
    """Remove staging variation and enforce the existing Kahi person schema."""
    code = cod_rh(document.get("_id"))
    output = empty_person(code)
    for field in PERSON_FIELDS:
        if field in document:
            output[field] = deepcopy(document[field])
    output["full_name"] = clean_person_name(output["full_name"])
    output["aliases"] = _deduplicate(
        [clean_person_name(item) for item in output["aliases"] if clean_text(item)],
        normalized_text,
    )
    output["aliases"] = [
        item for item in output["aliases"]
        if normalized_text(item) != normalized_text(output["full_name"])
    ]
    if not output["full_name"] and output["aliases"]:
        output["full_name"] = output["aliases"].pop(0)
    output["external_ids"] = _deduplicate(
        output["external_ids"],
        lambda item: (item.get("source"), repr(item.get("id"))),
    )
    output["ranking"] = _deduplicate(
        output["ranking"],
        lambda item: (item.get("source"), item.get("rank"), item.get("date")),
    )
    output["subjects"] = _deduplicate(output["subjects"], repr)
    output["degrees"] = _deduplicate(
        output["degrees"], lambda item: (item.get("source"), normalized_text(item.get("degree")))
    )
    output["affiliations"] = _deduplicate(
        output["affiliations"],
        lambda item: (
            item.get("id") or normalized_text(item.get("name")),
            item.get("start_date"), item.get("end_date"), item.get("position"),
        ),
    )
    output["updated"] = sorted(
        ({"source": source, "time": max(
            int(item.get("time") or 0) for item in output["updated"]
            if item.get("source") == source
        )} for source in {item.get("source") for item in output["updated"] if item.get("source")}),
        key=lambda item: item["source"],
    )
    related = []
    for item in output["related_works"]:
        doi = canonical_doi(item.get("id"))
        if item.get("source") != "doi" or not doi:
            continue
        value = {"provenance": "minciencias", "source": "doi", "id": doi}
        if int(item.get("author_count") or 0) > 0:
            value["author_count"] = int(item["author_count"])
        related.append(value)
    output["related_works"] = sorted(
        _deduplicate(related, lambda item: item["id"]), key=lambda item: item["id"]
    )
    return output


class ScientiPersonMaterializer:
    """Build and atomically publish a complete DOI-only ScienTI person snapshot."""

    def __init__(
        self,
        db,
        *,
        run_name: str,
        manifest_run_name: str,
        manifest_collection: str,
        directory_collection: str,
        recognized_collection: str,
        open_data_collection: str,
        cvlac_collection: str,
        cvlac_audit_name: str,
        gruplac_collection: str,
        gruplac_audit_name: str,
        affiliation_run_name: str,
        affiliation_collection: str,
        final_release_name: str,
        final_release_audit_name: str,
        target_collection: str,
        batch_size: int = 500,
        progress_every: int = 10000,
        expected_people: int = 0,
    ):
        if not NAME_RE.fullmatch(run_name or ""):
            raise ValueError("invalid person materialization run name")
        collection_values = (
            manifest_collection, directory_collection, recognized_collection,
            open_data_collection, cvlac_collection, gruplac_collection,
            affiliation_collection, target_collection,
        )
        if any(
            not COLLECTION_RE.fullmatch(value or "") or value.startswith("system.")
            for value in collection_values
        ):
            raise ValueError("invalid person materialization collection")
        if target_collection in set(collection_values[:-1]):
            raise ValueError("person target must differ from every source")
        if batch_size < 1 or progress_every < 1 or expected_people < 0:
            raise ValueError("invalid person materialization limits")
        self.db = db
        self.run_name = run_name
        self.manifest_run_name = manifest_run_name
        self.manifest_collection = manifest_collection
        self.directory_collection = directory_collection
        self.recognized_collection = recognized_collection
        self.open_data_collection = open_data_collection
        self.cvlac_collection = cvlac_collection
        self.cvlac_audit_name = cvlac_audit_name
        self.gruplac_collection = gruplac_collection
        self.gruplac_audit_name = gruplac_audit_name
        self.affiliation_run_name = affiliation_run_name
        self.affiliation_collection = affiliation_collection
        self.final_release_name = final_release_name
        self.final_release_audit_name = final_release_audit_name
        self.target_collection = target_collection
        self.temp_collection = f"__yuku_{run_name}_persons"
        self.edge_collection = f"__yuku_{run_name}_doi_edges"
        self.batch_size = batch_size
        self.progress_every = progress_every
        self.expected_people = expected_people
        self.runs = db[PERSON_RUNS]
        self.release_collections: dict[str, str] = {}
        self.release_expected_documents: dict[str, int] = {}
        self.config = {
            "materializer_version": MATERIALIZER_VERSION,
            "manifest_run_name": manifest_run_name,
            "manifest_collection": manifest_collection,
            "directory_collection": directory_collection,
            "recognized_collection": recognized_collection,
            "open_data_collection": open_data_collection,
            "cvlac_collection": cvlac_collection,
            "cvlac_audit_name": cvlac_audit_name,
            "gruplac_collection": gruplac_collection,
            "gruplac_audit_name": gruplac_audit_name,
            "affiliation_run_name": affiliation_run_name,
            "affiliation_collection": affiliation_collection,
            "final_release_name": final_release_name,
            "final_release_audit_name": final_release_audit_name,
            "target_collection": target_collection,
            "expected_people": expected_people,
        }
        self.config_hash = sha256(_canonical_json(self.config)).hexdigest()

    def _validate_audit(
        self, collection: str, audit_name: str, destination: str, parser: str
    ) -> None:
        audit = self.db[collection].find_one({"_id": audit_name}) or {}
        summary = audit.get("summary") or {}
        config = audit.get("config") or {}
        if audit.get("status") != "passed" or int(
            audit.get("critical_anomalies") or summary.get("critical_anomalies") or 0
        ):
            raise RuntimeError(f"source audit {audit_name!r} is not passed")
        if config.get("normalized_collection") not in {None, destination} and config.get(
            "destination_collection"
        ) != destination:
            raise RuntimeError(f"source audit {audit_name!r} proves another collection")
        audited_destination = config.get("normalized_collection") or config.get(
            "destination_collection"
        )
        if audited_destination != destination or config.get("parser_version") not in {
            None, parser
        } or config.get("expected_parser_version") not in {None, parser}:
            raise RuntimeError(f"source audit {audit_name!r} is incompatible")
        versions = summary.get("parser_version_counts") or {}
        if versions and set(versions) != {parser}:
            raise RuntimeError(f"source audit {audit_name!r} used another parser")

    def _validate_sources(self) -> dict[str, Any]:
        required = {
            self.manifest_collection, self.directory_collection,
            self.recognized_collection, self.open_data_collection,
            self.cvlac_collection, self.gruplac_collection,
            self.affiliation_collection,
        }
        missing = sorted(required - set(self.db.list_collection_names()))
        if missing:
            raise RuntimeError(f"person source collections are missing: {missing}")
        manifest_run = self.db[CVLAC_RUNS].find_one({"_id": self.manifest_run_name}) or {}
        if (
            manifest_run.get("status") != "complete"
            or manifest_run.get("manifest_status") != "ready"
            or (manifest_run.get("config") or {}).get("manifest_collection")
            != self.manifest_collection
        ):
            raise RuntimeError("person manifest is not a completed frozen snapshot")
        self._validate_audit(
            CVLAC_AUDITS, self.cvlac_audit_name, self.cvlac_collection,
            REQUIRED_CVLAC_PARSER_VERSION,
        )
        self._validate_audit(
            GRUPLAC_AUDITS, self.gruplac_audit_name, self.gruplac_collection,
            REQUIRED_GRUPLAC_PARSER_VERSION,
        )
        affiliation = self.db[AFFILIATION_PUBLICATIONS].find_one(
            {"_id": self.affiliation_run_name}
        ) or {}
        affiliation_audit = self.db[AFFILIATION_AUDITS].find_one(
            {"_id": affiliation.get("audit")}
        ) or {}
        if (
            affiliation.get("status") != "published"
            or affiliation.get("collection") != self.affiliation_collection
            or affiliation_audit.get("status") != "passed"
            or int(affiliation_audit.get("critical_anomalies") or 0)
            or affiliation_audit.get("collection") != self.affiliation_collection
            or int(affiliation.get("documents") or 0)
            != int(affiliation_audit.get("documents") or -1)
        ):
            raise RuntimeError("affiliation snapshot is not proven and published")
        release = self.db[FINAL_RELEASES].find_one({"_id": self.final_release_name}) or {}
        release_audit = self.db[FINAL_RELEASE_AUDITS].find_one(
            {"_id": self.final_release_audit_name}
        ) or {}
        collections = release.get("collections") or {}
        if (
            release.get("status") != "published"
            or release.get("audit") != self.final_release_audit_name
            or release_audit.get("status") != "passed"
            or release_audit.get("release_name") not in {
                None, self.final_release_name
            }
            or int(release_audit.get("critical_anomalies") or 0)
            or release_audit.get("collections") != collections
            or set(collections) != {"works", "projects", "patents", "events"}
        ):
            raise RuntimeError("final entity release is not proven and published")
        invalid_collections = [
            value for value in collections.values()
            if not COLLECTION_RE.fullmatch(value or "") or value.startswith("system.")
        ]
        missing_release = sorted(
            set(collections.values()) - set(self.db.list_collection_names())
        )
        if invalid_collections or missing_release:
            raise RuntimeError("final entity release collections are invalid or missing")
        evidence = release_audit.get("evidence") or {}
        expected_documents: dict[str, int] = {}
        for entity, collection_name in collections.items():
            entity_evidence = evidence.get(entity) or {}
            if (
                entity_evidence.get("collection") != collection_name
                or int(entity_evidence.get("documents") or 0) < 1
            ):
                raise RuntimeError("final entity audit proves another collection")
            expected_documents[entity] = int(entity_evidence.get("documents") or 0)
        self.release_collections = collections
        self.release_expected_documents = expected_documents
        return manifest_run

    def _proofs(self, manifest_run: dict[str, Any]) -> dict[str, Any]:
        release_documents = {
            entity: self.db[name].count_documents({})
            for entity, name in self.release_collections.items()
        }
        if release_documents != self.release_expected_documents:
            raise RuntimeError("final entity release counts changed after its audit")
        manifest_query = {"run_name": self.manifest_run_name}
        return {
            "manifest": {
                "documents": self.db[self.manifest_collection].count_documents(
                    manifest_query
                ),
                "sha256": manifest_run.get("manifest_sha256"),
            },
            "directory": collection_fingerprint(
                self.db[self.directory_collection], SOURCE_PROJECTIONS["directory"]
            ),
            "recognized": collection_fingerprint(
                self.db[self.recognized_collection], SOURCE_PROJECTIONS["recognized"]
            ),
            "open_data": collection_fingerprint(
                self.db[self.open_data_collection], SOURCE_PROJECTIONS["open_data"]
            ),
            "cvlac": collection_fingerprint(
                self.db[self.cvlac_collection], SOURCE_PROJECTIONS["cvlac"]
            ),
            "gruplac": collection_fingerprint(
                self.db[self.gruplac_collection], SOURCE_PROJECTIONS["gruplac"]
            ),
            "affiliations": {
                "documents": self.db[self.affiliation_collection].count_documents({}),
                "run": self.affiliation_run_name,
            },
            "release": {
                "name": self.final_release_name,
                "audit": self.final_release_audit_name,
                "collections": deepcopy(self.release_collections),
                "documents": release_documents,
                "expected_documents": deepcopy(self.release_expected_documents),
            },
        }

    def _prepare(self) -> tuple[dict[str, Any], dict[str, Any]]:
        manifest_run = self._validate_sources()
        proofs = self._proofs(manifest_run)
        previous = self.runs.find_one({"_id": self.run_name})
        if previous and previous.get("config_hash") != self.config_hash:
            raise ValueError("person run exists with another configuration")
        if previous and previous.get("source_proofs") != proofs:
            raise RuntimeError("person sources changed since the run started")
        if previous and previous.get("status") == "complete":
            return previous, proofs
        if not previous:
            if self.target_collection in self.db.list_collection_names():
                raise RuntimeError("immutable person target already exists")
            self.db[self.temp_collection].drop()
            self.db[self.edge_collection].drop()
            previous = {
                "_id": self.run_name, "status": "pending",
                "config": deepcopy(self.config), "config_hash": self.config_hash,
                "source_proofs": deepcopy(proofs), "created_at": utc_now(),
                "stages": {},
            }
            self.runs.insert_one(previous)
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"status": "running", "started_at": utc_now()},
             "$inc": {"attempts": 1}, "$unset": {"error": ""}},
        )
        return previous, proofs

    def _manifest(self) -> set[str]:
        source_run = self.db[CVLAC_RUNS].find_one(
            {"_id": self.manifest_run_name}
        ) or {}
        digest = sha256()
        people = set()
        documents = 0
        query = {"run_name": self.manifest_run_name}
        for item in self.db[self.manifest_collection].find(
            query, {"_id": 1, "segment": 1, "sources": 1}
        ).sort("_id", ASCENDING):
            code = clean_text(item.get("_id"))
            labels = sorted(clean_text(value) for value in item.get("sources", []))
            digest.update(
                f"{code}|{clean_text(item.get('segment'))}|{','.join(labels)}\n".encode(
                    "utf-8"
                )
            )
            valid_code = cod_rh(code)
            if valid_code:
                people.add(valid_code)
            documents += 1
        if (
            documents != int(source_run.get("target_count") or 0)
            or digest.hexdigest() != source_run.get("manifest_sha256")
        ):
            raise RuntimeError("person manifest count or SHA-256 does not match its run")
        return people

    def _identity_universe(self, manifest: set[str]) -> set[str]:
        """Add valid people evidenced by the audited GrupLAC snapshot."""
        people = set(manifest)
        for item in self.db[self.gruplac_collection].find({}, {"members.cod_rh": 1}):
            for member in item.get("members", []) or []:
                code = cod_rh(member.get("cod_rh"))
                if code:
                    people.add(code)
        return people

    def _stage(self, name: str) -> dict[str, Any]:
        run = self.runs.find_one({"_id": self.run_name}, {f"stages.{name}": 1}) or {}
        return ((run.get("stages") or {}).get(name) or {})

    def _complete_stage(self, name: str, summary: dict[str, Any]) -> None:
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {f"stages.{name}": {
                "status": "complete", "summary": summary, "finished_at": utc_now(),
            }}},
        )

    def _seed(self, people: set[str]) -> None:
        name = "seed"
        if self._stage(name).get("status") == "complete":
            return
        stage = self._stage(name)
        last = stage.get("last_id")
        batch = []
        processed = int(stage.get("processed") or 0)
        for code in sorted(value for value in people if last is None or value > last):
            batch.append(empty_person(code))
            if len(batch) >= self.batch_size:
                _bulk_replace(self.db[self.temp_collection], batch)
                processed += len(batch)
                last = code
                batch.clear()
                self.runs.update_one({"_id": self.run_name}, {"$set": {
                    f"stages.{name}": {"status": "running", "last_id": last,
                                       "processed": processed}
                }})
        if batch:
            _bulk_replace(self.db[self.temp_collection], batch)
            processed += len(batch)
        if processed != len(people):
            raise RuntimeError(f"person seed mismatch: {processed}/{len(people)}")
        self._complete_stage(name, {"people": processed})

    def _enrich(
        self,
        name: str,
        collection_name: str,
        projection: dict[str, int],
        manifest: set[str],
        transform: Callable[[dict[str, Any]], list[tuple[str, dict[str, Any]]]],
    ) -> None:
        if self._stage(name).get("status") == "complete":
            return
        stage = self._stage(name)
        last = stage.get("last_id")
        query = {"_id": {"$gt": last}} if last is not None else {}
        counters = defaultdict(int, (stage.get("counters") or {}))
        operations = []
        scanned_since_flush = 0
        for item in self.db[collection_name].find(query, projection).sort("_id", ASCENDING):
            counters["documents"] += 1
            scanned_since_flush += 1
            for code, update in transform(item):
                if not code:
                    counters["invalid_codes"] += 1
                elif code not in manifest:
                    counters["unresolved_codes"] += 1
                else:
                    operations.append(({"_id": code}, update, False))
                    counters["updates"] += 1
            if scanned_since_flush >= self.batch_size:
                _bulk_updates(self.db[self.temp_collection], operations)
                operations.clear()
                scanned_since_flush = 0
                self.runs.update_one({"_id": self.run_name}, {"$set": {
                    f"stages.{name}": {"status": "running", "last_id": item["_id"],
                                       "counters": dict(counters)}
                }})
                if counters["documents"] % self.progress_every == 0:
                    print(f"INFO: person {name} documents={counters['documents']}", flush=True)
        _bulk_updates(self.db[self.temp_collection], operations)
        self._complete_stage(name, dict(counters))

    def _directory_update(self, item: dict[str, Any], built_at: datetime):
        code = cod_rh(item.get("cod_rh"))
        name = clean_person_name(item.get("nombre_completo"))
        values = _add_to_set(
            aliases=[name], degrees=[degree(item.get("nivel_formacion"))],
            updated=[{"source": "minciencias", "time": int(built_at.timestamp())}],
        )
        update = {"$addToSet": values}
        if name:
            update["$set"] = {"full_name": name}
        return [(code, update)]

    def _recognized_update(self, item: dict[str, Any], built_at: datetime):
        code = cod_rh(item.get("cod_rh"))
        matches = [cod_rh(value) for value in (
            ((item.get("consulta_scienti") or {}).get("coincidencias") or [])
        )]
        matches = [value for value in matches if value]
        if not code and len(set(matches)) == 1:
            code = matches[0]
        if not code:
            return [("", {})]
        rankings = []
        for value in item.get("categorias_historicas", []) or []:
            rank = clean_text(value.get("categoria"))
            rank_year = int(value.get("año") or 0)
            if rank and 1900 <= rank_year <= 2100:
                rankings.append({
                    "source": "minciencias", "rank": rank,
                    "date": _epoch(rank_year),
                })
        name = clean_person_name(item.get("nombre_completo"))
        values = _add_to_set(
            aliases=[name], ranking=rankings,
            updated=[{"source": "minciencias", "time": int(built_at.timestamp())}],
        )
        update = {"$addToSet": values}
        if name:
            update["$set"] = {"full_name": name}
        return [(code, update)]

    def _open_update(
        self, item: dict[str, Any], built_at: datetime,
        latest: dict[str, int],
    ):
        code = cod_rh(item.get("id_persona_pr"))
        value_time = source_time(item.get("ano_convo"), built_at)
        rank = clean_text(item.get("nme_clasificacion_pr"))
        ranking = ({
            "source": "minciencias", "rank": rank,
            "id": clean_text(item.get("id_clas_pr")),
            "order": clean_text(item.get("orden_clas_pr")).removesuffix(".0"),
            "date": value_time,
        } if rank and normalized_text(rank) != "no registra" else None)
        values = _add_to_set(
            ranking=[ranking], subjects=[open_data_subject(item)],
            degrees=[degree(item.get("nme_niv_form_pr"))],
            updated=[{"source": "minciencias", "time": value_time}],
        )
        update: dict[str, Any] = {"$addToSet": values}
        if code and value_time == latest.get(code):
            changes: dict[str, Any] = {}
            sex = normalize_sex(item.get("nme_genero_pr"))
            place = birthplace(item)
            if sex:
                changes["sex"] = sex
            if place:
                changes["birthplace"] = place
            if changes:
                update["$set"] = changes
        return [(code, update)]

    def _gruplac_updates(self, item: dict[str, Any], built_at: datetime):
        output = []
        for member in item.get("members", []) or []:
            code = cod_rh(member.get("cod_rh"))
            affiliation = group_affiliation(item, member)
            name = clean_person_name(member.get("full_name"))
            values = _add_to_set(
                aliases=[name], affiliations=[affiliation],
                updated=[{"source": "scienti", "time": int(built_at.timestamp())}],
            )
            update: dict[str, Any] = {"$addToSet": values}
            if name:
                first, last, initials = split_person_name(name)
                update["$set"] = {
                    "full_name": name, "first_names": first,
                    "last_names": last, "initials": initials,
                }
            output.append((code, update))
        return output

    def _cvlac_update(self, item: dict[str, Any], built_at: datetime):
        code = cod_rh(item.get("id_persona_pr") or item.get("_id"))
        profile = item.get("profile") or {}
        general = profile.get("general") or {}
        name = clean_person_name(general.get("name") or item.get("profile_name"))
        aliases = [name, clean_person_name(general.get("citation_name"))]
        identifiers = [profile_external_id(value) for value in profile.get("identifiers", [])]
        degrees = [profile_degree(value) for value in profile.get("degrees", [])]
        affiliations = [
            experience_affiliation(value) for value in profile.get("experiences", [])
        ]
        updated_time = source_time((item.get("source") or {}).get("fetched_at"), built_at)
        values = _add_to_set(
            aliases=aliases, external_ids=identifiers, degrees=degrees,
            affiliations=affiliations,
            updated=[{"source": "scienti", "time": updated_time}],
        )
        update: dict[str, Any] = {"$addToSet": values}
        changes: dict[str, Any] = {}
        if name:
            first, last, initials = split_person_name(name)
            changes.update({
                "full_name": name, "first_names": first,
                "last_names": last, "initials": initials,
            })
        sex = normalize_sex(general.get("sex"))
        if sex:
            changes["sex"] = sex
        if changes:
            update["$set"] = changes
        return [(code, update)]

    def _scan_references(
        self, entity: str, collection_name: str, manifest: set[str]
    ) -> None:
        stage_name = f"references_{entity}"
        if self._stage(stage_name).get("status") == "complete":
            return
        stage = self._stage(stage_name)
        last = stage.get("last_id")
        query = {"_id": {"$gt": last}} if last is not None else {}
        counters = defaultdict(int, (stage.get("counters") or {}))
        operations = []
        scanned_since_flush = 0
        projection = {"authors.id": 1}
        if entity == "works":
            projection.update({"authors.full_name": 1, "doi": 1, "author_count": 1})
        for item in self.db[collection_name].find(query, projection).sort("_id", ASCENDING):
            counters["documents"] += 1
            scanned_since_flush += 1
            doi_value = clean_text(item.get("doi")) if entity == "works" else ""
            doi = canonical_doi(doi_value)
            if doi_value and not doi:
                counters["invalid_dois"] += 1
            seen_people = set()
            for author in item.get("authors", []) or []:
                raw_code = clean_text(author.get("id"))
                if not raw_code:
                    continue
                counters["nonempty_references"] += 1
                code = cod_rh(raw_code)
                if not code:
                    counters["invalid_references"] += 1
                    continue
                if code not in manifest:
                    counters["unresolved_references"] += 1
                    continue
                if entity != "works" or not doi or code in seen_people:
                    continue
                seen_people.add(code)
                author_count = int(item.get("author_count") or len(item.get("authors", [])))
                edge_id = sha256(f"{code}|{doi}".encode("utf-8")).hexdigest()
                update = {
                    "$setOnInsert": {
                        "_id": edge_id, "person": code, "doi": doi,
                        "provenance": "minciencias", "source": "doi",
                    },
                    "$max": {"author_count": author_count},
                }
                author_name = clean_person_name(author.get("full_name"))
                if author_name:
                    update["$addToSet"] = {"names": author_name}
                operations.append(({"_id": edge_id}, update, True))
                counters["doi_edge_occurrences"] += 1
            if scanned_since_flush >= self.batch_size:
                _bulk_updates(self.db[self.edge_collection], operations)
                operations.clear()
                scanned_since_flush = 0
                self.runs.update_one({"_id": self.run_name}, {"$set": {
                    f"stages.{stage_name}": {
                        "status": "running", "last_id": item["_id"],
                        "counters": dict(counters),
                    }
                }})
                if counters["documents"] % self.progress_every == 0:
                    print(
                        f"INFO: person {stage_name} documents={counters['documents']}",
                        flush=True,
                    )
        _bulk_updates(self.db[self.edge_collection], operations)
        if entity == "works":
            self.db[self.edge_collection].create_index([
                ("person", ASCENDING), ("doi", ASCENDING)
            ])
            counters["unique_doi_edges"] = self.db[self.edge_collection].count_documents({})
        self._complete_stage(stage_name, dict(counters))

    def _attach_related_works(self) -> None:
        name = "attach_related_works"
        if self._stage(name).get("status") == "complete":
            return
        last = self._stage(name).get("last_person")
        query = {"person": {"$gt": last}} if last else {}
        cursor = self.db[self.edge_collection].find(query).sort([
            ("person", ASCENDING), ("doi", ASCENDING)
        ])
        operations = []
        current = ""
        related: list[dict[str, Any]] = []
        names: list[str] = []
        attached = 0

        def flush_person() -> None:
            nonlocal attached
            if not current:
                return
            update: dict[str, Any] = {"$set": {"related_works": deepcopy(related)}}
            if names:
                update["$addToSet"] = {"aliases": {"$each": deepcopy(names)}}
            operations.append(({"_id": current}, update, False))
            attached += len(related)

        for edge in cursor:
            person = edge["person"]
            if current and person != current:
                flush_person()
                if len(operations) >= self.batch_size:
                    _bulk_updates(self.db[self.temp_collection], operations)
                    operations.clear()
                    checkpoint = {
                        "status": "running",
                        "last_person": current,
                        "related_works": attached,
                    }
                    self.runs.update_one(
                        {"_id": self.run_name},
                        {"$set": {f"stages.{name}": checkpoint}},
                    )
                related = []
                names = []
            current = person
            value = {
                "provenance": "minciencias", "source": "doi", "id": edge["doi"],
            }
            if int(edge.get("author_count") or 0) > 0:
                value["author_count"] = int(edge["author_count"])
            related.append(value)
            for author_name in edge.get("names", []) or []:
                if author_name not in names:
                    names.append(author_name)
        flush_person()
        _bulk_updates(self.db[self.temp_collection], operations)
        self._complete_stage(name, {"related_works": attached})

    def _finalize(self) -> None:
        name = "finalize"
        if self._stage(name).get("status") == "complete":
            return
        stage = self._stage(name)
        last = stage.get("last_id")
        query = {"_id": {"$gt": last}} if last else {}
        batch = []
        processed = int(stage.get("processed") or 0)
        for item in self.db[self.temp_collection].find(query).sort("_id", ASCENDING):
            batch.append(normalize_person(item))
            if len(batch) >= self.batch_size:
                _bulk_replace(self.db[self.temp_collection], batch)
                processed += len(batch)
                last = item["_id"]
                batch.clear()
                self.runs.update_one({"_id": self.run_name}, {"$set": {
                    f"stages.{name}": {"status": "running", "last_id": last,
                                       "processed": processed}
                }})
        if batch:
            _bulk_replace(self.db[self.temp_collection], batch)
            processed += len(batch)
        self._complete_stage(name, {"people": processed})

    def _audit(
        self, people: set[str], manifest: set[str], proofs: dict[str, Any]
    ) -> dict[str, Any]:
        affiliation_ids = set(self.db[self.affiliation_collection].distinct("_id"))
        output_ids = set()
        checks = defaultdict(int)
        quality = defaultdict(int)
        related_count = 0
        for item in self.db[self.temp_collection].find({}):
            code = clean_text(item.get("_id"))
            output_ids.add(code)
            if set(item) != {"_id"} | PERSON_FIELDS:
                checks["unexpected_destination_fields"] += 1
            external_codes = {
                value.get("id", {}).get("COD_RH")
                for value in item.get("external_ids", [])
                if isinstance(value.get("id"), dict)
            }
            checks["invalid_person_id"] += int(not cod_rh(code))
            checks["external_cod_rh_mismatch"] += int(code not in external_codes)
            quality["missing_full_name"] += int(not clean_text(item.get("full_name")))
            quality["with_affiliations"] += int(bool(item.get("affiliations")))
            quality["with_external_identity"] += int(len(item.get("external_ids", [])) > 1)
            seen_dois = set()
            for related in item.get("related_works", []):
                related_count += 1
                doi = canonical_doi(related.get("id"))
                if (
                    related.get("source") != "doi" or not doi
                    or set(related) - {"provenance", "source", "id", "author_count"}
                ):
                    checks["invalid_related_work"] += 1
                if doi in seen_dois:
                    checks["duplicate_related_work"] += 1
                seen_dois.add(doi)
            for affiliation in item.get("affiliations", []):
                identifier = clean_text(affiliation.get("id"))
                if identifier.startswith("COL") and identifier not in affiliation_ids:
                    checks["unresolved_group_affiliation"] += 1
            for external_id in item.get("external_ids", []):
                if normalized_text(external_id.get("source")) in {
                    "cedula", "cedula de ciudadania", "passport", "pasaporte",
                }:
                    checks["sensitive_national_identifier"] += 1
        checks["missing_identity_people"] += len(people - output_ids)
        checks["unexpected_people"] += len(output_ids - people)
        checks["document_count_mismatch"] += int(len(output_ids) != len(people))
        checks["doi_edge_count_mismatch"] += int(
            related_count != self.db[self.edge_collection].count_documents({})
        )
        if self.expected_people:
            checks["expected_people_mismatch"] += int(len(people) != self.expected_people)
        for source in ("directory", "recognized", "open_data", "gruplac", "cvlac"):
            summary = self._stage(source).get("summary") or {}
            checks[f"{source}_unresolved_codes"] += int(
                summary.get("unresolved_codes") or 0
            )
        for entity in ("works", "projects", "patents", "events"):
            summary = (self._stage(f"references_{entity}").get("summary") or {})
            checks[f"{entity}_invalid_references"] += int(
                summary.get("invalid_references") or 0
            )
            checks[f"{entity}_unresolved_references"] += int(
                summary.get("unresolved_references") or 0
            )
            if entity == "works":
                quality["excluded_invalid_work_dois"] += int(
                    summary.get("invalid_dois") or 0
                )
            expected = int(self.release_expected_documents.get(entity) or 0)
            observed = int(proofs["release"]["documents"].get(entity) or 0)
            if expected:
                checks[f"{entity}_release_count_mismatch"] += int(
                    observed != expected
                )
        ending_proofs = self._proofs(self._validate_sources())
        checks["source_changed_during_materialization"] += int(ending_proofs != proofs)
        critical = sum(checks.values())
        quality["frozen_manifest_people"] = len(manifest)
        quality["gruplac_added_people"] = len(people - manifest)
        audit = {
            "_id": f"{self.run_name}_audit", "run_name": self.run_name,
            "status": "passed" if not critical else "failed",
            "audited_at": utc_now(), "collection": self.target_collection,
            "documents": len(output_ids), "source_proofs": deepcopy(proofs),
            "critical_counts": dict(sorted(checks.items())),
            "critical_anomalies": critical,
            "quality_counts": dict(sorted(quality.items())),
            "related_works": {
                "policy": "canonical_doi_only", "documents": related_count,
            },
        }
        self.db[PERSON_AUDITS].replace_one({"_id": audit["_id"]}, audit, upsert=True)
        return audit

    def _publish(self, summary: dict[str, Any], audit: dict[str, Any]) -> None:
        publications = self.db[PERSON_PUBLICATIONS]
        current = publications.find_one({"_id": "current"}) or {}
        publication = {
            "_id": self.run_name, "status": "published", "published_at": utc_now(),
            "audit": audit["_id"], "collection": self.target_collection,
            "documents": summary["documents"],
            "final_entity_release": self.final_release_name,
        }
        publications.replace_one({"_id": self.run_name}, publication, upsert=True)
        publications.replace_one({"_id": "current"}, {
            "_id": "current", "current_run": self.run_name,
            "previous_run": clean_text(current.get("current_run")),
            "collection": self.target_collection, "audit": audit["_id"],
            "published_at": publication["published_at"],
        }, upsert=True)

    def _cleanup_transients(self) -> None:
        """Remove resumable artifacts after the immutable target is published."""
        removed = []
        for collection_name in (self.temp_collection, self.edge_collection):
            if collection_name in self.db.list_collection_names():
                self.db[collection_name].drop()
                removed.append(collection_name)
        self.runs.update_one({"_id": self.run_name}, {"$set": {
            "transient_cleanup": {
                "status": "complete", "removed": removed,
                "finished_at": utc_now(),
            }
        }})

    def run(self) -> dict[str, Any]:
        previous, proofs = self._prepare()
        if previous.get("status") == "complete":
            summary = deepcopy(previous.get("summary") or {})
            if self.target_collection not in self.db.list_collection_names():
                raise RuntimeError("completed person target is missing")
            if self.db[self.target_collection].count_documents({}) != summary.get("documents"):
                raise RuntimeError("completed person target count changed")
            self._cleanup_transients()
            return summary
        try:
            manifest = self._manifest()
            people = self._identity_universe(manifest)
            built_at = previous.get("created_at") or utc_now()
            self._seed(people)
            self._enrich(
                "directory", self.directory_collection, SOURCE_PROJECTIONS["directory"],
                people, lambda item: self._directory_update(item, built_at),
            )
            self._enrich(
                "recognized", self.recognized_collection,
                SOURCE_PROJECTIONS["recognized"], people,
                lambda item: self._recognized_update(item, built_at),
            )
            latest_open: dict[str, int] = {}
            for item in self.db[self.open_data_collection].find(
                {}, {"id_persona_pr": 1, "ano_convo": 1}
            ):
                code = cod_rh(item.get("id_persona_pr"))
                if code:
                    latest_open[code] = max(
                        latest_open.get(code, 0),
                        source_time(item.get("ano_convo"), built_at),
                    )
            self._enrich(
                "open_data", self.open_data_collection,
                SOURCE_PROJECTIONS["open_data"], people,
                lambda item: self._open_update(item, built_at, latest_open),
            )
            self._enrich(
                "gruplac", self.gruplac_collection, SOURCE_PROJECTIONS["gruplac"],
                people, lambda item: self._gruplac_updates(item, built_at),
            )
            self._enrich(
                "cvlac", self.cvlac_collection, SOURCE_PROJECTIONS["cvlac"],
                people, lambda item: self._cvlac_update(item, built_at),
            )
            for entity in ("works", "projects", "patents", "events"):
                self._scan_references(entity, self.release_collections[entity], people)
            self._attach_related_works()
            self._finalize()
            audit = self._audit(people, manifest, proofs)
            if audit["critical_anomalies"]:
                raise RuntimeError(
                    f"person materialization audit failed: {audit['critical_counts']}"
                )
            target = self.db[self.temp_collection]
            target.create_index("external_ids.id.COD_RH")
            target.create_index("external_ids.id")
            target.create_index("affiliations.id")
            target.create_index("full_name")
            target.rename(self.target_collection, dropTarget=False)
            summary = {
                "status": "complete", "collection": self.target_collection,
                "documents": len(people),
                "related_works": audit["related_works"]["documents"],
                "audit": audit["_id"], "quality": audit["quality_counts"],
                "finished_at": utc_now(),
            }
            self.runs.update_one({"_id": self.run_name}, {"$set": {
                "status": "complete", "summary": summary,
                "finished_at": summary["finished_at"],
            }})
            summary = deepcopy(
                (self.runs.find_one({"_id": self.run_name}) or {}).get("summary")
                or summary
            )
            self._publish(summary, audit)
            self._cleanup_transients()
            return summary
        except Exception as error:
            self.runs.update_one({"_id": self.run_name}, {"$set": {
                "status": "failed", "failed_at": utc_now(),
                "error": f"{type(error).__name__}: {error}",
            }})
            raise
