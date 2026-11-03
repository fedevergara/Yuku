import os
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

import mongomock
import pytest
from pymongo import MongoClient

from yuku.cvlac_related_works import normalize_doi
from yuku.cvlac_work_graph import materialize_bibliographic_context
from yuku.scienti_doi_reconciliation import (
    AUDITS, CHECKS, RUNS, VERSION, DOIIdentityVerifier, ScientiDOIReconciler,
    digest, identity_match, record_rows, similar_dois, validate_transformation_audit,
)
from yuku.scienti_journals import journal_context
from yuku.scienti_normalization import CVLAC_AUDIT_RUNS, GRUPLAC_AUDIT_RUNS

TITLE = "Effect of home care activities on labor participation in a developing country"
SHORT = "https://doi.org/10.14254/2071-789x.2024"
FULL = SHORT + "/17-1/7"


@pytest.mark.parametrize("text,country", [
    (f'AUTOR, "{TITLE}" . En: Polonia ECONOMICS AND SOCIOLOGY ISSN: 2071-789X ed: Centre of Sociological Research v. 17 fasc. 1 p.103 - 116 ,2024, DOI: {SHORT}', "Polonia ECONOMICS AND SOCIOLOGY"),
    (f"1.- Publicado en revista especializada: {TITLE} Polonia, ECONOMICS AND SOCIOLOGY ISSN: 2071-789X, 2024 vol:17 fasc: 1 págs: 103 - 116, DOI: {SHORT} Autores: AUTOR", "Polonia"),
])
def test_observed_html_layouts(text, country):
    context = journal_context(text, TITLE, country=country)
    assert context["journal_name"] == "ECONOMICS AND SOCIOLOGY"
    assert context["volume"] == "17"
    assert context["issue"] == "1"


@pytest.mark.parametrize("text", [
    f'AUTOR, "{TITLE}" En: Lugar desconocido Revista ambigua ISSN: 1234-5679',
    f'AUTOR, "{TITLE}" DOI: {SHORT}',
    f'{TITLE} ISSN: 1234-5679',
])
def test_does_not_guess_unbounded_journal(text):
    assert journal_context(text, TITLE)["journal_name"] == ""


def test_explicit_journal_label_and_missing_issue():
    result = journal_context(f"{TITLE} Revista: Economics & Sociology ISSN: 2071-789X fasc: N/A págs: 3 - 5", TITLE)
    assert result["journal_name"] == "Economics & Sociology"
    assert result["issue"] == ""


@pytest.mark.parametrize("left,right,expected", [
    (SHORT, FULL, True),
    (SHORT, SHORT, False),
    (FULL, FULL.replace("10.14254", "10.99999"), False),
    (FULL, FULL.replace("2071-789x", "completelydifferent"), False),
    (FULL, FULL.replace("17-1/7", "17-1/8"), True),
    ("https://doi.org/10.1234/a", "https://doi.org/10.1234/b", False),
])
def test_doi_similarity_scope(left, right, expected):
    assert similar_dois(left, right) == expected


def record(doi=SHORT, title=TITLE):
    return {"title": title, "doi": [doi], "year": 2024, "type_impactu": "Articulo de revista",
            "product_type": "Producción bibliográfica - Artículo - Publicado en revista especializada",
            "source_section": "Articulos", "country": "Polonia", "publisher": "",
            "authors": ["Claudia Liceth Fajardo Hoyos", "Jhon James Mora Rodriguez"],
            "issn": ["2071-789X"], "raw_text": f'Autores, "{title}" . En: Polonia ECONOMICS AND SOCIOLOGY ISSN: 2071-789X ed: Centre of Sociological Research v. 17 fasc. 1 p.103 - 116 ,2024, DOI: {doi}'}


