from unittest.mock import patch
import os
from uuid import uuid4

import mongomock
import pytest

from yuku.minciencias_publication import (
    CATALOG, EntityRetention, adopt_published_history, assert_current_transition,
    collection_name, filtered_target, next_version, parse_name, publish_current,
    promote_release, register_release, resolve_outputs, resolve_target,
)


@pytest.fixture
def db():
    return mongomock.MongoClient().dam


def name(version, entity="works"):
    return collection_name(entity, version, "20261009")


def set_current(db, version):
    collections = {"works": name(version)}
    db.scienti_final_release_publications.replace_one({"_id": "current"},
        {"_id": "current", "current_release": "release", "audit": "audit", "collections": collections}, upsert=True)
    db.scienti_final_release_audits.replace_one({"_id": "audit"},
        {"_id": "audit", "status": "passed", "collections": collections}, upsert=True)


@pytest.mark.parametrize("entity", ["works", "person", "projects", "events", "patents", "affiliations"])
def test_canonical_names_round_trip(entity):
    assert parse_name(name(3, entity)) == {"entity": entity, "version": 3, "date": "20261009", "filtered": False}
    assert collection_name("persons", "v03", "20261009") == name(3, "person")


@pytest.mark.parametrize("value", ["minciencias_works_v3_20261009", "minciencias_person_v00_20261009",
    "minciencias_works_v01_20260230", "minciencias_projects_v01_20261009_filtered"])
def test_invalid_names_rejected(value):
    with pytest.raises(ValueError):
        parse_name(value, required=True)


def test_versions_are_independent_and_existing_builds_are_not_reused(db):
    set_current(db, 7)
    db[name(8)].insert_one({"_id": "build"})
    db[name(4, "person")].insert_one({"_id": "person"})
    targets = resolve_outputs(db, date="20261009", versions={"projects": "v12"})
    assert targets["works"] == name(9)
    assert targets["person"] == name(5, "person")
    assert targets["projects"] == name(12, "projects")
    assert targets["patents"] == name(1, "patents")
    assert next_version(db, "works") == 9


def test_resume_uses_checkpoint_version(db):
    db.scienti_person_materialization_runs.insert_one({"_id": "job", "config": {"target_collection": name(2, "person")}})
    db[name(3, "person")].insert_one({"_id": "newer"})
    assert resolve_target(db, "person", run_name="job", run_namespace="scienti_person_materialization_runs") == name(2, "person")


def test_filtered_name_is_exactly_parent_version_and_date(db):
    db.scienti_final_release_publications.insert_one({"_id": "release", "collections": {"works": name(4)}})
    assert filtered_target(db, "release") == name(4) + "_filtered"
    with pytest.raises(ValueError):
        filtered_target(db, "release", name(5) + "_filtered")


def test_late_publication_cannot_replace_newer_current(db):
    pointer = {"_id": "current", "current_collection": name(5)}
    publish_current(db, "scienti_work_graph_publications", "current", pointer)
    with pytest.raises(RuntimeError, match="regress"):
        publish_current(db, "scienti_work_graph_publications", "current", {"_id": "current", "current_collection": name(4)})
    assert db.scienti_work_graph_publications.find_one({}) == pointer
    with pytest.raises(RuntimeError):
        assert_current_transition(pointer, {"current_collection": collection_name("works", 6, "20261008")})


def test_joint_release_cannot_lose_identities():
    with pytest.raises(RuntimeError, match="lose"):
        assert_current_transition({"collections": {"works": name(4), "persons": name(2, "person")}},
                                  {"collections": {"works": name(5)}})


def test_compare_and_swap_rejects_concurrent_writer(db):
    collection = db.scienti_work_graph_publications
    collection.insert_one({"_id": "current", "current_collection": name(2)})
    replace = collection.replace_one
    def racing_writer(query, value):
        collection.update_one({"_id": "current"}, {"$set": {"current_collection": name(4)}})
        return replace(query, value)
    with patch.object(collection, "replace_one", side_effect=racing_writer):
        with pytest.raises(RuntimeError, match="concurrently"):
            publish_current(db, collection.name, "current", {"_id": "current", "current_collection": name(3)})
    assert collection.find_one({})["current_collection"] == name(4)


