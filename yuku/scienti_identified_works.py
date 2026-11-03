"""Optional, locally validated work selection from a pinned ScienTI release.

This selects existing documents; it never merges works or infers authorship.
The input release and its publication pointer are read only.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import inspect
import json
import logging
from pathlib import Path
import re
from uuid import uuid4

from bson import BSON, ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from yuku.cvlac_work_graph import normalize_isbn_identity, type_family


LOGGER = logging.getLogger(__name__)
POLICY_VERSION = "scienti-identified-works-v1"
RUNS = "scienti_identified_work_runs"
AUDITS = "scienti_identified_work_audits"
TARGETS = "scienti_identified_work_targets"
PUBLICATIONS = "scienti_identified_work_publications"
RELEASES = "scienti_final_release_publications"
RELEASE_AUDITS = "scienti_final_release_audits"
FIELDS = frozenset({
    "titles", "updated", "doi", "keywords", "types", "external_ids",
    "year_published", "bibliographic_info", "author_count", "authors",
    "source", "ranking", "subjects", "groups",
})
COUNT_PROJECTION = {
    "doi": 1, "external_ids": 1, "types": 1,
    "bibliographic_info.minciencias.product_ids": 1,
    "source.external_ids": 1,
}
DOI_INPUT = re.compile(
    r"(?:(?:https?://)?(?:dx\.)?doi\.org/|doi\s*:\s*)?"
    r"(10\.([0-9]{4,9})/[^\s]+)", re.IGNORECASE,
)
CODE_RH = re.compile(r"[0-9]{10}")
CODE_PRODUCT = re.compile(r"[0-9]{1,7}")
EMPTY = object()


def _utcnow():
    return datetime.now(timezone.utc)


def _digest(value):
    return hashlib.sha256(BSON.encode(value)).hexdigest()


def _xor(left, right):
    return f"{int(left, 16) ^ int(right, 16):064x}"


def _sparse(value):
    """Remove absent metadata recursively, retaining explicit zero/False."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return EMPTY
    if isinstance(value, dict):
        cleaned = {key: item for key, raw in value.items()
                   if (item := _sparse(raw)) is not EMPTY}
        return cleaned if cleaned else EMPTY
    if isinstance(value, list):
        cleaned = [item for raw in value if (item := _sparse(raw)) is not EMPTY]
        return cleaned if cleaned else EMPTY
    return value


def _optional_module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        return None


def _shared_helpers():
    """Prefer local shared wrappers; support their published predecessors.

    Work.py and Mongo.py are not present in every utils 0.1.5 installation.
    The compatibility paths use the same doi_processor and empty_work schema.
    """
    try:
        from kahi.KahiBase import KahiBase
        from kahi_impactu_utils.Utils import doi_processor
        work = _optional_module("kahi_impactu_utils.Work")
        mongo = _optional_module("kahi_impactu_utils.Mongo")
    except ImportError as error:
        raise RuntimeError(
            "Identified works need the optional dependencies: "
            "pip install 'Yuku[identified_works]' (or install the local "
            "Kahi and Kahi_impactu_utils checkouts)."
        ) from error

    def canonical(value):
        normalized = doi_processor(value)
        if isinstance(normalized, str) and re.fullmatch(
                r"https://doi.org/10\.[0-9]{4,9}/\S+", normalized):
            return normalized
        return None

    def order(document, template):
        return {key: document[key] for key in ("_id", *template)
                if key in document}

    canonical = getattr(work, "canonical_work_doi", canonical)
    order = getattr(mongo, "order_document", order)
    template = KahiBase().empty_work()
    fingerprint = {
        "canonical": inspect.getsource(canonical),
        "doi_processor": inspect.getsource(doi_processor),
        "order": inspect.getsource(order),
        "empty_work": inspect.getsource(KahiBase.empty_work),
        "isbn": inspect.getsource(normalize_isbn_identity),
    }
    return canonical, order, template, {
        key: hashlib.sha256(value.encode()).hexdigest()
        for key, value in fingerprint.items()
    }


def valid_isbn(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9Xx\s-]+", value):
        return None
    compact = normalize_isbn_identity(value)
    if not compact or set(compact) == {"0"}:
        return None
    if len(compact) == 13 and not compact.startswith(("978", "979")):
        return None
    return compact


