"""Atomic publication and conservative cleanup for final Scienti entities."""

from __future__ import annotations

from copy import deepcopy
import re
from time import time
from typing import Any, Iterable

from pymongo import UpdateOne

from yuku.scienti_persons import canonical_doi, cod_rh


CORE_ENTITIES = ("works", "projects", "patents", "events")
IDENTITY_ENTITIES = ("persons", "affiliations")
FINAL_ENTITIES = CORE_ENTITIES + IDENTITY_ENTITIES
FINAL_RELEASE_AUDITS = "scienti_final_release_audits"
FINAL_RELEASE_PUBLICATIONS = "scienti_final_release_publications"
FINAL_RELEASE_CLEANUPS = "scienti_cleanup_runs"
MEASUREMENT_RUNS = "minciencias_measurement_runs"
PERSON_RUNS = "scienti_person_materialization_runs"
PERSON_AUDITS = "scienti_person_materialization_audits"
PERSON_PUBLICATIONS = "scienti_person_publications"
AFFILIATION_RUNS = "scienti_affiliation_materialization_runs"
AFFILIATION_AUDITS = "scienti_affiliation_materialization_audits"
AFFILIATION_PUBLICATIONS = "scienti_affiliation_publications"
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
COLLECTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,119}")
GROUP_RE = re.compile(r"COL\d{7}")


def _name(value: str, label: str) -> str:
    value = str(value or "")
    if not NAME_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _mapping(
    value: dict[str, str], label: str, entities: tuple[str, ...]
) -> dict[str, str]:
    if set(value) != set(entities):
        raise ValueError(f"{label} must define {', '.join(entities)}")
    output = {entity: str(value[entity] or "") for entity in entities}
    if any(
        not COLLECTION_RE.fullmatch(item) or item.startswith("system.")
        for item in output.values()
    ):
        raise ValueError(f"{label} contains an invalid collection or run")
    if len(set(output.values())) != len(output):
        raise ValueError(f"{label} values must be unique")
    return output