def register_versions(db, dependency=False):
    retention = EntityRetention(db)
    for v in (1, 2, 3):
        db[name(v)].insert_one({"_id": "work"})
        db[name(v) + "_filtered"].insert_one({"_id": "work"})
        db[f"temporary_{v}"].insert_one({"_id": "row"})
        doc = {"_id": f"run_{v}", "status": "complete"}
        if v == 3 and dependency:
            doc["source_audit"] = "run_1"
        db.scienti_doi_reconciliation_runs.insert_one(doc)
        retention.register(name(v), controls=[{"collection": "scienti_doi_reconciliation_runs", "id": f"run_{v}"}],
            temporaries=[f"temporary_{v}"], source_collections=["original_html"],
            row_controls=[{"collection": "scienti_doi_identity_checks", "owner": f"run_{v}"}])
        db.scienti_doi_identity_checks.insert_one({"_id": f"check_{v}", "run_name": f"run_{v}"})
    db.original_html.insert_one({"_id": "html"})
    set_current(db, 3)
    return retention


def test_retention_keeps_only_current_final_and_two_control_cohorts(db):
    retention = register_versions(db)
    result = retention.cleanup()
    assert result["retained_versions"] == [name(2), name(3)]
    assert set(db.scienti_doi_reconciliation_runs.distinct("_id")) == {"run_2", "run_3"}
    assert set(db.scienti_doi_identity_checks.distinct("_id")) == {"check_2", "check_3"}
    assert "temporary_1" not in db.list_collection_names()
    assert {"temporary_2", "temporary_3", "original_html", name(3), name(3) + "_filtered"} <= set(db.list_collection_names())
    assert name(1) not in db.list_collection_names() and name(2) not in db.list_collection_names()


def test_shared_proof_is_deferred_then_pruned_when_dependency_disappears(db):
    retention = register_versions(db, dependency=True)
    result = retention.cleanup()
    assert result["deferred_versions"] == [name(1)]
    assert db.scienti_doi_reconciliation_runs.find_one({"_id": "run_1"})
    assert db[CATALOG].find_one({"_id": name(1)})
    assert db.scienti_doi_identity_checks.find_one({"_id": "check_1"})
    db.scienti_doi_reconciliation_runs.update_one({"_id": "run_3"}, {"$unset": {"source_audit": ""}})
    retention.cleanup()
    assert not db.scienti_doi_reconciliation_runs.find_one({"_id": "run_1"})
    assert not db[CATALOG].find_one({"_id": name(1)})


def test_failed_audit_does_not_delete_anything(db):
    retention = register_versions(db)
    db.scienti_final_release_audits.update_one({"_id": "audit"}, {"$set": {"status": "failed"}})
    before = set(db.list_collection_names())
    with pytest.raises(RuntimeError, match="passed"):
        retention.cleanup()
    assert before - {"minciencias_publication_locks"} == set(db.list_collection_names()) - {"minciencias_publication_locks"}
    assert db.scienti_doi_identity_checks.count_documents({}) == 3


def test_retention_migrates_doi_proof_before_deleting_its_run(db):
    from datetime import datetime, timezone
    from yuku.scienti_doi_reconciliation import VERSION, digest
    from yuku.scienti_doi_history import HISTORY
    retention = register_versions(db)
    doi = "https://doi.org/10.1234/preserved"
    db.scienti_doi_identity_checks.insert_one({"_id": digest((VERSION, "run_1", doi)),
        "run_name": "run_1", "doi": doi, "status": "found", "checked_at": datetime.now(timezone.utc),
        "matches": {"context": {"confirmed": True}}})
    retention.cleanup()
    assert db[HISTORY].find_one({"doi": doi})["legacy_matches"]["context"]["confirmed"]
    assert not db.scienti_doi_identity_checks.find_one({"run_name": "run_1"})


def test_permanent_history_cannot_be_retired_as_a_temporary(db):
    from yuku.scienti_doi_history import HISTORY
    retention = register_versions(db)
    db[HISTORY].insert_one({"_id": "proof"})
    db[CATALOG].update_one({"_id": name(1)}, {"$addToSet": {"temporaries": HISTORY}})
    with pytest.raises(RuntimeError, match="permanent DOI history"):
        retention.cleanup()
    assert db[HISTORY].find_one({"_id": "proof"})


