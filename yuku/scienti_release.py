"""Atomic publication and conservative cleanup for final Scienti entities."""

from __future__ import annotations

from copy import deepcopy
import re
from time import time
from typing import Any, Iterable


FINAL_ENTITIES = ("works", "projects", "patents", "events")
FINAL_RELEASE_AUDITS = "scienti_final_release_audits"
FINAL_RELEASE_PUBLICATIONS = "scienti_final_release_publications"
FINAL_RELEASE_CLEANUPS = "scienti_cleanup_runs"
MEASUREMENT_RUNS = "minciencias_measurement_runs"
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")


def _name(value: str, label: str) -> str:
    value = str(value or "")
    if not NAME_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _mapping(value: dict[str, str], label: str) -> dict[str, str]:
    if set(value) != set(FINAL_ENTITIES):
        raise ValueError(f"{label} must define works, projects, patents and events")
    output = {entity: str(value[entity] or "") for entity in FINAL_ENTITIES}
    if any(not item or item.startswith("system.") for item in output.values()):
        raise ValueError(f"{label} contains an invalid collection or run")
    if len(set(output.values())) != len(output):
        raise ValueError(f"{label} values must be unique")
    return output


class ScientiFinalReleaseManager:
    """Publish four proven materializations as one indivisible release."""

    def __init__(self, db):
        self.db = db

    def publish(
        self,
        *,
        release_name: str,
        collections: dict[str, str],
        materialization_runs: dict[str, str],
        audit_name: str = "",
    ) -> dict[str, Any]:
        release_name = _name(release_name, "release_name")
        audit_name = _name(
            audit_name or f"{release_name}_audit", "audit_name"
        )
        collections = _mapping(collections, "collections")
        materialization_runs = _mapping(
            materialization_runs, "materialization_runs"
        )
        available = set(self.db.list_collection_names())
        checks: dict[str, int] = {}
        evidence: dict[str, dict[str, Any]] = {}
        for entity in FINAL_ENTITIES:
            collection = collections[entity]
            run_name = materialization_runs[entity]
            missing = int(collection not in available)
            run = self.db[MEASUREMENT_RUNS].find_one({"_id": run_name}) or {}
            summary = run.get("summary") or {}
            actual = (
                self.db[collection].count_documents({}) if not missing else 0
            )
            expected = summary.get("documents")
            if expected is None and entity == "works":
                expected = summary.get("works")
            entity_checks = {
                "missing_collection": missing,
                "missing_run": int(not run),
                "run_not_complete": int(run.get("status") != "complete"),
                "run_critical_anomalies": int(
                    summary.get("critical_anomalies") or 0
                ),
                "wrong_target_entity": int(
                    summary.get("target_entity") != entity
                ),
                "wrong_target_collection": int(
                    summary.get("collection") != collection
                ),
                "count_mismatch": int(
                    not isinstance(expected, int) or expected != actual
                ),
            }
            checks.update(
                {
                    f"{entity}_{key}": value
                    for key, value in entity_checks.items()
                }
            )
            evidence[entity] = {
                "collection": collection,
                "documents": actual,
                "materialization_run": run_name,
                "summary": deepcopy(summary),
            }
        critical = sum(checks.values())
        published_at = int(time())
        audit = {
            "_id": audit_name,
            "release_name": release_name,
            "status": "passed" if not critical else "failed",
            "audited_at": published_at,
            "collections": deepcopy(collections),
            "materialization_runs": deepcopy(materialization_runs),
            "evidence": evidence,
            "critical_counts": checks,
            "critical_anomalies": critical,
        }
        self.db[FINAL_RELEASE_AUDITS].replace_one(
            {"_id": audit_name}, audit, upsert=True
        )
        if critical:
            raise RuntimeError(f"final release audit failed: {checks}")
        publications = self.db[FINAL_RELEASE_PUBLICATIONS]
        current = publications.find_one({"_id": "current"}) or {}
        if current.get("current_release") == release_name:
            existing = publications.find_one({"_id": release_name}) or {}
            if (
                existing.get("collections") != collections
                or existing.get("materialization_runs") != materialization_runs
            ):
                raise RuntimeError(
                    "current release exists with different collections or runs"
                )
            return existing
        publication = {
            "_id": release_name,
            "status": "published",
            "published_at": published_at,
            "audit": audit_name,
            "collections": deepcopy(collections),
            "materialization_runs": deepcopy(materialization_runs),
        }
        publications.replace_one(
            {"_id": release_name}, publication, upsert=True
        )
        publications.replace_one(
            {"_id": "current"},
            {
                "_id": "current",
                "current_release": release_name,
                "previous_release": str(current.get("current_release") or ""),
                "published_at": published_at,
                "audit": audit_name,
                "collections": deepcopy(collections),
            },
            upsert=True,
        )
        return publication

    def cleanup(
        self,
        *,
        cleanup_name: str,
        release_name: str,
        candidates: Iterable[str],
        protected: Iterable[str] = (),
        reset_entity_publication: bool = True,
    ) -> dict[str, Any]:
        """Drop only named intermediates after the exact release is current."""
        cleanup_name = _name(cleanup_name, "cleanup_name")
        release_name = _name(release_name, "release_name")
        current = self.db[FINAL_RELEASE_PUBLICATIONS].find_one(
            {"_id": "current"}
        ) or {}
        if current.get("current_release") != release_name:
            raise RuntimeError("cleanup release is not the current published release")
        final_collections = set((current.get("collections") or {}).values())
        protected_names = {str(value) for value in protected if str(value)}
        candidate_names = sorted(
            {str(value) for value in candidates if str(value)}
        )
        unsafe = sorted(
            set(candidate_names) & (final_collections | protected_names)
        )
        if unsafe:
            raise ValueError(f"cleanup candidates include protected collections: {unsafe}")
        removed = []
        absent = []
        for collection in candidate_names:
            if collection not in self.db.list_collection_names():
                absent.append(collection)
                continue
            count = self.db[collection].count_documents({})
            self.db[collection].drop()
            removed.append({"collection": collection, "documents": count})
        removed_names = {value["collection"] for value in removed}
        for publication_collection in (
            "scienti_work_graph_publications",
            "scienti_final_entity_publications",
        ):
            self.db[publication_collection].update_many(
                {"previous_collection": {"$in": sorted(removed_names)}},
                {"$set": {"previous_collection": ""}},
            )
        if reset_entity_publication:
            self.db["scienti_entity_publications"].delete_one({"_id": "current"})
        result = {
            "_id": cleanup_name,
            "status": "complete",
            "release_name": release_name,
            "cleaned_at": int(time()),
            "removed": removed,
            "absent": absent,
            "retained_final_collections": sorted(final_collections),
            "protected_collections": sorted(protected_names),
            "reset_entity_publication": reset_entity_publication,
        }
        self.db[FINAL_RELEASE_CLEANUPS].replace_one(
            {"_id": cleanup_name}, result, upsert=True
        )
        return result