def fixture(db):
    db.cvlac_source.insert_one({"_id": "0000000001", "production": [record()]})
    group = record(FULL)
    group["product_type"] = "Publicado en revista especializada:"
    group["source_section"] = "Artículos publicados"
    db.gruplac_source.insert_one({"_id": "COL0000001", "production": [group]})
    for collection, name, config, summary in [
        (CVLAC_AUDIT_RUNS, "cvlac_audit", {"destination_collection": "cvlac_source", "parser_version": "3.2.0"},
         {"destination_profiles": 1, "source_profiles": 1}),
        (GRUPLAC_AUDIT_RUNS, "gruplac_audit", {"normalized_collection": "gruplac_source", "expected_parser_version": "3.2.0"},
         {"normalized_groups": 1, "expected_groups": 1, "raw_groups": 1}),
    ]:
        db[collection].insert_one({"_id": name, "status": "passed", "config": config,
            "summary": {**summary, "critical_anomalies": 0, "stored_parser_errors": 0,
                        "parser_version_counts": {"3.2.0": 1}}})


class Verifier:
    def __init__(self, db, statuses=None):
        self.db, self.calls = db, []
        self.statuses = statuses or {SHORT: "missing", FULL: "found"}

    def verify(self, doi, rows):
        self.calls.append(doi)
        status = self.statuses[doi]
        result = {"_id": digest((VERSION, "test_reconcile", doi)), "doi": doi, "status": status,
                  "matches": {row["_id"]: {"confirmed": status == "found"} for row in rows}}
        self.db[CHECKS].replace_one({"_id": result["_id"]}, result, upsert=True)
        return result


def reconciler(db, *, confirm=True, verifier=None):
    return ScientiDOIReconciler(db, run_name="test_reconcile", confirm_doi=confirm,
        sources={"cvlac": "cvlac_source", "gruplac": "gruplac_source"},
        targets={"cvlac": "cvlac_output", "gruplac": "gruplac_output"},
        audits={"cvlac": "cvlac_audit", "gruplac": "gruplac_audit"}, batch_size=1,
        verifier=verifier or Verifier(db), doi_normalizer=normalize_doi)


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient().dam
    fixture(database)
    def compatible_bulk(collection, operations):
        for operation in operations:
            if hasattr(operation, "_doc"):
                if operation._doc.get("_id"):
                    collection.replace_one(operation._filter, operation._doc, upsert=operation._upsert)
                else:
                    collection.update_one(operation._filter, operation._doc, upsert=operation._upsert)
    monkeypatch.setattr(ScientiDOIReconciler, "_bulk", staticmethod(compatible_bulk))
    return database


def test_end_to_end_correction_local_context_and_original_immutability(db):
    original = deepcopy(db.cvlac_source.find_one())
    verifier = Verifier(db)
    result = reconciler(db, verifier=verifier).run()
    assert result["status"] == "passed"
    assert result["corrected_identities"] == 1
    assert set(verifier.calls) == {SHORT, FULL}
    output = db.cvlac_output.find_one()["production"][0]
    assert output["doi"] == [FULL]
    assert output["journal_name"] == "ECONOMICS AND SOCIOLOGY"
    assert output["publisher"] == "Centre of Sociological Research"
    assert output["title"] == original["production"][0]["title"]
    assert output["authors"] == original["production"][0]["authors"]
    assert db.cvlac_source.find_one() == original
    assert result["registry_enrichment"] is False
    wrapper = db[CVLAC_AUDIT_RUNS].find_one({"_id": "test_reconcile_cvlac_audit"})
    validate_transformation_audit(db, wrapper, "cvlac_output")
    db[AUDITS].update_one({"_id": "test_reconcile"}, {"$set": {"status": "failed"}})
    with pytest.raises(RuntimeError):
        validate_transformation_audit(db, wrapper, "cvlac_output")


@pytest.mark.parametrize("statuses,decision", [
    ({SHORT: "found", FULL: "found"}, "review"),
    ({SHORT: "pending", FULL: "found"}, "pending"),
])
def test_no_automatic_choice_between_valid_or_pending_dois(db, statuses, decision):
    verifier = Verifier(db, statuses)
    result = reconciler(db, verifier=verifier).run()
    assert result["corrected_identities"] == 0
    assert result["decision_counts"] == {decision: 1}
    assert db.cvlac_output.find_one()["production"][0]["doi"] == [SHORT]


@pytest.mark.parametrize("change", ["title", "doi", "same_doi"])
def test_non_candidates_never_trigger_requests(db, change):
    if change == "title":
        db.gruplac_source.update_one({}, {"$set": {"production.0.title": "Un trabajo completamente distinto"}})
    else:
        db.gruplac_source.update_one({}, {"$set": {"production.0.doi": [SHORT if change == "same_doi" else "https://doi.org/10.14254/completelyunrelatedidentifier"]}})
    verifier = Verifier(db)
    result = reconciler(db, verifier=verifier).run()
    assert verifier.calls == []
    assert result["corrected_identities"] == 0


