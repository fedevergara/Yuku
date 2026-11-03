"""Permanent DOI confirmation evidence, independent of graph/run retention."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

HISTORY = "minciencias_doi_resolution_history"
COMPARISON_POLICY = "scienti-doi-title098-year-family-v1"
LEGACY_VERSION = "scienti-doi-confirmation-v1"
DEFAULT_MISSING_TTL_DAYS = 7


def utcnow():
    return datetime.now(timezone.utc)


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             default=str).encode()).hexdigest()


def context_id(row):
    return fingerprint({key: row.get(key) for key in ("title", "year", "family")})


def aware(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class DOIResolutionHistory:
    def __init__(self, db, checks, *, missing_ttl_days=DEFAULT_MISSING_TTL_DAYS):
        if missing_ttl_days <= 0:
            raise ValueError("missing DOI history requires a positive expiry")
        self.db, self.checks = db, db[checks]
        self.collection = db[HISTORY]
        self.missing_ttl = timedelta(days=missing_ttl_days)
        self.metrics = Counter()
        self.collection.create_index("doi", unique=True, sparse=True)
        self.checks.create_index("doi")

    @staticmethod
    def key(doi):
        return fingerprint(doi)

    def bootstrap(self):
        """Migrate old proofs without HTTP or fabricated registry snapshots.

        Old match IDs already fingerprint DOI/title/year/family. They can prove
        the same context even if the run's temporary rows were already retired.
        The migration is incremental and safe to run after any interruption.
        """
        marker = self.collection.find_one({"_id": "legacy_migration"}) or {}
        last = marker.get("last_id")
        query = {"_id": {"$gt": last}} if last else {}
        migrated = 0
        available = set(self.db.list_collection_names())
        for check in self.checks.find(query).sort("_id", 1):
            doi = check.get("doi")
            checked_at = aware(check.get("checked_at"))
            if (isinstance(doi, str) and doi.startswith("https://doi.org/")
                    and not check.get("history_id")
                    and check["_id"] == fingerprint((LEGACY_VERSION, check.get("run_name"), doi))
                    and checked_at and check.get("status") in {"found", "missing"}):
                key = self.key(doi)
                existing = self.collection.find_one({"_id": key}) or {}
                matches = check.get("matches", {})
                row_collection = "__yuku_" + str(check.get("run_name")) + "_doi_rows"
                contexts = {}
                if row_collection in available and matches:
                    for row in self.db[row_collection].find({"_id": {"$in": list(matches)}},
                            {"title": 1, "year": 1, "family": 1}):
                        contexts[context_id(row)] = {
                            "local": {k: row.get(k) for k in ("title", "year", "family")},
                            "comparison": matches[row["_id"]], "checked_at": checked_at,
                            "comparison_policy": COMPARISON_POLICY,
                        }
                # Preserve a newer snapshot or positive result; never let an
                # old negative replace a known positive identity.
                newer = aware(existing.get("checked_at"))
                same_positive = existing.get("status") == check["status"] == "found"
                eligible = (not existing or (not existing.get("identity")
                            and (same_positive or not newer or checked_at >= newer)
                            and not (existing.get("status") == "found" and check["status"] == "missing")))
                if eligible:
                    evidence = {k: check[k] for k in ("doi", "checked_at", "status", "handle_http",
                                "handle_code", "agency", "agency_http", "identity_http") if k in check}
                    previous_matches = existing.get("legacy_matches", {}) if existing.get("status") == check["status"] else {}
                    older_positive = same_positive and newer and checked_at < newer
                    if older_positive:
                        evidence = {k: existing[k] for k in evidence if k in existing}
                    evidence.update(comparison_policy=COMPARISON_POLICY,
                                    legacy_matches=({**matches, **previous_matches} if older_positive
                                                    else {**previous_matches, **matches}),
                                    contexts=({**contexts, **existing.get("contexts", {})} if older_positive
                                              else {**existing.get("contexts", {}), **contexts}),
                                    migrated_from=check["_id"], updated_at=utcnow())
                    if existing:
                        # Compare-and-swap protects a fresh registry snapshot
                        # written by another process during migration.
                        outcome = self.collection.update_one({"_id": key,
                            "checked_at": existing.get("checked_at"),
                            "status": existing.get("status"),
                            "lease_token": {"$exists": False}}, {"$set": evidence,
                                "$min": {"first_checked_at": checked_at}})
                        migrated += outcome.modified_count
                    else:
                        outcome = self.collection.update_one({"_id": key}, {"$setOnInsert": {
                            **evidence, "first_checked_at": checked_at}}, upsert=True)
                        migrated += int(outcome.upserted_id is not None)
            last = check["_id"]
            if migrated and migrated % 500 == 0:
                self.collection.update_one({"_id": "legacy_migration"}, {
                    "$set": {"last_id": last}}, upsert=True)
        if last:
            self.collection.update_one({"_id": "legacy_migration"}, {
                "$set": {"last_id": last, "completed_at": utcnow()}}, upsert=True)
        self.metrics["migrated_legacy_checks"] += migrated
        return migrated

    def reuse(self, doi, rows, match, *, count_expiry=True):
        saved = self.collection.find_one({"_id": self.key(doi)}) or {}
        status = saved.get("status")
        checked_at = aware(saved.get("checked_at"))
        if status == "missing":
            if not checked_at or utcnow() - checked_at >= self.missing_ttl:
                if count_expiry:
                    self.metrics["expired_negative_checks"] += 1
                return None
            matches = {row["_id"]: {"confirmed": False} for row in rows}
        elif status == "found":
            if saved.get("identity"):
                matches = {row["_id"]: match(row, saved["identity"]) for row in rows}
            elif saved.get("comparison_policy") == COMPARISON_POLICY:
                old = saved.get("legacy_matches", {})
                if not all(row["_id"] in old for row in rows):
                    return None
                matches = {row["_id"]: old[row["_id"]] for row in rows}
            else:
                return None
        else:
            return None
        self.metrics["history_hits"] += 1
        context_updates = {"contexts." + context_id(row): {
            "local": {key: row.get(key) for key in ("title", "year", "family")},
            "comparison": matches[row["_id"]], "checked_at": utcnow(),
            "comparison_policy": COMPARISON_POLICY,
        } for row in rows}
        self.collection.update_one({"_id": saved["_id"]}, {
            "$set": {"last_reused_at": utcnow(), **context_updates}, "$inc": {"reuse_count": 1}})
        return {**{key: saved[key] for key in ("status", "checked_at", "doi", "handle_http",
                 "handle_code", "agency", "agency_http", "identity_http") if key in saved},
                "matches": matches, "history_id": saved["_id"],
                "comparison_policy": COMPARISON_POLICY, "reused_history": True}

    def acquire(self, doi):
        token = uuid4().hex
        try:
            saved = self.collection.find_one_and_update({"_id": self.key(doi), "$or": [
                {"lease_until": {"$exists": False}}, {"lease_until": {"$lt": utcnow()}}]},
                {"$set": {"doi": doi, "lease_token": token,
                          "lease_until": utcnow() + timedelta(minutes=15)}},
                upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            return None
        return token if saved and saved.get("lease_token") == token else None

    def release(self, doi, token):
        self.collection.update_one({"_id": self.key(doi), "lease_token": token},
                                  {"$unset": {"lease_token": "", "lease_until": ""}})

    def save(self, result, rows, identity, token):
        query = {"_id": self.key(result["doi"]), "lease_token": token}
        checked_at = result["checked_at"]
        values = {"last_attempt": {key: result[key] for key in ("checked_at", "status", "reason",
                   "handle_http", "handle_code", "agency_http", "identity_http") if key in result}}
        if result["status"] in {"found", "missing"}:
            values.update({key: result[key] for key in ("doi", "checked_at", "status", "handle_http",
                "handle_code", "agency", "agency_http", "identity_http") if key in result})
            values["comparison_policy"] = COMPARISON_POLICY
            values["identity"] = identity if result["status"] == "found" else None
            values["legacy_matches"] = {}
            values["contexts"] = {context_id(row): {
                "local": {key: row.get(key) for key in ("title", "year", "family")},
                "comparison": result["matches"][row["_id"]], "checked_at": checked_at,
            } for row in rows}
        # A timeout while checking a new context never destroys prior evidence.
        observation = {key: result[key] for key in ("run_name", "checked_at", "status", "reason",
            "handle_http", "handle_code", "agency", "agency_http", "identity_http") if key in result}
        observation["comparison_policy"] = COMPARISON_POLICY
        self.collection.update_one(query, {"$set": values, "$min": {"first_checked_at": checked_at},
                                           "$push": {"observations": observation}})
        self.metrics["doi_remote_checks"] += 1