class ScientiFinalReleaseManager:
    """Publish proven core or Kahi-ready snapshots through an atomic pointer."""

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
        collections = _mapping(collections, "collections", CORE_ENTITIES)
        materialization_runs = _mapping(
            materialization_runs, "materialization_runs", CORE_ENTITIES
        )
        available = set(self.db.list_collection_names())
        checks: dict[str, int] = {}
        evidence: dict[str, dict[str, Any]] = {}
        for entity in CORE_ENTITIES:
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

    def _core_release(self, release_name: str) -> dict[str, Any]:
        release = self.db[FINAL_RELEASE_PUBLICATIONS].find_one(
            {"_id": release_name}
        ) or {}
        audit = self.db[FINAL_RELEASE_AUDITS].find_one(
            {"_id": release.get("audit")}
        ) or {}
        collections = release.get("collections") or {}
        runs = release.get("materialization_runs") or {}
        if (
            release.get("status") != "published"
            or set(collections) != set(CORE_ENTITIES)
            or set(runs) != set(CORE_ENTITIES)
            or audit.get("status") != "passed"
            or audit.get("release_name") != release_name
            or int(audit.get("critical_anomalies") or 0)
            or audit.get("collections") != collections
            or audit.get("materialization_runs") != runs
        ):
            raise RuntimeError("base four-entity release is not proven and published")
        available = set(self.db.list_collection_names())
        validated_documents: dict[str, int] = {}
        for entity in CORE_ENTITIES:
            evidence = (audit.get("evidence") or {}).get(entity) or {}
            collection = collections[entity]
            documents = self.db[collection].count_documents({}) if collection in available else -1
            if (
                collection not in available
                or evidence.get("collection") != collection
                or documents != int(evidence.get("documents") or -1)
            ):
                raise RuntimeError("base release collection changed after its audit")
            validated_documents[entity] = documents
        release["_validated_documents"] = validated_documents
        return release

    def _identity_release(
        self, entity: str, run_name: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if entity == "persons":
            names = (PERSON_RUNS, PERSON_AUDITS, PERSON_PUBLICATIONS)
        elif entity == "affiliations":
            names = (AFFILIATION_RUNS, AFFILIATION_AUDITS, AFFILIATION_PUBLICATIONS)
        else:
            raise ValueError("invalid identity entity")
        run = self.db[names[0]].find_one({"_id": run_name}) or {}
        publication = self.db[names[2]].find_one({"_id": run_name}) or {}
        audit = self.db[names[1]].find_one({"_id": publication.get("audit")}) or {}
        summary = run.get("summary") or {}
        collection = str(publication.get("collection") or "")
        actual = (
            self.db[collection].count_documents({})
            if collection in self.db.list_collection_names() else -1
        )
        if (
            run.get("status") != "complete"
            or summary.get("collection") != collection
            or summary.get("audit") != audit.get("_id")
            or publication.get("status") != "published"
            or audit.get("status") != "passed"
            or audit.get("collection") != collection
            or int(audit.get("critical_anomalies") or 0)
            or len({summary.get("documents"), publication.get("documents"),
                    audit.get("documents"), actual}) != 1
        ):
            raise RuntimeError(f"{entity} snapshot is not proven and published")
        return run, publication, audit

    @staticmethod
    def _bulk_dois(collection, dois: list[str]) -> None:
        if not dois:
            return
        operations = [
            UpdateOne({"_id": doi}, {"$setOnInsert": {"seen": True}}, upsert=True)
            for doi in set(dois)
        ]
        try:
            collection.bulk_write(operations, ordered=False)
        except TypeError:  # Compatibility with older mongomock/PyMongo adapters.
            for doi in set(dois):
                collection.update_one(
                    {"_id": doi}, {"$setOnInsert": {"seen": True}}, upsert=True
                )

    @staticmethod
    def _example(examples: dict[str, list[Any]], key: str, value: Any) -> None:
        if len(examples.setdefault(key, [])) < 20:
            examples[key].append(value)

    def _validate_references(
        self,
        release_name: str,
        collections: dict[str, str],
        *,
        batch_size: int,
        progress_every: int,
    ) -> tuple[dict[str, int], dict[str, list[Any]], dict[str, int]]:
        checks: dict[str, int] = {}
        examples: dict[str, list[Any]] = {}
        metrics = {"persons_scanned": 0, "related_dois": 0, "entities_scanned": 0}
        affiliation_ids = {
            str(item["_id"])
            for item in self.db[collections["affiliations"]].find({}, {"_id": 1})
        }
        person_ids: set[str] = set()
        temp_name = f"__yuku_{release_name}_reference_dois"
        dois = self.db[temp_name]
        dois.drop()
        pending: list[str] = []
        try:
            for person in self.db[collections["persons"]].find(
                {}, {"external_ids": 1, "affiliations.id": 1, "related_works": 1}
            ):
                metrics["persons_scanned"] += 1
                raw_identifier = person.get("_id")
                identifier = str(raw_identifier or "")
                if not isinstance(raw_identifier, str) or not cod_rh(identifier):
                    key = "persons_invalid_ids"
                    checks[key] = checks.get(key, 0) + 1
                    self._example(examples, key, identifier)
                else:
                    person_ids.add(identifier)
                external_codes = {
                    value.get("id", {}).get("COD_RH")
                    for value in person.get("external_ids", [])
                    if isinstance(value.get("id"), dict)
                }
                if identifier not in external_codes:
                    key = "persons_external_cod_rh_mismatch"
                    checks[key] = checks.get(key, 0) + 1
                    self._example(examples, key, identifier)
                for affiliation in person.get("affiliations", []) or []:
                    target = str(affiliation.get("id") or "")
                    if target and target not in affiliation_ids:
                        key = "persons_unresolved_affiliations"
                        checks[key] = checks.get(key, 0) + 1
                        self._example(examples, key, {"person": identifier, "id": target})
                seen = set()
                for related in person.get("related_works", []) or []:
                    doi = canonical_doi(related.get("id"))
                    if (
                        related.get("provenance") != "minciencias"
                        or related.get("source") != "doi"
                        or not doi
                        or set(related) - {
                            "provenance", "source", "id", "author_count"
                        }
                    ):
                        key = "persons_invalid_related_works"
                        checks[key] = checks.get(key, 0) + 1
                        self._example(examples, key, identifier)
                        continue
                    if doi in seen:
                        key = "persons_duplicate_related_dois"
                        checks[key] = checks.get(key, 0) + 1
                        self._example(examples, key, {"person": identifier, "doi": doi})
                        continue
                    seen.add(doi)
                    pending.append(doi)
                    if len(pending) >= batch_size:
                        self._bulk_dois(dois, pending)
                        pending.clear()
                if metrics["persons_scanned"] % progress_every == 0:
                    print(
                        f"INFO: six-entity reference audit persons="
                        f"{metrics['persons_scanned']}", flush=True
                    )
            self._bulk_dois(dois, pending)
            metrics["related_dois"] = dois.count_documents({})

            for entity in CORE_ENTITIES:
                projection = {"authors.id": 1, "groups.id": 1}
                if entity == "works":
                    projection["doi"] = 1
                doi_batch: list[str] = []
                scanned = 0
                for item in self.db[collections[entity]].find({}, projection):
                    scanned += 1
                    metrics["entities_scanned"] += 1
                    for author in item.get("authors", []) or []:
                        raw_value = author.get("id")
                        raw = str(raw_value or "")
                        if not raw:
                            continue
                        if not isinstance(raw_value, str) or not cod_rh(raw):
                            key = f"{entity}_invalid_author_references"
                        elif raw not in person_ids:
                            key = f"{entity}_unresolved_author_references"
                        else:
                            continue
                        checks[key] = checks.get(key, 0) + 1
                        self._example(examples, key, {"entity": item["_id"], "id": raw})
                    for group in item.get("groups", []) or []:
                        raw_value = group.get("id")
                        raw = str(raw_value or "")
                        if not raw:
                            continue
                        if not isinstance(raw_value, str) or not GROUP_RE.fullmatch(raw):
                            key = f"{entity}_invalid_group_references"
                        elif raw not in affiliation_ids:
                            key = f"{entity}_unresolved_group_references"
                        else:
                            continue
                        checks[key] = checks.get(key, 0) + 1
                        self._example(examples, key, {"entity": item["_id"], "id": raw})
                    if entity == "works":
                        doi = canonical_doi(item.get("doi"))
                        if item.get("doi") and not doi:
                            key = "works_excluded_invalid_dois"
                            metrics[key] = metrics.get(key, 0) + 1
                            self._example(examples, key, item["_id"])
                        elif doi:
                            doi_batch.append(doi)
                        if len(doi_batch) >= batch_size:
                            dois.delete_many({"_id": {"$in": list(set(doi_batch))}})
                            doi_batch.clear()
                    if scanned % progress_every == 0:
                        print(
                            f"INFO: six-entity reference audit {entity}={scanned}",
                            flush=True,
                        )
                if doi_batch:
                    dois.delete_many({"_id": {"$in": list(set(doi_batch))}})
                metrics[f"{entity}_documents"] = scanned
            missing_dois = dois.count_documents({})
            if missing_dois:
                checks["persons_unresolved_related_dois"] = missing_dois
                examples["persons_unresolved_related_dois"] = [
                    item["_id"] for item in dois.find({}, {"_id": 1}).limit(20)
                ]
        finally:
            dois.drop()
        return checks, examples, metrics

    def publish_with_identities(
        self,
        *,
        release_name: str,
        base_release_name: str,
        person_run_name: str,
        affiliation_run_name: str,
        audit_name: str = "",
        batch_size: int = 1000,
        progress_every: int = 100000,
    ) -> dict[str, Any]:
        """Validate all cross-references and atomically publish six entities."""
        release_name = _name(release_name, "release_name")
        base_release_name = _name(base_release_name, "base_release_name")
        person_run_name = _name(person_run_name, "person_run_name")
        affiliation_run_name = _name(affiliation_run_name, "affiliation_run_name")
        audit_name = _name(audit_name or f"{release_name}_audit", "audit_name")
        if batch_size < 1 or progress_every < 1:
            raise ValueError("batch_size and progress_every must be positive")
        if release_name == base_release_name:
            raise ValueError("six-entity release must differ from its base release")
        existing_audit = self.db[FINAL_RELEASE_AUDITS].find_one(
            {"_id": audit_name}
        ) or {}
        if existing_audit and existing_audit.get("release_name") != release_name:
            raise RuntimeError("release audit name already belongs to another release")
        base = self._core_release(base_release_name)
        person_run, person_publication, person_audit = self._identity_release(
            "persons", person_run_name
        )
        _, affiliation_publication, _ = self._identity_release(
            "affiliations", affiliation_run_name
        )
        person_config = person_run.get("config") or {}
        source_release = (person_audit.get("source_proofs") or {}).get("release") or {}
        if (
            person_config.get("final_release_name") != base_release_name
            or person_config.get("affiliation_run_name") != affiliation_run_name
            or person_config.get("affiliation_collection")
            != affiliation_publication.get("collection")
            or source_release.get("name") != base_release_name
            or source_release.get("collections") != base.get("collections")
            or source_release.get("documents") != base.get("_validated_documents")
        ):
            raise RuntimeError("person snapshot does not prove the selected dependencies")
        collections = {
            **deepcopy(base["collections"]),
            "persons": person_publication["collection"],
            "affiliations": affiliation_publication["collection"],
        }
        runs = {
            **deepcopy(base["materialization_runs"]),
            "persons": person_run_name,
            "affiliations": affiliation_run_name,
        }
        collections = _mapping(collections, "collections", FINAL_ENTITIES)
        runs = _mapping(runs, "materialization_runs", FINAL_ENTITIES)
        expected_documents = {
            **base["_validated_documents"],
            "persons": int(person_publication["documents"]),
            "affiliations": int(affiliation_publication["documents"]),
        }
        reference_checks, examples, metrics = self._validate_references(
            release_name, collections,
            batch_size=batch_size, progress_every=progress_every,
        )
        all_checks = dict(reference_checks)
        for key in (
            "persons_invalid_ids", "persons_external_cod_rh_mismatch",
            "persons_unresolved_affiliations", "persons_invalid_related_works",
            "persons_duplicate_related_dois", "persons_unresolved_related_dois",
        ):
            all_checks.setdefault(key, 0)
        for entity in CORE_ENTITIES:
            for suffix in (
                "invalid_author_references", "unresolved_author_references",
                "invalid_group_references", "unresolved_group_references",
            ):
                all_checks.setdefault(f"{entity}_{suffix}", 0)
        quality_counts = {
            "excluded_invalid_work_dois": int(
                metrics.get("works_excluded_invalid_dois") or 0
            ),
        }
        evidence = {
            entity: {
                "collection": collection,
                "documents": self.db[collection].count_documents({}),
                "materialization_run": runs[entity],
            }
            for entity, collection in collections.items()
        }
        for entity in FINAL_ENTITIES:
            observed = int(evidence[entity]["documents"])
            all_checks[f"{entity}_count_changed_during_audit"] = int(
                observed != expected_documents[entity]
            )
        critical = sum(all_checks.values())
        published_at = int(time())
        audit = {
            "_id": audit_name, "release_name": release_name,
            "base_release_name": base_release_name,
            "status": "passed" if not critical else "failed",
            "audited_at": published_at, "collections": deepcopy(collections),
            "materialization_runs": deepcopy(runs), "evidence": evidence,
            "reference_metrics": metrics,
            "critical_counts": dict(sorted(all_checks.items())),
            "critical_anomalies": critical,
            "quality_counts": quality_counts,
            "examples": examples,
        }
        self.db[FINAL_RELEASE_AUDITS].replace_one(
            {"_id": audit_name}, audit, upsert=True
        )
        if critical:
            raise RuntimeError(f"six-entity release audit failed: {all_checks}")
        publications = self.db[FINAL_RELEASE_PUBLICATIONS]
        existing = publications.find_one({"_id": release_name}) or {}
        if existing and (
            existing.get("status") != "published"
            or existing.get("entity_count") != 6
            or existing.get("audit") != audit_name
            or existing.get("collections") != collections
            or existing.get("materialization_runs") != runs
            or existing.get("base_release_name") != base_release_name
        ):
            raise RuntimeError("immutable six-entity release already exists differently")
        current = publications.find_one({"_id": "current"}) or {}
        publication = existing or {
            "_id": release_name, "status": "published", "entity_count": 6,
            "published_at": published_at, "audit": audit_name,
            "base_release_name": base_release_name,
            "collections": deepcopy(collections),
            "materialization_runs": deepcopy(runs),
        }
        if not existing:
            publications.insert_one(publication)
        if current.get("current_release") != release_name:
            publications.replace_one({"_id": "current"}, {
                "_id": "current", "current_release": release_name,
                "previous_release": str(current.get("current_release") or ""),
                "published_at": published_at, "audit": audit_name,
                "entity_count": 6, "collections": deepcopy(collections),
            }, upsert=True)
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