def test_confirmation_optional_and_completed_run_idempotent(db):
    verifier = Verifier(db)
    worker = reconciler(db, confirm=False, verifier=verifier)
    worker.run()
    worker.run()
    assert verifier.calls == []
    assert db.cvlac_output.find_one()["production"][0]["doi"] == [SHORT]


def test_null_distinct_value_is_ignored_before_sorting(db):
    worker = reconciler(db)
    original = worker.rows.distinct
    def distinct(key, query=None):
        assert query == {"check_dois": {"$type": "array"}}
        return [None, "", *original(key, query)]
    with patch.object(worker.rows, "distinct", side_effect=distinct):
        result = worker.run()
    assert result["corrected_identities"] == 1
    assert set(worker.verifier.calls) == {SHORT, FULL}


def test_known_null_bug_checkpoint_migrates_before_confirmation_only(db):
    from yuku.scienti_doi_reconciliation import PRECONFIRM_NULL_BUG_IMPLEMENTATIONS
    worker = reconciler(db)
    with patch.object(worker, "_confirm", side_effect=TypeError("null distinct")):
        with pytest.raises(TypeError):
            worker.run()
    previous = next(iter(PRECONFIRM_NULL_BUG_IMPLEMENTATIONS))
    db[RUNS].update_one({"_id": "test_reconcile"}, {"$set": {"config.implementation": previous}})
    result = reconciler(db).run()
    assert result["corrected_identities"] == 1
    history = db[RUNS].find_one({"_id": "test_reconcile"})["implementation_history"]
    assert len(history) == 1 and history[0]["previous"] == previous


def test_conflicting_valid_targets_do_not_correct_a_shared_missing_doi(db):
    alternative = FULL + "/1"
    second = record(alternative)
    second["product_type"] = "Publicado en revista especializada:"
    db.gruplac_source.update_one({}, {"$push": {"production": second}})
    result = reconciler(db, verifier=Verifier(db, {SHORT: "missing", FULL: "found", alternative: "found"})).run()
    assert result["corrected_identities"] == 0
    assert "confirmed_correction" not in result["decision_counts"]
    assert db.cvlac_output.find_one()["production"][0]["doi"] == [SHORT]


def test_resume_after_copy_before_checkpoint(db):
    worker = reconciler(db)
    original_touch = worker._touch
    def fail(**values):
        if "copied_last.cvlac" in values:
            raise RuntimeError("controlled interruption")
        return original_touch(**values)
    with patch.object(worker, "_touch", side_effect=fail):
        with pytest.raises(RuntimeError, match="controlled"):
            worker.run()
    assert db[RUNS].find_one()["status"] == "failed"
    result = reconciler(db).run()
    assert result["documents"] == {"cvlac": 1, "gruplac": 1}
    assert db.cvlac_output.count_documents({}) == 1


def test_audit_detects_modified_output_before_publication(db):
    worker = reconciler(db)
    original_copy = worker._bulk
    def tamper(collection, operations):
        original_copy(collection, operations)
        if collection.name == "cvlac_output":
            collection.update_one({}, {"$set": {"production.0.title": "Wrong title"}})
    with patch.object(worker, "_bulk", side_effect=tamper):
        with pytest.raises(RuntimeError, match="audit mismatch"):
            worker.run()
    assert db[AUDITS].count_documents({"status": "passed"}) == 0


def test_local_journal_conflicts_preserved_without_registry_enrichment():
    nodes = [{"type_family": "article", "journal_name": "Economics & Sociology", "publisher": "Publisher", "issn": ["2071-789X"]},
             {"type_family": "article", "journal_name": "American Journal of Economics and Sociology", "publisher": "Publisher", "issn": ["0002-9246"]}]
    output = {"source": {}}
    materialize_bibliographic_context(output, nodes, 123, [])
    assert output["source"] == {}
    assert output["bibliographic_info"]["scienti"]["fields"]["journal_name"]["status"] == "conflict"


