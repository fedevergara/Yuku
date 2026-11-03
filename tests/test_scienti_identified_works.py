"""Identifier policy and recovery tests, independent of optional NLP packages."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from unittest.mock import patch
from uuid import uuid4

from bson import ObjectId
import mongomock
import pytest

from yuku.scienti_identified_works import (
    AUDITS, FIELDS, PUBLICATIONS, RELEASE_AUDITS, RELEASES, RUNS,
    ScientiIdentifiedWorkSelector, WorkIdentifierPolicy,
    _resume_query, _shared_helpers, valid_isbn, valid_issn,
)


# The full schema lets unit tests run without installing Kahi's optional stack.
# The integration test below checks the actual runtime empty_work template.
SCHEMA = dict.fromkeys([
    "titles", "updated", "doi", "abstracts", "keywords", "types", "external_ids",
    "external_urls", "date_published", "year_published", "bibliographic_info",
    "open_access", "apc", "references_count", "references", "citations_count",
    "citations", "author_count", "authors", "source", "ranking", "subjects",
    "citations_by_year", "groups", "rights", "primary_topic", "topics",
])
RH = "0000164771"
ISBN = "9780306406157"
DOI = "https://doi.org/10.1234/abc"


def canonical(value):
    found = re.search(r"10\.[0-9]{4,9}/\S+", value)
    return "https://doi.org/" + found[0].lower() if found else None


def ordered(document, template):
    return {key: document[key] for key in ("_id", *template) if key in document}


@pytest.fixture
def policy():
    return WorkIdentifierPolicy(helpers=(canonical, ordered, SCHEMA, {"fixture": "v1"}))


def ext(family, value):
    return {"source": family, "provenance": "minciencias", "id": value}


def pair(product="72", rh=RH):
    return [ext("scienti", {"COD_RH": rh, "COD_PRODUCTO": product}),
            ext("minciencias", f"ART-{rh}-{product}")]


@pytest.mark.parametrize("raw,expected", [
    ("doi:10.1234/ABC", DOI), ("http://dx.doi.org/10.1234/ABC", DOI),
    ("10.1234/a", "https://doi.org/10.1234/a"), ("0.1234/abc", None),
    ("101234/abc", None), ("10.0000/abc", None), ("10.1234/none", None),
    ("10.1234/0000", None), ("10.1234/abc?download=1", None),
    ("10.1234/abc#page", None), ("10.1234/ab c", None),
    ("10.1234/abc\x00junk", None), ("text 10.1234/abc", None),
    ("10.1234%2fabc", None), (None, None), (1234, None),
])
def test_doi_local_validation(policy, raw, expected):
    assert policy.doi(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("978-0-306-40615-7", ISBN), ("0-306-40615-2", "0306406152"),
    ("0-8044-2957-X", "080442957X"), ("9780306406158", None),
    ("0000000000", None), ("4006381333931", None),
    ("ISBN 9780306406157", None), (9780306406157, None), (None, None),
])
def test_isbn_checksum_and_namespace(raw, expected):
    assert valid_isbn(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2049 3630", "2049-3630"), ("2434-561x", "2434-561X"),
    ("2049-3631", None), ("0000-0000", None), (None, None),
])
def test_issn_metadata_validation(raw, expected):
    assert valid_issn(raw) == expected


def test_pair_requires_same_element_anchor_and_real_person(policy):
    good = {"external_ids": pair()}
    assert policy.evaluate(good, {RH})["eligible"]
    assert not policy.evaluate(good, set())["eligible"]
    crossed = {"external_ids": [ext("scienti", {"COD_RH": RH}),
                                ext("scienti", {"COD_PRODUCTO": "72"}), *pair()[1:]]}
    assert not policy.evaluate(crossed, {RH})["eligible"]
    no_anchor = {"external_ids": pair("73")[:1],
                 "bibliographic_info": {"minciencias": {"product_ids": [f"ART-{RH}-72"]}}}
    assert not policy.evaluate(no_anchor, {RH})["eligible"]
    anchored = deepcopy(no_anchor)
    anchored["bibliographic_info"]["minciencias"]["product_ids"] = [f"ART-{RH}-73"]
    assert policy.evaluate(anchored, {RH})["eligible"]


@pytest.mark.parametrize("rh,product", [(RH, 72), (123, "72"), ("0000000000", "72"),
                                       (RH, "1;2"), ("164771", "72")])
def test_pair_rejects_repaired_or_combined_codes(policy, rh, product):
    assert not policy.evaluate({"external_ids": pair(product, rh)}, {RH})["eligible"]


def test_sparse_order_invalid_ids_and_author_preservation(policy):
    original = {"_id": "a", "updated": [{"source": "scienti", "time": 1}],
                "titles": [{"title": "A chapter", "lang": ""}], "doi": "bad",
                "external_ids": [ext("isbn", ISBN), ext("doi", "bad"),
                                 ext("issn", "2049-3630"), ext("title_fingerprint", "abc")],
                "types": [{"source": "impactu", "type": "Capitulo de libro"}],
                "bibliographic_info": {"book_title": "Book", "volume": ""},
                "authors": [{"full_name": "Ana", "external_ids": []}], "author_count": 0,
                "source": {"name": "Book", "external_ids": [ext("isbn", ISBN), ext("issn", "bad")]},
                "references": [], "keywords": [], "custom": 123}
    snapshot = deepcopy(original)
    decision = policy.evaluate(original, set())
    actual = policy.project(original, decision)
    assert list(actual) == ["_id", "titles", "updated", "types", "external_ids",
                            "bibliographic_info", "author_count", "authors", "source"]
    assert actual["author_count"] == 0
    assert actual["authors"] == [{"full_name": "Ana"}]
    assert [entry["source"] for entry in actual["external_ids"]] == ["isbn", "issn"]
    assert actual["source"]["external_ids"] == [ext("isbn", ISBN)]
    assert actual["bibliographic_info"] == {"book_title": "Book"}
    assert decision["isbn_role"] == "container"
    assert original == snapshot


def test_issn_is_not_an_admission_criterion(policy):
    decision = policy.evaluate({"external_ids": [ext("issn", "2049-3630")]}, set())
    assert not decision["eligible"] and decision["issn_only"]


@pytest.mark.parametrize("value", [None, "bad", {"source": "isbn", "id": ISBN}])
def test_malformed_identifier_arrays_do_not_interrupt_selection(policy, value):
    decision = policy.evaluate({"doi": DOI, "external_ids": value,
                                "types": None, "source": {"external_ids": value}}, set())
    assert decision["eligible"]
    assert decision["external_ids"] == []
    assert decision["rejections"]["identifier_invalid_structure"] == 1


class MemorySelector(ScientiIdentifiedWorkSelector):
    def _write_documents(self, documents):
        # mongomock doesn't support PyMongo 4.17's new bulk 'sort' argument.
        # A real-Mongo test below covers the production bulk-write path.
        for document in documents:
            self.db[self.stage_name].replace_one({"_id": document["_id"]}, document, upsert=True)


def seed(db):
    docs = [
        {"_id": "a", "doi": DOI},
        {"_id": "b", "external_ids": [ext("isbn", ISBN)],
         "types": [{"source": "impactu", "type": "Capítulo de libro"}], "titles": [{"title": "Chapter one"}]},
        {"_id": "c", "external_ids": [ext("isbn", ISBN)],
         "types": [{"source": "impactu", "type": "Capítulo de libro"}], "titles": [{"title": "Chapter two"}]},
        {"_id": "d", "external_ids": pair(), "authors": [], "author_count": 0},
        {"_id": "e", "external_ids": [ext("issn", "2049-3630")]},
        {"_id": "f", "external_ids": pair()[:1]},
        {"_id": "g", "doi": DOI, "external_ids": [ext("isbn", ISBN), *pair()]},
        {"_id": "h", "doi": "10.0000/abc", "external_ids": [ext("isbn", "0000000000")]},
    ]
    db.works.insert_many(docs)
    db.persons.insert_one({"_id": RH})
    collections = {"works": "works", "persons": "persons"}
    db[RELEASES].insert_one({"_id": "release", "status": "published", "audit": "release_audit",
                            "collections": collections})
    db[RELEASE_AUDITS].insert_one({"_id": "release_audit", "release_name": "release",
                                  "status": "passed", "critical_anomalies": 0,
                                  "collections": collections,
                                  "reference_metrics": {"works_documents": 8, "persons_scanned": 1}})
    db[RELEASES].insert_one({"_id": "current", "current_release_name": "release"})
    return docs


@pytest.fixture
def database():
    db = mongomock.MongoClient().dam
    seed(db)
    return db


def selector(db, policy, **kwargs):
    return MemorySelector(db, release_name="release", run_name=kwargs.pop("run_name", "build"),
                          target_collection=kwargs.pop("target_collection", "identified"),
                          policy=policy, batch_size=2, progress_every=2, **kwargs)


def test_exact_union_chapters_and_separate_publication(database, policy):
    pointer = database[RELEASES].find_one({"_id": "current"})
    count = selector(database, policy, run_name="count", target_collection="", count_only=True).run()
    assert count["stats"]["selected"] == 5
    assert count["stats"]["by_criterion"] == {"doi": 2, "isbn": 3, "pair": 2}
    assert count["stats"]["isbn_roles"] == {"container": 2, "work": 1}
    assert "identified" not in database.list_collection_names()
    built = selector(database, policy, expected_count_run="count").run()
    assert built["status"] == "complete"
    assert database.identified.count_documents({}) == 5
    assert database.identified.find_one({"_id": "b"}) != database.identified.find_one({"_id": "c"})
    assert database.identified.find_one({"_id": "d"}) == {
        "_id": "d", "external_ids": pair(), "author_count": 0,
    }
    assert database[RELEASES].find_one({"_id": "current"}) == pointer
    assert database.works.count_documents({}) == 8
    assert database[AUDITS].find_one({"_id": "build"})["status"] == "passed"
    assert database[PUBLICATIONS].find_one({"_id": "identified"})["documents"] == 5
    assert selector(database, policy, expected_count_run="count").run()["stats"] == built["stats"]


def test_resume_after_write_before_checkpoint(database, policy):
    interrupted = selector(database, policy)
    original = interrupted._set

    def fail_after_writes(fields):
        if fields.get("last_id") == "d":
            raise RuntimeError("simulated crash after write, before checkpoint")
        return original(fields)

    with patch.object(interrupted, "_set", side_effect=fail_after_writes):
        with pytest.raises(RuntimeError, match="simulated crash"):
            interrupted.run()
    assert database[RUNS].find_one({"_id": "build"})["stats"]["processed"] == 2
    assert database[interrupted.stage_name].count_documents({}) == 4
    resumed = selector(database, policy).run()
    assert resumed["stats"]["processed"] == 8
    assert database.identified.count_documents({}) == 5


def test_recover_rename_before_control_checkpoint(database, policy):
    interrupted = selector(database, policy)
    original = interrupted._set

    def fail_after_rename(fields):
        if fields.get("status") == "complete":
            raise RuntimeError("simulated crash after rename")
        return original(fields)

    with patch.object(interrupted, "_set", side_effect=fail_after_rename):
        with pytest.raises(RuntimeError, match="after rename"):
            interrupted.run()
    assert database[RUNS].find_one({"_id": "build"})["status"] == "publishing"
    assert database.identified.count_documents({}) == 5
    assert selector(database, policy).run()["status"] == "complete"


def test_missing_or_modified_staging_is_not_published(database, policy):
    interrupted = selector(database, policy)
    original = interrupted._audit

    def corrupt(state, source):
        database[interrupted.stage_name].update_one({"_id": "a"}, {"$set": {"custom": 1}})
        return original(state, source)

    with patch.object(interrupted, "_audit", side_effect=corrupt):
        with pytest.raises(RuntimeError, match="metadata"):
            interrupted.run()
    assert "identified" not in database.list_collection_names()
    database.drop_collection(interrupted.stage_name)
    with pytest.raises(RuntimeError, match="staging collection is missing"):
        selector(database, policy).run()


def test_exclusive_run_and_target_reservations(database, policy):
    first = selector(database, policy)
    with patch.object(first, "_scan", side_effect=RuntimeError("pause")):
        with pytest.raises(RuntimeError):
            first.run()
    database[RUNS].update_one({"_id": "build"}, {"$set": {
        "owner": "other-process", "lease_until": datetime.now(timezone.utc) + timedelta(minutes=1),
    }})
    with pytest.raises(RuntimeError, match="Another process"):
        selector(database, policy).run()
    with pytest.raises(ValueError, match="owns this target"):
        selector(database, policy, run_name="second").run()
    database[RUNS].update_one({"_id": "build"}, {"$set": {
        "lease_until": datetime.now(timezone.utc) - timedelta(minutes=1),
    }})
    assert selector(database, policy).run()["status"] == "complete"


def test_config_changes_and_existing_target_are_rejected(database, policy):
    selector(database, policy).run()
    with pytest.raises(ValueError, match="pinned to another"):
        selector(database, policy, target_collection="different").run()
    database.already_exists.insert_one({"_id": "preserve"})
    with pytest.raises(ValueError, match="already exists"):
        selector(database, policy, run_name="another", target_collection="already_exists").run()
    assert database.already_exists.find_one() == {"_id": "preserve"}
    with pytest.raises(ValueError, match="over source"):
        selector(database, policy, run_name="overwrite", target_collection="works").run()


def test_source_evidence_and_count_mismatch_block_publication(database, policy):
    database[RELEASE_AUDITS].update_one({"_id": "release_audit"}, {"$set": {"critical_anomalies": 1}})
    with pytest.raises(RuntimeError, match="matching passed audit"):
        selector(database, policy).run()
    database[RELEASE_AUDITS].update_one({"_id": "release_audit"}, {"$set": {"critical_anomalies": 0}})
    selector(database, policy, run_name="count", count_only=True, target_collection="").run()
    database[RUNS].update_one({"_id": "count"}, {"$set": {"stats.selected": 100}})
    with pytest.raises(RuntimeError, match="differs from the exact count"):
        selector(database, policy, expected_count_run="count").run()
    assert "identified" not in database.list_collection_names()


def test_mixed_bson_ids_resume_does_not_skip_objectids(database, policy):
    oid = ObjectId()
    database.works.insert_one({"_id": oid, "doi": DOI})
    database[RELEASE_AUDITS].update_one({"_id": "release_audit"}, {"$set": {
        "reference_metrics.works_documents": 9,
    }})
    interrupted = selector(database, policy)
    original = interrupted._set

    def fail_at_string_boundary(fields):
        if fields.get("last_id") == "h":
            raise RuntimeError("boundary crash")
        return original(fields)

    with patch.object(interrupted, "_set", side_effect=fail_at_string_boundary):
        with pytest.raises(RuntimeError, match="boundary crash"):
            interrupted.run()
    resumed = selector(database, policy).run()
    assert resumed["stats"]["processed"] == 9
    assert database.identified.find_one({"_id": oid})["doi"] == DOI
    assert database.works.find_one({"_id": oid})["_id"] == oid


@pytest.mark.skipif(not os.getenv("YUKU_IDENTIFIED_TEST_URI"), reason="Opt-in real Mongo integration")
def test_real_mongo_shared_helpers_bulk_order_and_rename(tmp_path):
    from pymongo import MongoClient
    from kahi.KahiBase import KahiBase

    client = MongoClient(os.environ["YUKU_IDENTIFIED_TEST_URI"], serverSelectionTimeoutMS=5000)
    name = "yuku_identified_test_" + uuid4().hex
    db = client[name]
    try:
        seed(db)
        policy = WorkIdentifierPolicy()
        assert list(policy.template) == list(KahiBase().empty_work())
        assert policy.doi("doi:10.1007/S11192-020-03647-4") == "https://doi.org/10.1007/s11192-020-03647-4"
        real = ScientiIdentifiedWorkSelector(db, release_name="release", run_name="real",
                                             target_collection="identified", policy=policy, batch_size=2)
        assert real.run()["stats"]["selected"] == 5
        for document in db.identified.find():
            assert list(document) == ["_id", *(key for key in policy.template if key in document)]
            assert set(document) <= FIELDS | {"_id"}
        # Cross-type restart queries must work with actual Mongo's type bracketing.
        oid = ObjectId()
        db.mixed.insert_many([{"_id": "z"}, {"_id": oid}])
        assert [row["_id"] for row in db.mixed.find(_resume_query("z")).sort("_id", 1)] == [oid]
        assert _shared_helpers()[3]["empty_work"]
        # Exercise the actual detached controller, persistent files and report.
        full = "minciencias_works_v01_20261009"
        release = db[RELEASES].find_one({"_id": "release"})
        source = release["collections"]["works"]
        db[source].rename(full)
        db[RELEASES].update_one({"_id": "release"}, {"$set": {"collections.works": full}})
        db[RELEASE_AUDITS].update_one({"_id": release["audit"]}, {"$set": {"collections.works": full}})
        script = Path(__file__).resolve().parents[1] / "scripts/run_scienti_identified_works.py"
        command = [sys.executable, str(script), "--database", name, "--release", "release",
                   "--run-name", "detached",
                   "--output-dir", str(tmp_path), "--batch-size", "2"]
        launched = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
            env={**os.environ, "YUKU_MONGO_URI": os.environ["YUKU_IDENTIFIED_TEST_URI"]},
        )
        assert launched.returncode == 0, launched.stderr
        metadata = json.loads(launched.stdout)
        assert int((tmp_path / "job.pid").read_text()) == metadata["pid"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = json.loads((tmp_path / "status.json").read_text())
            if status["status"] in {"complete", "failed"}:
                break
            time.sleep(0.1)
        assert status["status"] == "complete", (tmp_path / "job.log").read_text()
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["selected_works"] == 5 and report["excluded_works"] == 3
        assert report["collection"] == full + "_filtered"
        assert db[full + "_filtered"].count_documents({}) == 5
        assert db[RELEASES].find_one({"_id": "current"})["current_release_name"] == "release"
    finally:
        client.drop_database(name)
        client.close()
