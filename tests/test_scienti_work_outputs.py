from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from unittest.mock import patch
from uuid import uuid4

import mongomock
import pytest
from pymongo import MongoClient

from yuku.Yuku import Yuku
from yuku.scienti_release import ScientiFinalReleaseManager
from yuku.scienti_work_outputs import ScientiWorkOutputs


@pytest.fixture
def db():
    db = mongomock.MongoClient().dam
    collections = {key: "new_" + key for key in (
        "works", "persons", "affiliations", "projects", "patents", "events")}
    for name in (*collections.values(), "old_works", "old_identified", "new_identified", "base", "measured", "links", "raw_html"):
        db[name].insert_one({"_id": 1})
    db.scienti_final_release_publications.insert_many([
        {"_id": "old_release", "status": "published", "collections": {"works": "old_works", "persons": "old_persons"}},
        {"_id": "current", "current_release": "old_release"},
        {"_id": "new_release", "status": "published", "entity_count": 6,
         "collections": collections, "audit": "release_audit", "published_at": 123,
         "materialization_runs": {"works": "final_run", "persons": "persons_run"}},
    ])
    db.scienti_final_release_audits.insert_one({
        "_id": "release_audit", "status": "passed", "critical_anomalies": 0,
        "release_name": "new_release", "collections": collections,
        "materialization_runs": {"works": "final_run", "persons": "persons_run"},
    })
    db.scienti_work_graph_publications.insert_many([
        {"_id": "current", "current_collection": "old_works"},
        {"_id": "old_run", "status": "published", "collection": "old_works"},
        {"_id": "graph_run", "status": "published", "collection": "base"},
        {"_id": "final_run", "status": "published", "collection": "new_works", "documents": 1},
    ])
    db.minciencias_measurement_runs.insert_one({"_id": "final_run", "status": "complete",
        "config": {"graph_collection": "base", "measured_collection": "measured", "links_collection": "links"}})
    db.scienti_person_publications.insert_one({"_id": "persons_run", "status": "published",
        "collection": "new_persons", "published_at": 123, "audit": "person_audit"})
    config = {"target": "new_identified", "source": {"release_name": "new_release",
              "collections": collections, "counts": {"works": 1, "persons": 1}}}
    db.scienti_identified_work_publications.insert_many([
        {"_id": "new_identified", "release_name": "new_release", "audit": "identified_audit", "run_name": "identified_run", "documents": 1},
        {"_id": "old_identified", "release_name": "old_release", "audit": "old_identified_audit", "documents": 1},
    ])
    db.scienti_identified_work_audits.insert_many([
        {"_id": "identified_audit", "status": "passed", "config": config, "documents": 1, "documents_digest": "proof"},
        {"_id": "old_identified_audit", "config": {"source": {"collections": {"works": "old_works"}}}},
    ])
    db.scienti_identified_work_runs.insert_one({"_id": "identified_run", "status": "complete",
        "config": config, "documents_digest": "proof"})
    return db


def arguments():
    return {"cleanup_name": "work_outputs", "source_release": "old_release",
            "release_name": "new_release", "identified": "new_identified",
            "intermediates": ["base", "measured", "links"]}


def test_plan_is_read_only_and_finalization_leaves_only_two_work_datasets(db):
    outputs = ScientiWorkOutputs(db)
    before = set(db.list_collection_names())
    assert outputs.plan(**arguments())["candidates"] == ["base", "links", "measured", "old_identified", "old_works"]
    assert set(db.list_collection_names()) == before
    result = outputs.finalize(**arguments())
    assert result["retained_work_collections"] == ["new_identified", "new_works"]
    assert before - set(db.list_collection_names()) == set(arguments()["intermediates"]) | {"old_works", "old_identified"}
    assert db.raw_html.count_documents({}) == 1
    assert db.scienti_final_release_publications.find_one({"_id": "current"})["current_release"] == "new_release"
    pair = db.scienti_identified_work_publications.find_one({"_id": "current"})
    assert (pair["full_graph"], pair["collection"]) == ("new_works", "new_identified")
    assert db.scienti_work_graph_publications.find_one({"_id": "old_run"})["artifact_state"] == "retired"
    assert db.scienti_final_release_audits.count_documents({}) == 1
    assert outputs.finalize(**arguments()) == result