def test_article_source_contains_journal_and_issn():
    output = {"source": {}}
    materialize_bibliographic_context(output, [{"type_family": "article", "journal_name": "Economics & Sociology", "issn": ["2071-789X"], "issue": "1", "volume": "17"}], 123, [])
    assert output["source"]["types"][0]["type"] == "journal"
    assert output["source"]["external_ids"] == [{"source": "issn", "id": "2071-789X"}]


class Response:
    headers = {}
    def __init__(self, status, data):
        self.status_code, self.data = status, data
    def json(self):
        return self.data


class Session:
    def __init__(self, responses):
        self.headers, self.responses, self.calls = {}, iter(responses), []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_crossref_requests_only_confirmation_fields_and_stores_no_metadata():
    database = mongomock.MongoClient().dam
    session = Session([Response(200, {"responseCode": 1}), Response(200, [{"RA": "Crossref"}]),
        Response(200, {"message": {"items": [{"DOI": FULL.removeprefix("https://doi.org/"),
            "title": [TITLE], "published": {"date-parts": [[2024, 3]]}, "type": "journal-article"}]}})])
    verifier = DOIIdentityVerifier(database, "request_test", session=session, delay=0)
    rows = record_rows(record(), normalize_doi)
    result = verifier.verify(FULL, rows)
    assert result["matches"][rows[0]["_id"]]["confirmed"]
    assert session.calls[-1][1]["params"]["select"] == "DOI,title,published,type"
    assert "title" not in result and "publisher" not in result and "journal_name" not in result
    verifier.verify(FULL, rows)
    assert len(session.calls) == 3


def test_rate_limit_is_pending_not_invalid():
    database = mongomock.MongoClient().dam
    verifier = DOIIdentityVerifier(database, "rate_limit", session=Session([Response(429, {})]), delay=0, attempts=1)
    assert verifier.verify(FULL, record_rows(record(), normalize_doi))["status"] == "pending"


def registry_session():
    return Session([Response(200, {"responseCode": 1}), Response(200, [{"RA": "Crossref"}]),
        Response(200, {"message": {"items": [{"DOI": FULL.removeprefix("https://doi.org/"),
            "title": [TITLE], "published": {"date-parts": [[2024]]}, "type": "journal-article",
            "publisher": "This extra field must not be retained"}]}})])


def test_permanent_history_survives_run_change_and_per_run_proof_deletion():
    from yuku.scienti_doi_history import HISTORY
    database = mongomock.MongoClient().dam
    rows = record_rows(record(), normalize_doi)
    first_session = registry_session()
    first = DOIIdentityVerifier(database, "first", session=first_session, delay=0).verify(FULL, rows)
    database[CHECKS].delete_many({})
    second_session = Session([])
    second = DOIIdentityVerifier(database, "second", session=second_session, delay=0).verify(FULL, rows)
    assert second["matches"] == first["matches"]
    assert second["reused_history"] and second["run_name"] == "second"
    assert len(first_session.calls) == 3 and second_session.calls == []
    saved = database[HISTORY].find_one({"doi": FULL})
    assert saved["identity"] == {"doi": FULL, "titles": [TITLE], "years": [2024], "type": "journal-article"}
    assert "publisher" not in str(saved)


@pytest.mark.parametrize("change", ["title", "year", "family"])
def test_new_local_context_is_recompared_without_remote_enrichment(change):
    database = mongomock.MongoClient().dam
    rows = record_rows(record(), normalize_doi)
    DOIIdentityVerifier(database, "first", session=registry_session(), delay=0).verify(FULL, rows)
    changed = deepcopy(rows[0])
    changed[change] = {"title": "an entirely different work title", "year": 2023, "family": "book"}[change]
    changed["_id"] = "different_context"
    session = Session([])
    result = DOIIdentityVerifier(database, "second", session=session, delay=0).verify(FULL, [changed])
    assert result["status"] == "found" and not result["matches"]["different_context"]["confirmed"]
    assert session.calls == []