def test_unfinished_dependency_blocks_cleanup(db):
    retention = register_versions(db)
    db.scienti_person_materialization_runs.insert_one({"_id": "unfinished", "status": "running", "config": {"source": name(1)}})
    with pytest.raises(RuntimeError, match="unfinished"):
        retention.cleanup()
    assert db[name(1)].count_documents({}) == 1


def test_retention_does_not_delete_a_final_still_used_by_joint_current(db):
    retention = register_versions(db)
    retention.register(name(4))
    db[name(4)].insert_one({"_id": "new"})
    db.scienti_work_graph_publications.insert_one({"_id": "current", "current_collection": name(4)})
    with pytest.raises(RuntimeError, match="current data"):
        retention.cleanup()
    assert db[name(3)].count_documents({}) == 1


def test_legacy_adoption_does_not_rename_or_remove_data(db):
    old = "scienti_works_final_v11_20260902"
    db[old].insert_one({"_id": "work"})
    publication = {"_id": "legacy", "status": "published", "published_at": 1,
                   "collections": {"works": old}, "materialization_runs": {"works": "run"}, "audit": "legacy_audit"}
    db.scienti_final_release_publications.insert_one(publication)
    db.scienti_final_release_audits.insert_one({"_id": "legacy_audit", "status": "passed", "critical_anomalies": 0,
        "collections": publication["collections"], "materialization_runs": publication["materialization_runs"]})
    assert adopt_published_history(db) == ["legacy"]
    assert db[old].count_documents({}) == 1
    assert db[CATALOG].find_one({"_id": old})["legacy"]


@pytest.mark.parametrize("runs", [None, {}, {"works": "work_run"}, ["work_run"],
    {"works": "", "projects": "project_run"},
    {"works": "work_run", "projects": None}])
def test_history_without_complete_materialization_proof_is_preserved(db, runs):
    collections = {"works": "scienti_works_final_v5_20260901",
                   "projects": "scienti_projects_final_v5_20260901"}
    for collection in collections.values():
        db[collection].insert_one({"_id": "original"})
    publication = {"_id": "legacy_incomplete", "status": "published", "published_at": 1,
                   "audit": "legacy_audit", "collections": collections}
    audit = {"_id": "legacy_audit", "status": "passed", "critical_anomalies": 0,
             "collections": collections}
    if runs is not None:
        publication["materialization_runs"] = runs
        audit["materialization_runs"] = runs
    db.scienti_final_release_publications.insert_one(publication)
    db.scienti_final_release_audits.insert_one(audit)

    assert adopt_published_history(db) == []
    assert db[CATALOG].count_documents({}) == 0
    assert db.scienti_final_release_publications.find_one({"_id": publication["_id"]}) == publication
    assert db.scienti_final_release_audits.find_one({"_id": audit["_id"]}) == audit
    for collection in collections.values():
        assert db[collection].count_documents({}) == 1
    with pytest.raises(RuntimeError, match="complete materialization proofs"):
        register_release(db, publication)
    assert db[CATALOG].count_documents({}) == 0


def test_incomplete_history_does_not_block_registration_of_later_proven_release(db):
    historical = {"_id": "legacy", "status": "published", "published_at": 1,
                  "audit": "legacy_audit", "collections": {"works": "scienti_works_final_v5_20260901"}}
    proven = {"_id": "proven", "status": "published", "published_at": 2,
              "audit": "proven_audit", "collections": {"works": name(4)},
              "materialization_runs": {"works": "work_run"}}
    for publication in (historical, proven):
        db.scienti_final_release_publications.insert_one(publication)
        audit = {"_id": publication["audit"], "status": "passed", "critical_anomalies": 0,
                 "collections": publication["collections"]}
        if "materialization_runs" in publication:
            audit["materialization_runs"] = publication["materialization_runs"]
        db.scienti_final_release_audits.insert_one(audit)
    assert adopt_published_history(db) == ["proven"]
    assert db[CATALOG].find_one({"_id": name(4)})
    assert db[CATALOG].count_documents({}) == 1