def valid_issn(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9Xx\s-]+", value):
        return None
    compact = re.sub(r"[\s-]", "", value).upper()
    if not re.fullmatch(r"[0-9]{7}[0-9X]", compact) or compact == "00000000":
        return None
    digits = [10 if item == "X" else int(item) for item in compact]
    if sum((8 - index) * item for index, item in enumerate(digits)) % 11:
        return None
    return f"{compact[:4]}-{compact[4:]}"


def _pair_codes(value):
    if not isinstance(value, dict):
        return None
    rh, product = value.get("COD_RH"), value.get("COD_PRODUCTO")
    if (not isinstance(rh, str) or not CODE_RH.fullmatch(rh)
            or rh == "0000000000" or not isinstance(product, str)
            or not CODE_PRODUCT.fullmatch(product)):
        return None
    return rh, product


def _entries(document, key="external_ids"):
    value = document.get(key)
    return value if isinstance(value, list) else []


def _official_products(document):
    values = [entry.get("id") for entry in _entries(document)
              if isinstance(entry, dict) and entry.get("source") == "minciencias"]
    info = document.get("bibliographic_info") or {}
    if isinstance(info, dict):
        minciencias = info.get("minciencias") or {}
        if isinstance(minciencias, dict):
            values.extend(_entries(minciencias, "product_ids"))
    return {value for value in values if isinstance(value, str)}


class WorkIdentifierPolicy:
    """Pure selector/projection; existence checks are supplied by the runner."""

    def __init__(self, *, helpers=None):
        self.canonical, self.order, self.template, self.helpers = (
            helpers if helpers is not None else _shared_helpers()
        )
        if not FIELDS.issubset(self.template):
            raise ValueError("The installed empty_work schema misses required fields")
        self.fields = tuple(key for key in self.template if key in FIELDS)
        self.fingerprint = _digest({
            "version": POLICY_VERSION, "helpers": self.helpers,
            "fields": list(self.template),
            "implementation": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        })

    def doi(self, value):
        if not isinstance(value, str):
            return None
        match = DOI_INPUT.fullmatch(value.strip())
        if not match:
            return None
        bare, registrant = match.groups()
        suffix = bare.split("/", 1)[1]
        if (set(registrant) == {"0"} or any(char in bare for char in "?#\x00")
                or re.fullmatch(r"(?:0+|none|null|n/?a|sin[-_]?doi)", suffix, re.I)):
            return None
        normalized = self.canonical(value.strip())
        # Shared helpers may repair identities; require identity preservation.
        if normalized != f"https://doi.org/{bare.lower()}":
            return None
        return normalized

    def pair_candidates(self, document):
        products = _official_products(document)
        return {
            pair[0] for entry in _entries(document)
            if isinstance(entry, dict) and entry.get("source") == "scienti"
            and (pair := _pair_codes(entry.get("id")))
            and any(value.endswith(f"-{pair[0]}-{pair[1]}") for value in products)
        }

    def evaluate(self, document, people):
        """Return valid identifiers and occurrence-level rejection counters."""
        products = _official_products(document)
        rejection = Counter()
        if "external_ids" in document and not isinstance(document["external_ids"], list):
            rejection["identifier_invalid_structure"] += 1
        identifiers = []
        dois, isbns, pairs, issns = set(), set(), set(), set()
        top_doi = self.doi(document.get("doi"))
        if top_doi:
            dois.add(top_doi)
        elif document.get("doi"):
            rejection["doi_invalid"] += 1
        for entry in _entries(document):
            if not isinstance(entry, dict):
                rejection["identifier_invalid_structure"] += 1
                continue
            family, raw = entry.get("source"), entry.get("id")
            cleaned = deepcopy(entry)
            if family in {"doi", "isbn", "issn"}:
                normalized = {"doi": self.doi, "isbn": valid_isbn,
                              "issn": valid_issn}[family](raw)
                if not normalized:
                    rejection[f"{family}_invalid"] += 1
                    continue
                cleaned["id"] = normalized
                {"doi": dois, "isbn": isbns, "issn": issns}[family].add(normalized)
            elif family == "scienti":
                pair = _pair_codes(raw)
                if not pair:
                    rejection["pair_invalid_format"] += 1
                    continue
                if not any(value.endswith(f"-{pair[0]}-{pair[1]}") for value in products):
                    rejection["pair_missing_product_evidence"] += 1
                    continue
                if pair[0] not in people:
                    rejection["pair_person_missing"] += 1
                    continue
                pairs.add(pair)
                cleaned["id"] = {"COD_RH": pair[0], "COD_PRODUCTO": pair[1]}
            elif isinstance(family, str) and "fingerprint" in family:
                continue
            identifiers.append(cleaned)
        container = self._container(document.get("source"))
        kinds = [kind for kind, values in (("doi", dois), ("isbn", isbns),
                                          ("pair", pairs)) if values]
        chapter = any(
            type_family(entry.get("type", "")) == "book_chapter"
            or "capitulo" in str(entry.get("type", "")).lower()
            or "chapter" in str(entry.get("type", "")).lower()
            for entry in _entries(document, "types") if isinstance(entry, dict)
        )
        return {
            "eligible": bool(kinds), "combination": "+".join(kinds) or "none",
            "doi": top_doi or (sorted(dois)[0] if dois else None),
            "external_ids": identifiers, "container": container,
            "criteria": kinds, "rejections": dict(rejection),
            "issn_only": not kinds and bool(issns),
            "isbn_role": ("container" if chapter else "work") if isbns else None,
        }

    def _container(self, source):
        if not isinstance(source, dict):
            return source
        cleaned = deepcopy(source)
        if "external_ids" in cleaned:
            identifiers = []
            for entry in _entries(cleaned):
                if not isinstance(entry, dict):
                    continue
                family = entry.get("source")
                validator = {"isbn": valid_isbn, "issn": valid_issn,
                             "doi": self.doi}.get(family)
                if validator:
                    normalized = validator(entry.get("id"))
                    if normalized:
                        identifiers.append({**entry, "id": normalized})
                elif not (isinstance(family, str) and "fingerprint" in family):
                    identifiers.append(entry)
            cleaned["external_ids"] = identifiers
        return cleaned

    def project(self, document, decision):
        if not decision["eligible"]:
            raise ValueError("Cannot export an excluded work")
        clean = {key: deepcopy(value) for key, value in document.items()
                 if key in FIELDS or key == "_id"}
        clean["doi"] = decision["doi"]
        clean["external_ids"] = decision["external_ids"]
        if "source" in clean:
            clean["source"] = decision["container"]
        return self.order(_sparse(clean), self.template)


