"""Promote an audited pair of work outputs and retire its named predecessors."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from yuku.minciencias_publication import (
    EntityRetention, adopt_published_history, assert_current_transition, parse_name,
    publish_current, register_release,
)

from yuku.scienti_identified_works import AUDITS, PUBLICATIONS, RUNS
from yuku.scienti_release import (
    FINAL_RELEASE_AUDITS, FINAL_RELEASE_CLEANUPS, FINAL_RELEASE_PUBLICATIONS,
    PERSON_PUBLICATIONS, ScientiFinalReleaseManager,
)


GRAPH_PUBLICATIONS = "scienti_work_graph_publications"
LOCK = "scienti_work_outputs_lock"


class ScientiWorkOutputs:
    """Keep two final work datasets; preserve source data and proof collections."""

    def __init__(self, db):
        self.db = db

    def _validate(self, release_name, identified):
        release = self.db[FINAL_RELEASE_PUBLICATIONS].find_one({"_id": release_name}) or {}
        audit = self.db[FINAL_RELEASE_AUDITS].find_one({"_id": release.get("audit")}) or {}
        collections = release.get("collections", {})
        if (release.get("status") != "published" or release.get("entity_count") != 6
                or audit.get("status") != "passed" or audit.get("critical_anomalies") != 0
                or audit.get("release_name") != release_name
                or audit.get("collections") != collections
                or audit.get("materialization_runs") != release.get("materialization_runs")):
            raise RuntimeError("work outputs require a matching passed six-entity release")
        available = set(self.db.list_collection_names())
        if not set(collections.values()).issubset(available):
            raise RuntimeError("published release has missing collections")
        selected = self.db[PUBLICATIONS].find_one({"_id": identified}) or {}
        if parse_name(collections["works"]) and identified != collections["works"] + "_filtered":
            raise RuntimeError("filtered works must share the full graph's entity version and date")
        selection_audit = self.db[AUDITS].find_one({"_id": selected.get("audit")}) or {}
        run = self.db[RUNS].find_one({"_id": selected.get("run_name")}) or {}
        source = selection_audit.get("config", {}).get("source", {})
        if (selected.get("release_name") != release_name
                or selection_audit.get("status") != "passed" or run.get("status") != "complete"
                or selection_audit.get("config") != run.get("config")
                or source.get("release_name") != release_name
                or source.get("collections") != collections
                or selection_audit.get("config", {}).get("target") != identified
                or not selection_audit.get("documents_digest")
                or selection_audit.get("documents_digest") != run.get("documents_digest")
                or identified not in available or identified in collections.values()):
            raise RuntimeError("identified works must be proven from this exact full graph")
        counts = source.get("counts", {})
        for entity in ("works", "persons"):
            if self.db[collections[entity]].count_documents({}) != counts.get(entity):
                raise RuntimeError("source counts changed after selection")
        actual = self.db[identified].count_documents({})
        if actual != selection_audit.get("documents") or actual != selected.get("documents"):
            raise RuntimeError("identified collection differs from its passed audit")
        final_run = release["materialization_runs"]["works"]
        graph = self.db[GRAPH_PUBLICATIONS].find_one({"_id": final_run}) or {}
        person = self.db[PERSON_PUBLICATIONS].find_one(
            {"_id": release["materialization_runs"]["persons"]}) or {}
        if (graph.get("status") != "published" or graph.get("collection") != collections["works"]
                or graph.get("documents") != counts["works"]
                or person.get("status") != "published"
                or person.get("collection") != collections["persons"]):
            raise RuntimeError("final work/person publication proofs are missing")
        return release, selected

    def plan(self, *, cleanup_name, source_release, release_name, identified, intermediates=()):
        """Read-only plan; never infer that raw or control collections are garbage."""
        release, selected = self._validate(release_name, identified)
        previous = self.db[FINAL_RELEASE_PUBLICATIONS].find_one({"_id": source_release}) or {}
        old_work = previous.get("collections", {}).get("works")
        current = self.db[FINAL_RELEASE_PUBLICATIONS].find_one({"_id": "current"}) or {}
        if (previous.get("status") != "published" or not old_work
                or current.get("current_release") not in {source_release, release_name}):
            raise RuntimeError("current release changed; refuse to retire another publication")
        keep = {release["collections"]["works"], identified}
        old_identified = set()
        for publication in self.db[PUBLICATIONS].find({"_id": {"$ne": "current"}}):
            proof = self.db[AUDITS].find_one({"_id": publication.get("audit")}) or {}
            if proof.get("config", {}).get("source", {}).get("collections", {}).get("works") == old_work:
                old_identified.add(publication["_id"])
        transient = set(intermediates)
        measurement = self.db.minciencias_measurement_runs.find_one(
            {"_id": release["materialization_runs"]["works"]}) or {}
        config = measurement.get("config", {})
        allowed = {config.get(key) for key in ("graph_collection", "measured_collection", "links_collection")}
        if transient - (allowed - {None, ""}):
            raise RuntimeError("only this materialization's proven intermediates may be retired")
        candidates = ({old_work} | old_identified | transient) - keep
        # A named intermediate must belong to this run's proven pipeline. The
        # controller supplies only base graph/measurement artifacts, never HTML.
        available = set(self.db.list_collection_names())
        work_datasets, graph_controls = self._work_datasets(available)
        unexpected = work_datasets - keep - candidates
        if unexpected:
            raise RuntimeError(f"unclassified work datasets require review: {sorted(unexpected)}")
        protected = set(release["collections"].values()) | {identified} | graph_controls
        for control in ("scienti_entity_publications", "scienti_affiliation_publications",
                        "scienti_person_publications", "scienti_final_entity_publications"):
            for pointer in self.db[control].find({"_id": {"$regex": "^current"}}):
                protected.update(pointer.get("destinations", {}).values())
                name = pointer.get("collection") or pointer.get("current_collection")
                if name and name != previous.get("collections", {}).get("persons"):
                    protected.add(name)
        if candidates & protected:
            raise RuntimeError(f"cleanup includes live collections: {sorted(candidates & protected)}")
        if candidates:
            self._check_dependents(candidates, source_release)
        return {"_id": cleanup_name, "source_release": source_release, "release_name": release_name,
                "full_graph": release["collections"]["works"], "identified": identified,
                "retained_work_collections": sorted(keep), "candidates": sorted(candidates),
                "intermediates": sorted(transient), "protected": sorted(protected),
                "documents": {"full": self.db[release["collections"]["works"]].count_documents({}),
                              "identified": selected["documents"]}}

    def _work_datasets(self, available):
        """Classify graph controls by their publication and matching checkpoint.

        A suffix alone cannot exempt an unknown work dataset from review. The
        completed checkpoint remains proof even after its graph is retired.
        Published output collections always count as data, regardless of name.
        """
        datasets, graph_controls = set(), set()
        for publication in self.db[GRAPH_PUBLICATIONS].find({"_id": {"$ne": "current"}}):
            graph = publication.get("collection")
            if not isinstance(graph, str) or publication.get("status") != "published":
                continue
            if graph in available:
                datasets.add(graph)
            runs = graph + "_graph_runs"
            if runs not in available:
                continue
            checkpoint = self.db[runs].find_one({"_id": publication["_id"],
                "status": "complete", "$or": [
                    {"config.target_collection": graph}, {"target_collection": graph}]}, {"_id": 1})
            if checkpoint:
                graph_controls.update({runs, graph + "_graph_review"} & available)
        for publication in self.db[PUBLICATIONS].find({"_id": {"$ne": "current"}}):
            if publication["_id"] in available:
                datasets.add(publication["_id"])
        graph_controls -= datasets
        datasets.update(name for name in available
                        if (name.startswith("scienti_works_") or
                            (parse_name(name) and parse_name(name)["entity"] == "works"))
                        and name not in graph_controls)
        return datasets, graph_controls

    def _check_dependents(self, candidates, source_release):
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for child in value.values():
                    yield from strings(child)
            elif isinstance(value, (tuple, list)):
                for child in value:
                    yield from strings(child)
        for name in self.db.list_collection_names():
            if not name.endswith("_runs") or name == FINAL_RELEASE_CLEANUPS:
                continue
            for run in self.db[name].find({"status": {"$nin": ["complete", "passed", "published"]}},
                                          {"config": 1, "status": 1}):
                references = set(strings(run.get("config", {})))
                if references & (candidates | {source_release}):
                    raise RuntimeError(f"unfinished run still needs cleanup inputs: {name}/{run['_id']}")

    def finalize(self, **arguments):
        token = uuid4().hex
        controls = self.db[FINAL_RELEASE_CLEANUPS]
        timestamp = datetime.now(timezone.utc)
        try:
            controls.find_one_and_update({"_id": LOCK, "$or": [
                {"lease_until": {"$exists": False}}, {"lease_until": {"$lte": timestamp}}]},
                {"$set": {"owner": token, "lease_until": timestamp + timedelta(minutes=15)}},
                upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError as error:
            raise RuntimeError("another work-output promotion is active") from error
        try:
            plan = self.plan(**arguments)
            recorded = controls.find_one({"_id": plan["_id"]}) or {}
            if recorded.get("plan") and recorded["plan"] != plan:
                raise RuntimeError("cleanup plan changed; refuse a different deletion set")
            if recorded.get("status") == "complete":
                return recorded
            controls.update_one({"_id": plan["_id"]}, {
                "$set": {"plan": plan, "status": "promoting"}}, upsert=True)
            release, selected = self._validate(plan["release_name"], plan["identified"])
            published_at = release["published_at"]
            current = self.db[FINAL_RELEASE_PUBLICATIONS].find_one({"_id": "current"}) or {}
            assert_current_transition(current, {"collections": release["collections"]})
            if parse_name(plan["full_graph"]):
                adopt_published_history(self.db)
            # All data/proofs already exist. Only metadata pointers change here.
            publish_current(self.db, GRAPH_PUBLICATIONS, "current", {
                "_id": "current", "target_entity": "works", "current_collection": plan["full_graph"],
                "current_run_name": release["materialization_runs"]["works"],
                "previous_collection": "", "published_at": published_at})
            person = self.db[PERSON_PUBLICATIONS].find_one(
                {"_id": release["materialization_runs"]["persons"]})
            publish_current(self.db, PERSON_PUBLICATIONS, "current", {
                "_id": "current", "current_run": person["_id"], "previous_run": "",
                "collection": person["collection"], "audit": person["audit"],
                "published_at": person["published_at"]})
            publish_current(self.db, FINAL_RELEASE_PUBLICATIONS, "current", {
                "_id": "current", "current_release": release["_id"],
                "previous_release": plan["source_release"], "entity_count": 6,
                "collections": deepcopy(release["collections"]), "audit": release["audit"],
                "published_at": published_at})
            # This single document exposes the full/identified pair atomically.
            publish_current(self.db, PUBLICATIONS, "current", {
                "_id": "current", "release_name": release["_id"],
                "full_graph": plan["full_graph"], "collection": plan["identified"],
                "run_name": selected["run_name"], "audit": selected["audit"],
                "documents": plan["documents"], "published_at": published_at})
            register_release(self.db, release)
            result = ScientiFinalReleaseManager(self.db).cleanup(
                cleanup_name=plan["_id"] + "_drop", release_name=release["_id"],
                candidates=plan["candidates"], protected=plan["protected"],
                reset_entity_publication=False)
            removed = set(plan["candidates"])
            self.db[GRAPH_PUBLICATIONS].update_many({"collection": {"$in": sorted(removed)}},
                {"$set": {"artifact_state": "retired", "cleanup": plan["_id"]}})
            self.db[PUBLICATIONS].update_many({"_id": {"$in": sorted(removed)}},
                {"$set": {"artifact_state": "retired", "cleanup": plan["_id"]}})
            retention = EntityRetention(self.db).cleanup()
            controls.update_one({"_id": plan["_id"]}, {"$set": {
                "status": "complete", "cleanup": result, "retention": retention,
                "retained_work_collections": plan["retained_work_collections"]}})
            return controls.find_one({"_id": plan["_id"]})
        finally:
            controls.delete_one({"_id": LOCK, "owner": token})