def test_legacy_checks_migrate_without_registry_requests_or_invented_metadata():
    from datetime import datetime, timezone
    from yuku.scienti_doi_history import HISTORY
    database = mongomock.MongoClient().dam
    rows = record_rows(record(), normalize_doi)
    old_id = digest((VERSION, "old", FULL))
    database[CHECKS].insert_one({"_id": old_id, "run_name": "old", "doi": FULL,
        "checked_at": datetime.now(timezone.utc), "status": "found", "agency": "Crossref",
        "matches": {r["_id"]: {"title_score": 1, "year_match": True, "type_match": True, "confirmed": True} for r in rows}})
    session = Session([])
    result = DOIIdentityVerifier(database, "new", session=session, delay=0).verify(FULL, rows)
    assert result["matches"][rows[0]["_id"]]["confirmed"] and session.calls == []
    historic = database[HISTORY].find_one({"doi": FULL})
    assert historic["migrated_from"] == old_id and "identity" not in historic


def test_migrated_positive_with_unknown_context_requires_new_confirmation():
    from datetime import datetime, timezone
    database = mongomock.MongoClient().dam
    database[CHECKS].insert_one({"_id": digest((VERSION, "old", FULL)), "run_name": "old", "doi": FULL,
        "checked_at": datetime.now(timezone.utc), "status": "found", "matches": {"old_context": {"confirmed": True}}})
    session = registry_session()
    result = DOIIdentityVerifier(database, "new", session=session, delay=0).verify(FULL, record_rows(record(), normalize_doi))
    assert result["status"] == "found" and len(session.calls) == 3


def test_negative_expires_after_seven_days_then_can_become_found():
    from datetime import datetime, timedelta, timezone
    from yuku.scienti_doi_history import HISTORY
    database = mongomock.MongoClient().dam
    rows = record_rows(record(), normalize_doi)
    negative = DOIIdentityVerifier(database, "missing", session=Session([Response(404, {"responseCode": 100})]), delay=0)
    assert negative.verify(FULL, rows)["status"] == "missing"
    still_fresh = DOIIdentityVerifier(database, "reuse", session=Session([]), delay=0)
    assert still_fresh.verify(FULL, rows)["status"] == "missing"
    database[HISTORY].update_one({"doi": FULL}, {"$set": {"checked_at": datetime.now(timezone.utc) - timedelta(days=8)}})
    retry = registry_session()
    assert DOIIdentityVerifier(database, "retry", session=retry, delay=0).verify(FULL, rows)["status"] == "found"
    assert len(retry.calls) == 3


def test_pending_is_retried_and_never_treated_as_a_negative():
    database = mongomock.MongoClient().dam
    rows = record_rows(record(), normalize_doi)
    assert DOIIdentityVerifier(database, "first", session=Session([Response(429, {})]), delay=0, attempts=1).verify(FULL, rows)["status"] == "pending"
    retry = registry_session()
    result = DOIIdentityVerifier(database, "retry", session=retry, delay=0).verify(FULL, rows)
    assert result["matches"][rows[0]["_id"]]["confirmed"] and len(retry.calls) == 3


def test_busy_doi_history_does_not_duplicate_http_requests():
    database = mongomock.MongoClient().dam
    first = DOIIdentityVerifier(database, "first", session=Session([]), delay=0)
    token = first.history.acquire(FULL)
    second = DOIIdentityVerifier(database, "second", session=Session([]), delay=0)
    result = second.verify(FULL, record_rows(record(), normalize_doi))
    assert result["status"] == "pending" and result["reason"] == "historical_confirmation_in_progress"
    assert second.session.calls == []
    first.history.release(FULL, token)


def test_two_reconciliation_runs_reuse_history_and_keep_their_own_proof_ids(db):
    first_session = Session([Response(404, {"responseCode": 100}), *list(registry_session().responses)])
    first = DOIIdentityVerifier(db, "test_reconcile", session=first_session, delay=0)
    assert reconciler(db, verifier=first).run()["corrected_identities"] == 1
    second_session = Session([])
    second = DOIIdentityVerifier(db, "second_reconcile", session=second_session, delay=0)
    worker = ScientiDOIReconciler(db, run_name="second_reconcile", confirm_doi=True,
        sources={"cvlac": "cvlac_source", "gruplac": "gruplac_source"},
        targets={"cvlac": "second_cv", "gruplac": "second_gr"},
        audits={"cvlac": "cvlac_audit", "gruplac": "gruplac_audit"},
        verifier=second, doi_normalizer=normalize_doi)
    result = worker.run()
    assert result["corrected_identities"] == 1 and result["history_metrics"]["history_hits"] == 2
    assert len(first_session.calls) == 4 and not second_session.calls
    pair = worker.pairs.find_one()
    assert all(db[CHECKS].find_one({"_id": key})["run_name"] == "second_reconcile" for key in pair["checks"])