@pytest.mark.parametrize("damage", ["audit", "collection", "count", "source", "current"])
def test_failed_proof_never_changes_pointers_or_deletes_previous_data(db, damage):
    if damage == "audit":
        db.scienti_identified_work_audits.update_one({"_id": "identified_audit"}, {"$set": {"status": "failed"}})
    elif damage == "collection":
        db.new_identified.drop()
    elif damage == "count":
        db.new_identified.insert_one({"_id": 2})
    elif damage == "source":
        db.scienti_identified_work_audits.update_one({"_id": "identified_audit"}, {"$set": {"config.source.release_name": "unrelated"}})
    else:
        db.scienti_final_release_publications.update_one({"_id": "current"}, {"$set": {"current_release": "concurrent"}})
    before = deepcopy(db.scienti_final_release_publications.find_one({"_id": "current"}))
    with pytest.raises(RuntimeError):
        ScientiWorkOutputs(db).finalize(**arguments())
    assert db.scienti_final_release_publications.find_one({"_id": "current"}) == before
    assert db.old_works.count_documents({}) == db.old_identified.count_documents({}) == 1


def test_cleanup_can_resume_after_pointer_promotion_before_drop(db):
    outputs = ScientiWorkOutputs(db)
    with patch.object(ScientiFinalReleaseManager, "cleanup", side_effect=RuntimeError("interruption")):
        with pytest.raises(RuntimeError, match="interruption"):
            outputs.finalize(**arguments())
    assert db.old_works.count_documents({}) == 1
    assert db.scienti_final_release_publications.find_one({"_id": "current"})["current_release"] == "new_release"
    assert outputs.finalize(**arguments())["status"] == "complete"


def test_cleanup_resumes_after_a_partial_drop(db):
    outputs = ScientiWorkOutputs(db)
    def interrupted(*args, **kwargs):
        db.base.drop()
        raise RuntimeError("partial drop")
    with patch.object(ScientiFinalReleaseManager, "cleanup", side_effect=interrupted):
        with pytest.raises(RuntimeError, match="partial drop"):
            outputs.finalize(**arguments())
    result = outputs.finalize(**arguments())
    assert result["status"] == "complete"
    assert "base" in result["cleanup"]["absent"]
    assert db.new_works.count_documents({}) == db.new_identified.count_documents({}) == 1


def test_independent_selector_cannot_overwrite_pair_pointer(db):
    with pytest.raises(ValueError, match="reserved"):
        Yuku.__new__(Yuku).create_scienti_identified_works(
            release_name="release", run_name="run", target_collection="current")


def test_never_drop_an_unclassified_graph_or_raw_collection(db):
    db.scienti_works_unknown.insert_one({"_id": 1})
    with pytest.raises(RuntimeError, match="unclassified"):
        ScientiWorkOutputs(db).finalize(**arguments())
    db.scienti_works_unknown.drop()
    args = {**arguments(), "intermediates": ["raw_html"]}
    with pytest.raises(RuntimeError, match="proven intermediates"):
        ScientiWorkOutputs(db).finalize(**args)
    assert db.raw_html.count_documents({}) == 1


def graph_controls_fixture(db):
    base = "scienti_works_base_test"
    db.base.rename(base)
    db.minciencias_measurement_runs.update_one({"_id": "final_run"},
        {"$set": {"config.graph_collection": base}})
    db.scienti_work_graph_publications.update_one({"_id": "graph_run"},
        {"$set": {"collection": base}})
    db[base + "_graph_runs"].insert_one({"_id": "graph_run", "status": "complete",
        "config": {"target_collection": base}})
    db[base + "_graph_review"].insert_one({"_id": "candidate", "run_id": "graph_run"})
    return {**arguments(), "intermediates": [base, "measured", "links"]}, base