def test_incomplete_proof_cannot_promote_even_if_audit_is_marked_passed(db):
    set_current(db, 3)
    previous = db.scienti_final_release_publications.find_one({"_id": "current"})
    release = {"_id": "incomplete", "status": "published", "entity_count": 6,
               "audit": "incomplete_audit", "collections": {"works": name(4)}}
    db.scienti_final_release_audits.insert_one({"_id": "incomplete_audit", "status": "passed",
        "critical_anomalies": 0, "collections": release["collections"]})
    with pytest.raises(RuntimeError):
        promote_release(db, release)
    assert db.scienti_final_release_publications.find_one({"_id": "current"}) == previous
    assert db[CATALOG].count_documents({}) == 0


def test_unversioned_legacy_release_does_not_claim_retention_ownership(db):
    release = {"_id": "unversioned", "status": "published", "audit": "unversioned_audit",
               "collections": {"works": "custom_work_graph"}}
    db.custom_work_graph.insert_one({"_id": "original"})
    db.scienti_final_release_audits.insert_one({"_id": "unversioned_audit", "status": "passed",
        "critical_anomalies": 0, "collections": release["collections"]})
    register_release(db, release)
    assert db[CATALOG].count_documents({}) == 0
    assert db.custom_work_graph.count_documents({}) == 1


def test_retention_uses_immediate_previous_existing_version(db):
    retention = EntityRetention(db)
    for version in (1, 8, 14):
        retention.register(name(version))
    set_current(db, 14)
    assert retention.plan()["retained_versions"] == [name(8), name(14)]


@pytest.mark.skipif(not os.getenv("YUKU_IDENTIFIED_TEST_URI"), reason="Opt-in isolated real Mongo integration")
def test_real_mongo_current_guard_and_scoped_retention():
    from pymongo import MongoClient
    client = MongoClient(os.environ["YUKU_IDENTIFIED_TEST_URI"], serverSelectionTimeoutMS=5000)
    database_name = "yuku_publication_test_" + uuid4().hex
    database = client[database_name]
    try:
        retention = register_versions(database)
        retention.cleanup()
        assert database.scienti_doi_identity_checks.count_documents({"run_name": "run_1"}) == 0
        assert database.scienti_doi_identity_checks.count_documents({"run_name": "run_2"}) == 1
        assert database.original_html.count_documents({}) == 1
        namespace = "scienti_work_graph_publications"
        publish_current(database, namespace, "current", {"_id": "current", "current_collection": name(3)})
        with pytest.raises(RuntimeError):
            publish_current(database, namespace, "current", {"_id": "current", "current_collection": name(2)})
        assert database[namespace].find_one({"_id": "current"})["current_collection"] == name(3)
    finally:
        client.drop_database(database_name)
        client.close()


def test_entity_normalizations_reached_through_graph_collections_are_temporaries(db):
    # 20261017: the four scienti_*_normalized_* collections were never retired.
    projects = collection_name("projects", 4, "20261009")
    normalized = {entity: f"scienti_{entity}_normalized_r" for entity in ("works", "projects", "patents", "events")}
    for collection in [projects, "scienti_projects_graph_r", *normalized.values()]:
        db[collection].insert_one({"_id": "row"})
    db.minciencias_measurement_runs.insert_one({"_id": "projects_final", "status": "complete", "config": {
        "graph_collection": "scienti_projects_graph_r", "target_collection": projects}})
    db.scienti_entity_graph_runs.insert_one({"_id": "r_projects_graph", "status": "complete", "config": {
        "entity_run_name": "r_entities", "source_collection": normalized["projects"],
        "target_collection": "scienti_projects_graph_r"}})
    db.scienti_entity_normalization_runs.insert_one({"_id": "r_entities", "status": "complete",
                                                     "config": {"destinations": normalized}})
    release = {"_id": "release_r", "status": "published", "published_at": 1, "audit": "release_r_audit",
               "collections": {"projects": projects}, "materialization_runs": {"projects": "projects_final"}}
    db.scienti_final_release_audits.insert_one({"_id": "release_r_audit", "status": "passed",
        "critical_anomalies": 0, "collections": release["collections"],
        "materialization_runs": release["materialization_runs"]})
    register_release(db, release)
    record = db[CATALOG].find_one({"_id": projects})
    assert set(normalized.values()) | {"scienti_projects_graph_r"} <= set(record["temporaries"])
    assert not set(normalized.values()) & set(record["source_collections"])