def test_timeout_on_unknown_legacy_context_preserves_previous_confirmation():
    from datetime import datetime, timezone
    database = mongomock.MongoClient().dam
    rows = record_rows(record(), normalize_doi)
    database[CHECKS].insert_one({"_id": digest((VERSION, "old", FULL)), "run_name": "old", "doi": FULL,
        "checked_at": datetime.now(timezone.utc), "status": "found", "matches": {rows[0]["_id"]: {"confirmed": True}}})
    changed = {**rows[0], "_id": "unknown", "title": "a different title"}
    assert DOIIdentityVerifier(database, "retry", session=Session([Response(429, {})]), delay=0, attempts=1).verify(FULL, [changed])["status"] == "pending"
    verifier = DOIIdentityVerifier(database, "reuse", session=Session([]), delay=0)
    assert verifier.verify(FULL, rows)["matches"][rows[0]["_id"]]["confirmed"]
    assert not verifier.session.calls


@pytest.mark.skipif(not os.getenv("YUKU_IDENTIFIED_TEST_URI"), reason="Opt-in isolated real Mongo history")
def test_real_mongo_history_evidence_reuse_and_unique_lease():
    client = MongoClient(os.environ["YUKU_IDENTIFIED_TEST_URI"], serverSelectionTimeoutMS=5000)
    database = client["yuku_doi_history_test_" + uuid4().hex]
    try:
        rows = record_rows(record(), normalize_doi)
        first = DOIIdentityVerifier(database, "first", session=registry_session(), delay=0)
        assert first.verify(FULL, rows)["matches"][rows[0]["_id"]]["confirmed"]
        database[CHECKS].delete_many({})
        second = DOIIdentityVerifier(database, "second", session=Session([]), delay=0)
        assert second.verify(FULL, rows)["reused_history"] and not second.session.calls
        busy = FULL + "/busy"
        token = first.history.acquire(busy)
        assert second.verify(busy, rows)["reason"] == "historical_confirmation_in_progress"
        first.history.release(busy, token)
        assert database[first.history.collection.name].find_one({"doi": FULL})["identity"]["titles"] == [TITLE]
    finally:
        client.drop_database(database.name)
        client.close()


def test_resolution_alone_does_not_confirm_work():
    rows = record_rows(record(), normalize_doi)
    assert not identity_match(rows[0], {"titles": ["An unrelated work"], "years": [2024], "type": "journal-article"})["confirmed"]
    assert not identity_match(rows[0], {"titles": [TITLE], "years": [2023], "type": "journal-article"})["confirmed"]


def test_actual_cvlac_parser_populates_journal_context():
    from bs4 import BeautifulSoup
    from yuku.cvlac_related_works import parse_blockquote
    raw = record()["raw_text"]
    parsed = parse_blockquote("0000000001", "Articulos", BeautifulSoup(
        "<blockquote>" + raw + "</blockquote>", "html.parser").blockquote)
    assert parsed["journal_name"] == "ECONOMICS AND SOCIOLOGY"
    assert parsed["publisher"] == "Centre of Sociological Research"
    assert parsed["doi"] == [SHORT]


def test_optional_pipeline_stage_precedes_all_work_consumers(tmp_path):
    from yuku.scienti_pipeline import ScientiFullPipeline, STAGES, load_pipeline_config
    path = tmp_path / "pipeline.json"
    import json
    path.write_text(json.dumps({"run_name": "pipeline_doi", "processing": {"confirm_conflicting_dois": True},
        "convocations": {"researchers_json": "researchers.json", "groups_json": "groups.json"}}))
    config = load_pipeline_config(path)
    class Yuku:
        db = mongomock.MongoClient().dam
        def reconcile_scienti_dois(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "passed"}
    yuku = Yuku()
    pipeline = ScientiFullPipeline(yuku, config)
    assert STAGES.index("cvlac_audit") < STAGES.index("doi_reconciliation") < STAGES.index("entities_normalize") < STAGES.index("base_graph")
    pipeline._doi_reconciliation()
    assert yuku.kwargs["confirm_doi"] is True
    assert pipeline._normalized_source("cvlac") == config["collections"]["cvlac_reconciled"]
    assert pipeline._normalized_audit("cvlac") == "pipeline_doi_doi_reconciliation_cvlac_audit"
    pipeline.config["processing"]["confirm_conflicting_dois"] = False
    assert pipeline._doi_reconciliation()["status"] == "skipped"