def test_published_graph_controls_are_preserved_and_not_counted_as_work_datasets(db):
    args, base = graph_controls_fixture(db)
    outputs = ScientiWorkOutputs(db)
    result = outputs.finalize(**args)
    assert result["status"] == "complete"
    assert base not in db.list_collection_names()
    assert db[base + "_graph_runs"].count_documents({}) == 1
    assert db[base + "_graph_review"].count_documents({}) == 1
    assert outputs.finalize(**args) == result


@pytest.mark.parametrize("suffix", ["_graph_runs", "_graph_review"])
def test_an_unknown_collection_cannot_hide_behind_a_control_suffix(db, suffix):
    db["scienti_works_unknown" + suffix].insert_one({"_id": "unknown"})
    with pytest.raises(RuntimeError, match="unclassified"):
        ScientiWorkOutputs(db).finalize(**arguments())
    assert db.old_works.count_documents({}) == 1


def test_mismatched_checkpoint_does_not_exempt_graph_controls(db):
    args, base = graph_controls_fixture(db)
    db[base + "_graph_runs"].update_one({}, {"$set": {"config.target_collection": "unrelated"}})
    with pytest.raises(RuntimeError, match="unclassified"):
        ScientiWorkOutputs(db).finalize(**args)


def test_a_published_output_always_counts_even_with_a_control_suffix(db):
    args, base = graph_controls_fixture(db)
    db.scienti_work_graph_publications.insert_one({"_id": "extra_graph", "status": "published",
        "collection": base + "_graph_review"})
    with pytest.raises(RuntimeError, match="unclassified"):
        ScientiWorkOutputs(db).finalize(**args)


def test_incomplete_other_run_blocks_retirement(db):
    db.scienti_identified_work_runs.insert_one({"_id": "unfinished", "status": "failed",
        "config": {"source": {"release_name": "old_release", "collections": {"works": "old_works"}}}})
    with pytest.raises(RuntimeError, match="unfinished run"):
        ScientiWorkOutputs(db).finalize(**arguments())
    assert db.old_works.count_documents({}) == 1


def test_exclusive_promotion_does_not_release_another_workers_lock(db):
    db.scienti_cleanup_runs.insert_one({"_id": "scienti_work_outputs_lock", "owner": "another",
        "lease_until": datetime.now(timezone.utc) + timedelta(minutes=15)})
    with pytest.raises(RuntimeError, match="another work-output"):
        ScientiWorkOutputs(db).finalize(**arguments())
    assert db.scienti_cleanup_runs.find_one({"_id": "scienti_work_outputs_lock"})["owner"] == "another"
    assert db.old_works.count_documents({}) == 1


@pytest.mark.skipif(not os.getenv("YUKU_IDENTIFIED_TEST_URI"), reason="Opt-in real Mongo")
@pytest.mark.parametrize("with_controls", [False, True])
def test_real_mongo_pair_promotion_and_retirement(db, with_controls):
    args, base = graph_controls_fixture(db) if with_controls else (arguments(), "base")
    client = MongoClient(os.environ["YUKU_IDENTIFIED_TEST_URI"], serverSelectionTimeoutMS=5000)
    isolated = client["yuku_work_outputs_test_" + uuid4().hex]
    try:
        for name in db.list_collection_names():
            documents = list(db[name].find({}))  # tiny synthetic fixture only
            if documents:
                isolated[name].insert_many(documents)
        outputs = ScientiWorkOutputs(isolated)
        result = outputs.finalize(**args)
        assert result["status"] == "complete"
        assert not {"old_works", "old_identified", base, "measured", "links"} & set(isolated.list_collection_names())
        assert isolated.raw_html.count_documents({}) == 1
        if with_controls:
            assert isolated[base + "_graph_runs"].count_documents({}) == 1
            assert isolated[base + "_graph_review"].count_documents({}) == 1
        assert outputs.finalize(**args) == result
    finally:
        client.drop_database(isolated.name)
        client.close()