def _resume_query(last_id):
    """Mongo comparisons bracket BSON types; explicitly traverse both ID types."""
    if last_id is None:
        return {}
    if isinstance(last_id, str):
        return {"$or": [{"_id": {"$type": "string", "$gt": last_id}},
                        {"_id": {"$type": "objectId"}}]}
    if isinstance(last_id, ObjectId):
        return {"_id": {"$type": "objectId", "$gt": last_id}}
    raise ValueError("Only string and ObjectId source identities are supported")


class ScientiIdentifiedWorkSelector:
    """Checkpointed count/materialization with exclusive leases and atomic rename."""

    def __init__(self, db, *, release_name, run_name, target_collection="",
                 count_only=False, batch_size=500, progress_every=10000,
                 expected_count_run="", policy=None):
        self.db = db
        for label, value in (("release_name", release_name), ("run_name", run_name)):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise ValueError(f"{label} must be an explicit safe name")
        if count_only and (target_collection or expected_count_run):
            raise ValueError("Count-only must not name a target or expected count")
        if not count_only and not re.fullmatch(r"[A-Za-z0-9_-]+", target_collection):
            raise ValueError("Materialization needs an explicit safe target collection")
        if batch_size < 1 or progress_every < 1:
            raise ValueError("batch_size and progress_every must be positive")
        self.release_name, self.run_name = release_name, run_name
        self.target_name, self.count_only = target_collection, count_only
        self.batch_size, self.progress_every = batch_size, progress_every
        self.expected_count_run = expected_count_run
        self.policy = policy or WorkIdentifierPolicy()
        self.owner = uuid4().hex
        self.stage_name = f"{target_collection}__building_{run_name}" if target_collection else ""
        if len(self.stage_name.encode()) > 180:
            raise ValueError("Target and run names are too long")
        self.people = {}  # bounded positive/negative identity cache

    def _source(self):
        release = self.db[RELEASES].find_one({"_id": self.release_name}) or {}
        audit = self.db[RELEASE_AUDITS].find_one({"_id": release.get("audit")}) or {}
        collections = release.get("collections") or {}
        if (release.get("status") != "published" or audit.get("status") != "passed"
                or audit.get("critical_anomalies") != 0
                or audit.get("release_name") != self.release_name
                or audit.get("collections") != collections):
            raise RuntimeError("The pinned release must have a matching passed audit")
        if not collections.get("works") or not collections.get("persons"):
            raise RuntimeError("The release must contain works and persons")
        forbidden = set(collections.values()) | {RUNS, AUDITS, TARGETS, PUBLICATIONS,
                                                 RELEASES, RELEASE_AUDITS}
        if self.target_name in forbidden or self.stage_name in forbidden:
            raise ValueError("The selector cannot write over source or control collections")
        existing = set(self.db.list_collection_names())
        if not {collections["works"], collections["persons"]}.issubset(existing):
            raise RuntimeError("Pinned source collections are missing")
        counts = {key: self.db[collections[key]].count_documents({})
                  for key in ("works", "persons")}
        reference = audit.get("reference_metrics") or {}
        for key in counts:
            audited = reference.get(f"{key}_documents", reference.get(f"{key}_scanned"))
            if audited != counts[key]:
                raise RuntimeError(f"Pinned {key} count differs from its release audit")
        return {
            "release_name": self.release_name, "collections": collections,
            "counts": counts, "release_digest": _digest(release),
            "audit_digest": _digest(audit),
        }

    def _set(self, fields):
        result = self.db[RUNS].update_one(
            {"_id": self.run_name, "owner": self.owner},
            {"$set": {**fields, "heartbeat_at": _utcnow(),
                      "lease_until": _utcnow() + timedelta(seconds=120)}},
        )
        if result.matched_count != 1:
            raise RuntimeError("Selection lease was lost")

    def _acquire(self, config):
        initial = {
            "_id": self.run_name, "config": config, "status": "pending",
            "created_at": _utcnow(), "last_id": None,
            "stats": {"processed": 0, "selected": 0, "excluded": 0,
                      "by_criterion": {}, "combinations": {}, "rejections": {},
                      "rejection_examples": {},
                      "issn_only_excluded": 0, "isbn_roles": {}, "fields_populated": {}},
            "selection_digest": "0" * 64, "documents_digest": "0" * 64,
        }
        try:
            self.db[RUNS].insert_one(initial)
        except DuplicateKeyError:
            pass
        current = self.db[RUNS].find_one({"_id": self.run_name})
        if current["config"] != config:
            raise ValueError("This run is pinned to another source, policy, mode or target")
        if current["status"] == "complete":
            return current
        acquired = self.db[RUNS].find_one_and_update(
            {"_id": self.run_name,
             "$or": [{"lease_until": {"$exists": False}},
                      {"lease_until": {"$lt": _utcnow()}}]},
            {"$set": {"owner": self.owner,
                      "lease_until": _utcnow() + timedelta(seconds=120)},
             "$unset": {"error": ""}},
            return_document=ReturnDocument.AFTER,
        )
        if not acquired:
            raise RuntimeError("Another process owns this run; expired leases recover after 120 s")
        return acquired

    def _reserve_target(self, state):
        if self.count_only:
            return
        reservation = {"_id": self.target_name, "run_name": self.run_name,
                       "stage": self.stage_name, "config": state["config"]}
        try:
            self.db[TARGETS].insert_one(reservation)
        except DuplicateKeyError:
            if self.db[TARGETS].find_one({"_id": self.target_name}) != reservation:
                raise ValueError("Another run already owns this target")
        existing = set(self.db.list_collection_names())
        if self.target_name in existing and state["status"] != "publishing":
            raise ValueError("Destination already exists; it will never be overwritten")
        if self.stage_name not in existing and state["status"] != "publishing":
            if state["stats"]["processed"]:
                raise RuntimeError("The checkpoint's staging collection is missing")
            self.db.create_collection(self.stage_name)

    def _people_for(self, documents, source):
        codes = set().union(*(self.policy.pair_candidates(doc) for doc in documents))
        missing = codes - self.people.keys()
        if missing:
            rows = self.db[source["collections"]["persons"]].find(
                {"_id": {"$in": sorted(missing)}}, {"_id": 1},
            )
            found = {row["_id"] for row in rows}
            self.people.update({code: code in found for code in missing})
        found = {code for code in codes if self.people.get(code)}
        if len(self.people) > 100000:
            self.people.clear()
        return found

    def _write_documents(self, documents):
        # Bulk replacements retain BSON order and the original ID type.
        # Idempotent replacements replay an uncheckpointed batch after a crash.
        if documents:
            from pymongo import ReplaceOne
            self.db[self.stage_name].bulk_write([
                ReplaceOne({"_id": doc["_id"]}, doc, upsert=True) for doc in documents
            ], ordered=False)

    def _scan(self, state, source):
        stats = deepcopy(state["stats"])
        selection_digest, documents_digest = state["selection_digest"], state["documents_digest"]
        projection = COUNT_PROJECTION if self.count_only else {key: 1 for key in FIELDS}
        cursor = self.db[source["collections"]["works"]].find(
            _resume_query(state["last_id"]), projection,
        ).sort("_id", 1).batch_size(self.batch_size)
        self._set({"status": "scanning"})
        try:
            batch = []
            for doc in cursor:
                if not isinstance(doc["_id"], (str, ObjectId)):
                    raise ValueError("Unsupported BSON type for a source _id")
                batch.append(doc)
                if len(batch) == self.batch_size:
                    selection_digest, documents_digest = self._batch(
                        batch, source, stats, selection_digest, documents_digest,
                    )
                    batch = []
            if batch:
                selection_digest, documents_digest = self._batch(
                    batch, source, stats, selection_digest, documents_digest,
                )
        finally:
            cursor.close()
        if stats["processed"] != source["counts"]["works"]:
            raise RuntimeError("Processed count does not match the pinned source")
        return self.db[RUNS].find_one({"_id": self.run_name})

    def _batch(self, batch, source, stats, selection_digest, documents_digest):
        self._set({})  # renew/check ownership before touching the staging collection
        people = self._people_for(batch, source)
        output = []
        for doc in batch:
            decision = self.policy.evaluate(doc, people)
            stats["processed"] += 1
            stats["selected" if decision["eligible"] else "excluded"] += 1
            for kind in decision["criteria"]:
                counter = stats["by_criterion"]
                counter[kind] = counter.get(kind, 0) + 1
            combinations = stats["combinations"]
            combinations[decision["combination"]] = combinations.get(decision["combination"], 0) + 1
            for kind, count in decision["rejections"].items():
                stats["rejections"][kind] = stats["rejections"].get(kind, 0) + count
                examples = stats["rejection_examples"].setdefault(kind, [])
                if len(examples) < 5:
                    examples.append(doc["_id"])
            stats["issn_only_excluded"] += int(decision["issn_only"])
            if decision["isbn_role"]:
                role = decision["isbn_role"]
                stats["isbn_roles"][role] = stats["isbn_roles"].get(role, 0) + 1
            if decision["eligible"]:
                selection_digest = _xor(selection_digest, _digest({
                    "_id": doc["_id"], "criteria": decision["criteria"],
                }))
                if not self.count_only:
                    projected = self.policy.project(doc, decision)
                    output.append(projected)
                    documents_digest = _xor(documents_digest, _digest(projected))
                    for key in projected:
                        counter = stats["fields_populated"]
                        counter[key] = counter.get(key, 0) + 1
        self._write_documents(output)
        self._set({"last_id": batch[-1]["_id"], "stats": stats,
                   "selection_digest": selection_digest, "documents_digest": documents_digest})
        if stats["processed"] % self.progress_every < len(batch):
            LOGGER.info("run=%s processed=%s/%s selected=%s", self.run_name,
                        stats["processed"], source["counts"]["works"], stats["selected"])
        return selection_digest, documents_digest

    def _expected(self, state, *, check_result=True):
        if not self.expected_count_run:
            return
        expected = self.db[RUNS].find_one({"_id": self.expected_count_run}) or {}
        config = expected.get("config") or {}
        if (expected.get("status") != "complete" or not config.get("count_only")
                or config.get("source") != state["config"]["source"]
                or config.get("policy") != self.policy.fingerprint):
            raise RuntimeError("Expected count must be complete with the same pinned source and policy")
        if check_result and (expected["selection_digest"] != state["selection_digest"]
                or expected["stats"]["selected"] != state["stats"]["selected"]):
            raise RuntimeError("Materialization differs from the exact count")

    def _audit(self, state, source):
        self._set({"status": "auditing"})
        if self._source() != source:
            raise RuntimeError("Pinned source or release evidence changed during selection")
        self._expected(state)
        digest = "0" * 64
        count = 0
        collection = self.db[self.stage_name]
        cursor = collection.find({}).sort("_id", 1).batch_size(self.batch_size)
        try:
            for document in cursor:
                from yuku.scienti_types import type_anomalies
                if type_anomalies(document.get("types")):
                    raise RuntimeError("Filtered works contain types outside the source catalog")
                expected_order = ["_id", *(key for key in self.policy.fields if key in document)]
                if list(document) != expected_order or _sparse(document) != document:
                    raise RuntimeError("Stored metadata is not sparse or does not follow empty_work")
                digest = _xor(digest, _digest(document))
                count += 1
                if count % self.batch_size == 0:
                    self._set({"audit_documents": count})
                if count % self.progress_every == 0:
                    LOGGER.info("run=%s audit=%s/%s", self.run_name, count, state["stats"]["selected"])
        finally:
            cursor.close()
        if count != state["stats"]["selected"] or digest != state["documents_digest"]:
            raise RuntimeError("Stored documents differ from the validated projection")
        audit = {
            "_id": self.run_name, "status": "passed", "critical_anomalies": 0,
            "config": state["config"], "documents": count,
            "documents_digest": digest, "selection_digest": state["selection_digest"],
            "stats": state["stats"], "audited_at": _utcnow(),
        }
        self.db[AUDITS].replace_one({"_id": self.run_name}, audit, upsert=True)
        self._set({"status": "publishing"})

    def _publish(self, state):
        audit = self.db[AUDITS].find_one({"_id": self.run_name}) or {}
        if (audit.get("status") != "passed" or audit.get("config") != state["config"]
                or audit.get("documents_digest") != state["documents_digest"]):
            raise RuntimeError("Publication requires this run's passed audit")
        self._set({"status": "publishing"})
        existing = set(self.db.list_collection_names())
        if self.stage_name in existing:
            self.db[self.stage_name].rename(self.target_name, dropTarget=False)
        elif self.target_name not in existing:
            raise RuntimeError("Both staging and published collections are missing")
        if self.db[self.target_name].count_documents({}) != audit["documents"]:
            raise RuntimeError("Published collection count differs from the audit")
        publication = {
            "_id": self.target_name, "run_name": self.run_name,
            "release_name": self.release_name, "audit": self.run_name,
            "documents": audit["documents"], "published_at": _utcnow(),
        }
        self.db[PUBLICATIONS].replace_one({"_id": self.target_name}, publication, upsert=True)
        self._set({"status": "complete", "completed_at": _utcnow()})
        return self.db[RUNS].find_one({"_id": self.run_name})

    def run(self):
        source = self._source()
        config = {
            "source": source, "policy": self.policy.fingerprint,
            "policy_version": POLICY_VERSION, "helpers": self.policy.helpers,
            "fields": list(self.policy.fields), "count_only": self.count_only,
            "target": self.target_name, "expected_count_run": self.expected_count_run,
        }
        self._expected({"config": config}, check_result=False)
        state = self._acquire(config)
        if state["status"] == "complete":
            if not self.count_only:
                audit = self.db[AUDITS].find_one({"_id": self.run_name}) or {}
                if (audit.get("status") != "passed"
                        or self.db[self.target_name].count_documents({}) != state["stats"]["selected"]):
                    raise RuntimeError("Completed snapshot is missing or differs from its audit")
            return state
        try:
            self._reserve_target(state)
            if state["status"] == "publishing":
                return self._publish(state)
            state = self._scan(state, source)
            if self.count_only:
                if self._source() != source:
                    raise RuntimeError("Pinned source changed during the count")
                self._set({"status": "complete", "completed_at": _utcnow()})
                return self.db[RUNS].find_one({"_id": self.run_name})
            self._audit(state, source)
            return self._publish(state)
        except Exception as error:
            # Preserve 'publishing' to recover the atomic rename/control-write window.
            current = self.db[RUNS].find_one({"_id": self.run_name}) or {}
            fields = {"error": repr(error)}
            if current.get("status") != "publishing":
                fields["status"] = "failed"
            self.db[RUNS].update_one({"_id": self.run_name, "owner": self.owner}, {"$set": fields})
            raise
        finally:
            self.db[RUNS].update_one(
                {"_id": self.run_name, "owner": self.owner},
                {"$unset": {"owner": "", "lease_until": ""}},
            )