def test_versioned_release_publication_preserves_current_pointer():
    from yuku.scienti_release import ScientiFinalReleaseManager
    database = mongomock.MongoClient().dam
    collections = {kind: "output_" + kind for kind in ("works", "projects", "patents", "events")}
    runs = {kind: "run_" + kind for kind in collections}
    database.scienti_final_release_publications.insert_one({"_id": "current", "current_release": "old_release"})
    for kind in collections:
        database[collections[kind]].insert_one({"_id": "one"})
        database.minciencias_measurement_runs.insert_one({"_id": runs[kind], "status": "complete",
            "summary": {"target_entity": kind, "collection": collections[kind], "documents": 1}})
    result = ScientiFinalReleaseManager(database).publish(release_name="new_release",
        collections=collections, materialization_runs=runs, publish_pointer=False)
    assert result["status"] == "published"
    assert database.scienti_final_release_publications.find_one({"_id": "current"})["current_release"] == "old_release"
    proof = database.scienti_final_release_audits.find_one({"_id": result["audit"]})
    with patch("yuku.scienti_release.time", return_value=9999999999):
        repeated = ScientiFinalReleaseManager(database).publish(release_name="new_release",
            collections=collections, materialization_runs=runs, publish_pointer=False)
    assert repeated == result
    assert database.scienti_final_release_audits.find_one({"_id": result["audit"]}) == proof


def test_person_version_publication_preserves_current_pointer():
    from yuku.scienti_persons import ScientiPersonMaterializer
    database = mongomock.MongoClient().dam
    database.scienti_person_publications.insert_one({"_id": "current", "current_run": "previous"})
    builder = ScientiPersonMaterializer(database, run_name="new_persons",
        manifest_run_name="manifest", manifest_collection="manifest_items", directory_collection="directory",
        recognized_collection="recognized", open_data_collection="open_data", cvlac_collection="cvlac",
        cvlac_audit_name="cvlac_audit", gruplac_collection="gruplac", gruplac_audit_name="gruplac_audit",
        affiliation_run_name="affiliations_run", affiliation_collection="affiliations",
        final_release_name="new_release", final_release_audit_name="release_audit",
        target_collection="new_persons_collection", publish_pointer=False)
    builder._publish({"documents": 1}, {"_id": "new_persons_audit"})
    assert database.scienti_person_publications.find_one({"_id": "current"})["current_run"] == "previous"
    assert database.scienti_person_publications.find_one({"_id": "new_persons"})["collection"] == "new_persons_collection"


@pytest.mark.skipif(not os.getenv("YUKU_IDENTIFIED_TEST_URI"), reason="Opt-in real Mongo")
def test_real_mongo_bulk_audited_views():
    client = MongoClient(os.environ["YUKU_IDENTIFIED_TEST_URI"], serverSelectionTimeoutMS=5000)
    database = client["yuku_doi_test_" + uuid4().hex]
    try:
        fixture(database)
        worker = reconciler(database)
        # Exercise the actual distinct/index behaviour with absent, null and
        # empty candidate values, in addition to the valid DOI pair.
        worker.rows.insert_many([
            {"_id": "non_candidate", "check_dois": None},
            {"_id": "empty_candidate", "check_dois": [None, ""]},
        ])
        result = worker.run()
        assert result["corrected_identities"] == 1
        assert set(worker.verifier.calls) == {SHORT, FULL}
        assert database.cvlac_output.find_one()["production"][0]["doi"] == [FULL]
        assert result["digests"]["cvlac"]["source"] != result["digests"]["cvlac"]["output"]
    finally:
        client.drop_database(database.name)
        client.close()
