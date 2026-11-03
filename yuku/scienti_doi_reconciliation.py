"""Audited local journal extraction and optional DOI identity confirmation.

No registry bibliographic metadata is copied into normalized records or works.
Only close DOI/title candidates can trigger remote requests.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import combinations
import json
import logging
from pathlib import Path
import re
import time
from urllib.parse import quote
from uuid import uuid4

from bson import BSON
from pymongo import ReturnDocument, UpdateOne, ReplaceOne
import requests

from yuku.cvlac_related_works import norm_text
from yuku.cvlac_work_graph import type_family
from yuku.scienti_journals import enrich_article
from yuku.scienti_normalization import CVLAC_AUDIT_RUNS, GRUPLAC_AUDIT_RUNS
from yuku.scienti_routing import route_cvlac, route_gruplac
from yuku.scienti_doi_history import DOIResolutionHistory, COMPARISON_POLICY, HISTORY

RUNS = "scienti_doi_reconciliation_runs"
AUDITS = "scienti_doi_reconciliation_audits"
CHECKS = "scienti_doi_identity_checks"
VERSION = "scienti-doi-confirmation-v1"
# This implementation indexed the same candidates but failed before checking
# any DOI when Mongo distinct included null for an absent array. Only that
# failed, preconfirmation checkpoint can migrate without rebuilding its index.
PRECONFIRM_NULL_BUG_IMPLEMENTATIONS = {
    "353b896f39fd9be6c034e21e77eca23c23fd0fef9aa27b18a41ab70cceef5efe",
}
LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             default=str).encode()).hexdigest()


def title_key(value):
    return re.sub(r"[^\w]+", " ", norm_text(str(value or ""))).strip()


def similar_dois(left, right):
    if left == right:
        return False
    a, b = [value.removeprefix("https://doi.org/").split("/", 1) for value in (left, right)]
    if a[0] != b[0]:
        return False
    a, b = a[1], b[1]
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 8 and longer.startswith(shorter) and longer[len(shorter):1 + len(shorter)] in {"/", ".", "-"}:
        return True
    if min(len(a), len(b)) < 8 or abs(len(a) - len(b)) > 2:
        return False
    # At most two character edits, with a high relative similarity. A shared
    # registrant prefix alone never qualifies two identifiers as candidates.
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (x != y)))
        previous = current
    return previous[-1] <= 2 and SequenceMatcher(None, a, b).ratio() >= .90


def record_rows(record, doi_normalizer):
    if not record.get("doi"):
        return []
    key = title_key(record.get("title"))
    if len(key) < 15:
        return []
    family = type_family(record.get("type_impactu", ""))
    year = record.get("year")
    values = record.get("doi") or []
    if not isinstance(values, list):
        values = [values]
    rows = []
    for doi in sorted({value for raw in values if (value := doi_normalizer(raw))}):
        identity = {"doi": doi, "title": key, "year": year, "family": family}
        prefix = doi.removeprefix("https://doi.org/").split("/", 1)[0]
        # Exact titles plus a conservative near-title block. This blocking
        # intentionally favors precision and avoids quadratic universe scans.
        tokens = sorted(set(key.split()), key=lambda s: (-len(s), s))[:3]
        blocks = [digest((prefix, family, key))]
        blocks.extend(digest((prefix, family, sorted(pair))) for pair in combinations(tokens, 2))
        rows.append({"_id": digest(identity), **identity, "blocks": sorted(set(blocks))})
    return rows


def identity_match(row, identity):
    score = max((SequenceMatcher(None, row["title"], title_key(value)).ratio()
                 for value in identity.get("titles", [])), default=0)
    families = {"journal-article": "article", "book": "book", "monograph": "book",
                "book-chapter": "book_chapter", "Book": "book", "BookChapter": "book_chapter",
                "JournalArticle": "article", "Dissertation": "graduate_thesis"}
    same_year = bool(row.get("year") and row["year"] in identity.get("years", []))
    same_type = families.get(identity.get("type")) == row.get("family")
    return {"title_score": round(score, 6), "year_match": same_year,
            "type_match": same_type, "confirmed": score >= .98 and same_year and same_type}


class DOIIdentityVerifier:
    """Confirm identity using permanent evidence before any registry request."""

    def __init__(self, db, run_name, *, session=None, delay=1.0, attempts=3, timeout=20,
                 missing_ttl_days=7):
        self.cache = db[CHECKS]
        self.history = DOIResolutionHistory(db, CHECKS, missing_ttl_days=missing_ttl_days)
        self.history_ready = False
        self.identity = None
        self.run_name = run_name
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Yuku/DOI-identity-confirmation (Colav)",
                                     "Accept": "application/json"})
        self.delay, self.attempts, self.timeout = delay, attempts, timeout
        self.last_request = 0.0

    def _get(self, url, params=None):
        for attempt in range(self.attempts):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                self.history.metrics["http_requests"] += 1
                response = self.session.get(url, params=params, timeout=self.timeout,
                                            allow_redirects=False)
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return response
                retry = response.headers.get("Retry-After", "")
                delay = min(30, int(retry)) if retry.isdigit() else min(30, 2 ** (attempt + 1))
            except requests.RequestException:
                delay = min(30, 2 ** (attempt + 1))
            if attempt + 1 < self.attempts:
                time.sleep(delay)
        return None

    def verify(self, doi, rows):
        if not self.history_ready:
            self.history.bootstrap()
            self.history_ready = True
        cache_id = digest((VERSION, self.run_name, doi))
        reusable = self.history.reuse(doi, rows, identity_match)
        if reusable:
            return self._save({**reusable, "_id": cache_id, "run_name": self.run_name})
        token = self.history.acquire(doi)
        if token is None:
            return self._save({"_id": cache_id, "run_name": self.run_name, "doi": doi,
                "checked_at": now(), "status": "pending", "matches": {},
                "reason": "historical_confirmation_in_progress", "history_id": self.history.key(doi)})
        try:
            # Another process may have completed its confirmation between the
            # first history read and our acquisition of the per-DOI lease.
            reusable = self.history.reuse(doi, rows, identity_match, count_expiry=False)
            if reusable:
                return self._save({**reusable, "_id": cache_id, "run_name": self.run_name})
            self.identity = None
            result = self._request_verify(doi, rows)
            self.history.save(result, rows, self.identity, token)
            result.update(history_id=self.history.key(doi), comparison_policy=COMPARISON_POLICY,
                          reused_history=False)
            return self._save(result)
        finally:
            self.history.release(doi, token)

    def _request_verify(self, doi, rows):
        cache_id = digest((VERSION, self.run_name, doi))
        ids = {row["_id"] for row in rows}
        result = {"_id": cache_id, "run_name": self.run_name, "doi": doi,
                  "checked_at": now(), "status": "pending", "matches": {}}
        raw = doi.removeprefix("https://doi.org/")
        handle = self._get("https://doi.org/api/handles/" + quote(raw, safe="/"), {"type": "URL"})
        result["handle_http"] = handle.status_code if handle is not None else None
        if handle is None:
            return result
        try:
            payload = handle.json()
            code = payload.get("responseCode")
            result["handle_code"] = code
            if handle.status_code == 404 and code == 100:
                result.update(status="missing", matches={key: {"confirmed": False} for key in ids})
                return result
            if handle.status_code != 200 or code != 1:
                return result
            agency = self._get("https://doi.org/doiRA/" + quote(raw, safe="/"))
            result["agency_http"] = agency.status_code if agency is not None else None
            if agency is None or agency.status_code != 200:
                return result
            agency_rows = agency.json()
            ra = agency_rows[0].get("RA") if isinstance(agency_rows, list) and agency_rows else None
            result["agency"] = ra
            if ra == "Crossref":
                response = self._get("https://api.crossref.org/works", {
                    "filter": "doi:" + raw, "select": "DOI,title,published,type", "rows": 1,
                })
                result["identity_http"] = response.status_code if response is not None else None
                if response is None or response.status_code != 200:
                    return result
                items = response.json().get("message", {}).get("items", [])
                if len(items) != 1 or str(items[0].get("DOI", "")).lower() != raw:
                    return result
                item = items[0]
                years = [part[0] for part in item.get("published", {}).get("date-parts", []) if part]
                identity = {"titles": item.get("title", []), "years": years, "type": item.get("type")}
            elif ra == "DataCite":
                response = self._get("https://api.datacite.org/dois/" + quote(raw, safe="/"),
                                     {"fields[dois]": "doi,titles,publicationYear,types"})
                result["identity_http"] = response.status_code if response is not None else None
                if response is None or response.status_code != 200:
                    return result
                item = response.json().get("data", {}).get("attributes", {})
                if str(item.get("doi", "")).lower() != raw:
                    return result
                identity = {"titles": [value.get("title", "") for value in item.get("titles", [])],
                            "years": [item.get("publicationYear")],
                            "type": item.get("types", {}).get("resourceTypeGeneral")}
            else:
                result["reason"] = "unsupported_agency"
                return result
            self.identity = {"doi": doi, **identity}
            result.update(status="found", matches={row["_id"]: identity_match(row, identity) for row in rows})
        except (ValueError, TypeError, KeyError, AttributeError):
            result["reason"] = "unexpected_registry_response"
        return result

    def _save(self, result):
        self.cache.replace_one({"_id": result["_id"]}, result, upsert=True)
        return result


class ScientiDOIReconciler:
    """Versioned transformed views, conservative candidates, resumable audit."""

    def __init__(self, db, *, run_name, sources, targets, audits, confirm_doi=False,
                 batch_size=500, max_block=100, request_delay=1.0, verifier=None,
                 doi_normalizer=None):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", run_name):
            raise ValueError("invalid reconciliation run name")
        if any(set(value) != {"cvlac", "gruplac"} for value in (sources, targets, audits)):
            raise ValueError("sources, targets and audits must define cvlac and gruplac")
        names = [*sources.values(), *targets.values()]
        if len(set(names)) != 4 or not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,100}", name) for name in names):
            raise ValueError("source/target names must be safe and distinct")
        if batch_size < 1 or max_block < 2 or request_delay < 0:
            raise ValueError("invalid batch, block or delay")
        self.db, self.run_name = db, run_name
        self.sources, self.targets, self.audits = sources, targets, audits
        self.batch_size, self.max_block = batch_size, max_block
        if doi_normalizer is None:
            from yuku.scienti_identified_works import WorkIdentifierPolicy
            policy = WorkIdentifierPolicy()
            doi_normalizer = policy.doi
            normalizer_policy = policy.fingerprint
        else:
            normalizer_policy = "injected-local-validator"
        self.doi = doi_normalizer
        self.verifier = verifier or DOIIdentityVerifier(db, run_name, delay=request_delay)
        self.rows = db[f"__yuku_{run_name}_doi_rows"]
        self.pairs = db[f"{run_name}_doi_candidates"]
        self.corrections = db[f"{run_name}_doi_corrections"]
        self.runs = db[RUNS]
        self.token = uuid4().hex
        self.config = {"version": VERSION, "sources": sources, "targets": targets, "audits": audits,
                       "confirm_doi": bool(confirm_doi), "max_block": max_block,
                       "confirmation_policy": COMPARISON_POLICY, "doi_history": HISTORY,
                       "missing_ttl_days": 7,
                       "normalizer_policy": normalizer_policy,
                       "implementation": sha256(Path(__file__).read_bytes()).hexdigest(),
                       "journal_implementation": sha256(Path(__file__).with_name("scienti_journals.py").read_bytes()).hexdigest()}

    def _touch(self, **values):
        outcome = self.runs.update_one({"_id": self.run_name, "lease_token": self.token},
            {"$set": {**values, "updated_at": now(), "lease_until": now() + timedelta(minutes=15)}})
        if not outcome.matched_count:
            raise RuntimeError("reconciliation lease lost")

    def _state(self):
        return self.runs.find_one({"_id": self.run_name}) or {}

    def _proofs(self):
        proofs = {}
        for kind, collection in self.sources.items():
            audit_collection = CVLAC_AUDIT_RUNS if kind == "cvlac" else GRUPLAC_AUDIT_RUNS
            audit = self.db[audit_collection].find_one({"_id": self.audits[kind]}) or {}
            config, summary = audit.get("config", {}), audit.get("summary", {})
            actual = self.db[collection].count_documents({})
            expected = summary.get("destination_profiles" if kind == "cvlac" else "normalized_groups")
            if (audit.get("status") != "passed" or summary.get("critical_anomalies")
                    or summary.get("stored_parser_errors") or expected != actual
                    or (config.get("destination_collection") or config.get("normalized_collection")) != collection):
                raise RuntimeError(f"unproven normalized source: {kind}")
            proofs[kind] = {"audit": audit["_id"], "documents": actual,
                            "audit_digest": digest(audit)}
        return proofs

    @staticmethod
    def _bulk(collection, operations):
        if operations:
            collection.bulk_write(operations, ordered=False)

    def _index(self):
        if self._state().get("indexed") or not self.config["confirm_doi"]:
            return
        self.rows.create_index("blocks")
        self.rows.create_index("check_dois")
        for kind, collection in self.sources.items():
            last = self._state().get("indexed_last", {}).get(kind)
            cursor = self.db[collection].find({"_id": {"$gt": last}} if last else {},
                    {"production": 1}).sort("_id", 1)
            operations, count = [], 0
            for document in cursor:
                for record in document.get("production", []):
                    route = route_cvlac("production", record) if kind == "cvlac" else route_gruplac(record)
                    if not route or route.get("entity") != "works":
                        continue
                    for row in record_rows(record, self.doi):
                        operations.append(UpdateOne({"_id": row["_id"]}, {"$setOnInsert": row}, upsert=True))
                count += 1
                if count % self.batch_size == 0:
                    self._bulk(self.rows, operations)
                    operations = []
                    self._touch(**{f"indexed_last.{kind}": document["_id"], "phase": "index"})
                    if count % (self.batch_size * 20) == 0:
                        LOG.info("DOI candidate index %s profiles=%s last=%s", kind, count, document["_id"])
            self._bulk(self.rows, operations)
            if count:
                self._touch(**{f"indexed_last.{kind}": document["_id"]})
        self._touch(indexed=True)

    def _candidates(self):
        if self._state().get("candidates_ready") or not self.config["confirm_doi"]:
            return
        last = self._state().get("last_block", "")
        buckets = self.rows.aggregate([
            {"$unwind": "$blocks"}, {"$match": {"blocks": {"$gt": last}}},
            {"$group": {"_id": "$blocks", "count": {"$sum": 1},
                         "first_doi": {"$min": "$doi"}, "last_doi": {"$max": "$doi"}}},
            {"$match": {"$expr": {"$ne": ["$first_doi", "$last_doi"]}}}, {"$sort": {"_id": 1}},
        ], allowDiskUse=True)
        for bucket in buckets:
            if bucket["count"] > self.max_block:
                self._touch(**{"last_block": bucket["_id"], "phase": "candidates"})
                self.runs.update_one({"_id": self.run_name}, {"$addToSet": {"oversized_blocks": bucket["_id"]}})
                continue
            rows = list(self.rows.find({"blocks": bucket["_id"]}))
            for left, right in combinations(rows, 2):
                if (left["family"] != right["family"] or not similar_dois(left["doi"], right["doi"])
                        or SequenceMatcher(None, left["title"], right["title"]).ratio() < .97):
                    continue
                pair_id = digest(sorted((left["_id"], right["_id"])))
                self.pairs.update_one({"_id": pair_id}, {"$setOnInsert": {
                    "rows": [left["_id"], right["_id"]], "dois": [left["doi"], right["doi"]],
                    "status": "candidate"}}, upsert=True)
                for row in (left, right):
                    self.rows.update_one({"_id": row["_id"]}, {"$addToSet": {"check_dois": {"$each": [left["doi"], right["doi"]]}}})
            self._touch(last_block=bucket["_id"], phase="candidates")
        self._touch(candidates_ready=True)

    def _confirm(self):
        if self._state().get("confirmed") or not self.config["confirm_doi"]:
            return
        # MongoDB can expose null alongside distinct values of absent fields.
        # Restrict the query to candidate arrays and validate before sorting.
        values = self.rows.distinct("check_dois", {"check_dois": {"$type": "array"}})
        dois = sorted({value for value in values
                       if isinstance(value, str) and self.doi(value) == value})
        for index, doi in enumerate(dois, 1):
            rows = list(self.rows.find({"check_dois": doi}))
            self.verifier.verify(doi, rows)
            self._touch(phase="confirm", checked_dois=index, candidate_dois=len(dois))
            if index % 20 == 0:
                LOG.info("DOI identity checks %s/%s", index, len(dois))
        for pair in self.pairs.find({}).sort("_id", 1):
            checks = [self.db[CHECKS].find_one({"_id": digest((VERSION, self.run_name, doi))}) or {}
                      for doi in pair["dois"]]
            found = [i for i, check in enumerate(checks) if check.get("status") == "found"
                     and all(check.get("matches", {}).get(row, {}).get("confirmed") for row in pair["rows"])]
            if len(found) == 1 and checks[1 - found[0]].get("status") == "missing":
                winner = found[0]
                loser_row = pair["rows"][1 - winner]
                proposal = {"doi": pair["dois"][winner], "original": pair["dois"][1 - winner], "candidate": pair["_id"]}
                self.corrections.update_one({"_id": loser_row}, {"$addToSet": {"proposals": proposal}}, upsert=True)
                decision = "confirmed_correction"
            elif any(check.get("status") == "pending" for check in checks):
                decision = "pending"
            else:
                decision = "review"
            self.pairs.update_one({"_id": pair["_id"]}, {"$set": {"status": decision,
                                   "checks": [check.get("_id") for check in checks]}})
        for correction in self.corrections.find():
            if len({value["doi"] for value in correction["proposals"]}) > 1:
                self.pairs.update_many({"rows": correction["_id"]}, {"$set": {
                    "status": "review", "reason": "incompatible_correction_proposals"}})
        self._touch(confirmed=True, decision_counts=dict(Counter(pair["status"] for pair in self.pairs.find({}, {"status": 1}))))

    def _correction_map(self):
        output = {}
        for row in self.corrections.find():
            dois = {value["doi"] for value in row["proposals"]}
            if len(dois) == 1:
                output[row["_id"]] = row["proposals"][0]
        return output

    def transform(self, document, corrections):
        output = deepcopy(document)
        kind = "gruplac" if str(document["_id"]).startswith("COL") else "cvlac"
        for record in output.get("production", []):
            route = route_cvlac("production", record) if kind == "cvlac" else route_gruplac(record)
            if not route or route.get("entity") != "works":
                continue
            originals = record_rows(record, self.doi) if corrections else []
            applied = {row["doi"]: corrections[row["_id"]] for row in originals if row["_id"] in corrections}
            if applied:
                raw = record.get("doi") or []
                raw = raw if isinstance(raw, list) else [raw]
                record["doi"] = list(dict.fromkeys(applied.get(self.doi(value), {}).get("doi", value) for value in raw))
                record["doi_reconciliation"] = {"run": self.run_name, "version": VERSION,
                    "corrections": list(applied.values())}
            enrich_article(record)
        output["bibliographic_transformation"] = {"run": self.run_name, "version": VERSION,
            "registry_enrichment": False}
        return output

    def _copy_and_audit(self):
        corrections = self._correction_map()
        if len(corrections) > 200000:
            raise RuntimeError("correction map exceeds safe bound; review candidate scope")
        for kind, collection in self.sources.items():
            state = self._state()
            if state.get("copied", {}).get(kind):
                continue
            target = self.db[self.targets[kind]]
            last = state.get("copied_last", {}).get(kind)
            buffer = []
            for document in self.db[collection].find({"_id": {"$gt": last}} if last else {}).sort("_id", 1):
                output = self.transform(document, corrections)
                buffer.append(ReplaceOne({"_id": output["_id"]}, output, upsert=True))
                if len(buffer) >= self.batch_size:
                    self._bulk(target, buffer)
                    buffer = []
                    self._touch(**{f"copied_last.{kind}": document["_id"], "phase": "copy"})
            self._bulk(target, buffer)
            self._touch(**{f"copied.{kind}": True})
        counts, coverage, hashes = {}, Counter(), {}
        for kind, collection in self.sources.items():
            target = self.db[self.targets[kind]]
            total = 0
            original_hash, output_hash = sha256(), sha256()
            # Sequential sorted cursors avoid hundreds of thousands of lookups.
            original = self.db[collection].find().sort("_id", 1)
            built = target.find().sort("_id", 1)
            for document in original:
                actual = next(built, None)
                expected = self.transform(document, corrections)
                if actual != expected:
                    raise RuntimeError(f"transformation audit mismatch: {kind}/{document['_id']}")
                original_hash.update(BSON.encode(document))
                output_hash.update(BSON.encode(actual))
                for record in actual.get("production", []):
                    if norm_text(record.get("type_impactu", "")) == "articulo de revista":
                        coverage["articles"] += 1
                        for field in ("journal_name", "publisher", "volume", "issue", "pages"):
                            coverage[field] += bool(record.get(field))
                total += 1
                if total % 500 == 0:
                    self._touch(phase="audit")
                if total % 10000 == 0:
                    LOG.info("Local transformation audit %s %s profiles", kind, total)
            if next(built, None) is not None:
                raise RuntimeError("transformation audit contains unexpected profiles")
            counts[kind] = total
            hashes[kind] = {"source": original_hash.hexdigest(), "output": output_hash.hexdigest()}
        if self._proofs() != self._state()["source_proofs"]:
            raise RuntimeError("source proof changed during reconciliation")
        audit = {"_id": self.run_name, "status": "passed", "version": VERSION,
                 "critical_anomalies": 0, "audited_at": now(), "sources": self.sources,
                 "targets": self.targets, "source_proofs": self._state()["source_proofs"],
                 "documents": counts, "digests": hashes, "local_coverage": dict(coverage),
                 "corrected_identities": len(corrections),
                 "decision_counts": self._state().get("decision_counts", {}),
                 "oversized_blocks": len(self._state().get("oversized_blocks", [])),
                 "registry_enrichment": False}
        if hasattr(self.verifier, "history"):
            audit["history_metrics"] = dict(self.verifier.history.metrics)
        self.db[AUDITS].replace_one({"_id": self.run_name}, audit, upsert=True)
        # These are transformation audits carrying forward the existing parser
        # and raw-HTML coverage proof. They do not claim the parser was rerun.
        for kind in self.sources:
            name = CVLAC_AUDIT_RUNS if kind == "cvlac" else GRUPLAC_AUDIT_RUNS
            wrapper = deepcopy(self.db[name].find_one({"_id": self.audits[kind]}))
            wrapper["_id"] = self.run_name + "_" + kind + "_audit"
            field = "destination_collection" if kind == "cvlac" else "normalized_collection"
            wrapper["config"][field] = self.targets[kind]
            wrapper["config"]["transformation_run"] = self.run_name
            wrapper["base_audit"] = self.audits[kind]
            wrapper["finished_at"] = now()
            wrapper["transformation_audit"] = audit["_id"]
            self.db[name].replace_one({"_id": wrapper["_id"]}, wrapper, upsert=True)
        self._touch(status="complete", phase="complete", summary=audit, completed_at=now())
        return audit

    def run(self):
        proofs = self._proofs()
        existing = self._state()
        existing = self._upgrade_null_checkpoint(existing, proofs)
        if existing and (existing.get("config") != self.config or existing.get("source_proofs") != proofs):
            raise RuntimeError("immutable reconciliation inputs/policy changed")
        if existing.get("status") == "complete":
            return existing["summary"]
        if not existing:
            for target in self.targets.values():
                if target in self.db.list_collection_names():
                    raise ValueError("reconciliation target already exists")
                reservation = self.db[RUNS + "_targets"].find_one({"_id": target})
                if reservation and reservation.get("run") != self.run_name:
                    raise ValueError("reconciliation target reserved by another run")
                reservation = self.db[RUNS + "_targets"].find_one_and_update({"_id": target},
                    {"$setOnInsert": {"run": self.run_name}}, upsert=True,
                    return_document=ReturnDocument.AFTER)
                if reservation.get("run") != self.run_name:
                    raise ValueError("reconciliation target reserved by another run")
            self.runs.insert_one({"_id": self.run_name, "config": self.config,
                                  "source_proofs": proofs, "created_at": now()})
        lease = self.runs.find_one_and_update({"_id": self.run_name,
            "$or": [{"lease_until": {"$exists": False}}, {"lease_until": {"$lt": now()}}]},
            {"$set": {"lease_token": self.token, "lease_until": now() + timedelta(minutes=15),
                      "status": "running"}}, return_document=ReturnDocument.AFTER)
        if not lease:
            raise RuntimeError("reconciliation already running")
        try:
            self._index()
            self._candidates()
            self._confirm()
            return self._copy_and_audit()
        except Exception as error:
            self._touch(status="failed", error=repr(error))
            raise
        finally:
            self.runs.update_one({"_id": self.run_name, "lease_token": self.token},
                                 {"$unset": {"lease_until": "", "lease_token": ""}})

    def _upgrade_null_checkpoint(self, existing, proofs):
        old_config = existing.get("config", {})
        previous = old_config.get("implementation")
        compatible = {**old_config, "implementation": self.config["implementation"]}
        if (previous in PRECONFIRM_NULL_BUG_IMPLEMENTATIONS
                and existing.get("status") == "failed" and not existing.get("confirmed")
                and existing.get("indexed") and existing.get("candidates_ready")
                and existing.get("source_proofs") == proofs and compatible == self.config
                and not self.db[CHECKS].count_documents({"run_name": self.run_name})):
            result = self.runs.update_one({"_id": self.run_name, "status": "failed",
                "config.implementation": previous, "confirmed": {"$ne": True}}, {
                "$set": {"config.implementation": self.config["implementation"]},
                "$push": {"implementation_history": {"previous": previous,
                    "current": self.config["implementation"], "updated_at": now(),
                    "reason": "Ignore null distinct values before DOI confirmation"}},
            })
            if not result.matched_count:
                raise RuntimeError("checkpoint changed during compatibility migration")
            return self._state()
        return existing


def validate_transformation_audit(db, normalization_audit, destination):
    """Additional proof required whenever a normalization audit wraps a view."""
    run_name = normalization_audit.get("config", {}).get("transformation_run")
    if not run_name:
        return
    run = db[RUNS].find_one({"_id": run_name}) or {}
    proof = db[AUDITS].find_one({"_id": run_name}) or {}
    kind = next((kind for kind, target in proof.get("targets", {}).items()
                 if target == destination), None)
    if (run.get("status") != "complete" or proof.get("status") != "passed"
            or proof.get("critical_anomalies") or not kind
            or proof.get("registry_enrichment") is not False
            or db[destination].count_documents({}) != proof.get("documents", {}).get(kind)):
        raise RuntimeError("normalized transformation is not proven")
