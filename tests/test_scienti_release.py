import unittest

import mongomock

from yuku.scienti_release import ScientiFinalReleaseManager


ENTITIES = ("works", "projects", "patents", "events")
PERSON_ID = "0000000001"
GROUP_ID = "COL0000001"
DOI = "https://doi.org/10.1000/test"


class ScientiFinalReleaseTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.manager = ScientiFinalReleaseManager(self.db)
        self.collections = {entity: f"final_{entity}" for entity in ENTITIES}
        self.runs = {entity: f"run_{entity}" for entity in ENTITIES}
        for entity in ENTITIES:
            self.db[self.collections[entity]].insert_one({"_id": entity})
            self.db.minciencias_measurement_runs.insert_one(
                {
                    "_id": self.runs[entity],
                    "status": "complete",
                    "summary": {
                        "status": "complete",
                        "target_entity": entity,
                        "collection": self.collections[entity],
                        "documents": 1,
                        "works": 1 if entity == "works" else None,
                        "critical_anomalies": 0,
                    },
                }
            )

    def prepare_identity_snapshots(self):
        base = self.manager.publish(
            release_name="release_test",
            collections=self.collections,
            materialization_runs=self.runs,
        )
        self.db[self.collections["works"]].update_one(
            {"_id": "works"},
            {"$set": {
                "doi": DOI, "authors": [{"id": PERSON_ID}],
                "groups": [{"id": GROUP_ID}],
            }},
        )
        for entity in ("projects", "patents", "events"):
            self.db[self.collections[entity]].update_one(
                {"_id": entity},
                {"$set": {"authors": [{"id": PERSON_ID}], "groups": []}},
            )
        self.db.persons_final.insert_one({
            "_id": PERSON_ID,
            "external_ids": [{"source": "scienti", "id": {"COD_RH": PERSON_ID}}],
            "affiliations": [{"id": GROUP_ID}],
            "related_works": [{"provenance": "minciencias", "source": "doi", "id": DOI}],
        })
        self.db.affiliations_final.insert_one({"_id": GROUP_ID})
        self.db.scienti_person_materialization_runs.insert_one({
            "_id": "persons_run", "status": "complete",
            "config": {
                "final_release_name": "release_test",
                "affiliation_run_name": "affiliations_run",
                "affiliation_collection": "affiliations_final",
            },
            "summary": {
                "collection": "persons_final", "documents": 1,
                "audit": "persons_run_audit",
            },
        })
        self.db.scienti_person_materialization_audits.insert_one({
            "_id": "persons_run_audit", "status": "passed",
            "collection": "persons_final", "documents": 1,
            "critical_anomalies": 0,
            "source_proofs": {
                "release": {
                    "name": "release_test", "collections": self.collections,
                    "documents": {entity: 1 for entity in ENTITIES},
                }
            },
        })
        self.db.scienti_person_publications.insert_one({
            "_id": "persons_run", "status": "published",
            "collection": "persons_final", "documents": 1,
            "audit": "persons_run_audit",
        })
        self.db.scienti_affiliation_materialization_runs.insert_one({
            "_id": "affiliations_run", "status": "complete",
            "summary": {
                "collection": "affiliations_final", "documents": 1,
                "audit": "affiliations_run_audit",
            },
        })
        self.db.scienti_affiliation_materialization_audits.insert_one({
            "_id": "affiliations_run_audit", "status": "passed",
            "collection": "affiliations_final", "documents": 1,
            "critical_anomalies": 0,
        })
        self.db.scienti_affiliation_publications.insert_one({
            "_id": "affiliations_run", "status": "published",
            "collection": "affiliations_final", "documents": 1,
            "audit": "affiliations_run_audit",
        })
        return base

    def test_publishes_only_when_all_four_materializations_are_proven(self):
        result = self.manager.publish(
            release_name="release_test",
            audit_name="release_test_audit",
            collections=self.collections,
            materialization_runs=self.runs,
        )
        self.assertEqual(result["status"], "published")
        current = self.db.scienti_final_release_publications.find_one(
            {"_id": "current"}
        )
        self.assertEqual(current["current_release"], "release_test")
        self.assertEqual(current["collections"], self.collections)
        audit = self.db.scienti_final_release_audits.find_one(
            {"_id": "release_test_audit"}
        )
        self.assertEqual(audit["critical_anomalies"], 0)

    def test_failed_entity_never_replaces_current_release(self):
        self.db.scienti_final_release_publications.insert_one(
            {"_id": "current", "current_release": "safe_release"}
        )
        self.db.minciencias_measurement_runs.update_one(
            {"_id": self.runs["events"]}, {"$set": {"status": "failed"}}
        )
        with self.assertRaises(RuntimeError):
            self.manager.publish(
                release_name="release_bad",
                collections=self.collections,
                materialization_runs=self.runs,
            )
        current = self.db.scienti_final_release_publications.find_one(
            {"_id": "current"}
        )
        self.assertEqual(current["current_release"], "safe_release")

    def test_republishing_current_release_preserves_previous_pointer(self):
        self.db.scienti_final_release_publications.insert_one(
            {"_id": "current", "current_release": "previous_release"}
        )
        first = self.manager.publish(
            release_name="release_test",
            collections=self.collections,
            materialization_runs=self.runs,
        )
        second = self.manager.publish(
            release_name="release_test",
            collections=self.collections,
            materialization_runs=self.runs,
        )
        current = self.db.scienti_final_release_publications.find_one(
            {"_id": "current"}
        )
        self.assertEqual(first, second)
        self.assertEqual(current["previous_release"], "previous_release")

    def test_cleanup_removes_only_explicit_nonprotected_intermediates(self):
        self.manager.publish(
            release_name="release_test",
            collections=self.collections,
            materialization_runs=self.runs,
        )
        self.db.intermediate.insert_one({"_id": 1})
        self.db.raw_source.insert_one({"_id": 1})
        self.db.scienti_entity_publications.insert_one({"_id": "current"})
        result = self.manager.cleanup(
            cleanup_name="cleanup_test",
            release_name="release_test",
            candidates=["intermediate"],
            protected=["raw_source"],
        )
        self.assertEqual(result["removed"][0]["collection"], "intermediate")
        self.assertNotIn("intermediate", self.db.list_collection_names())
        self.assertIn("raw_source", self.db.list_collection_names())
        for collection in self.collections.values():
            self.assertIn(collection, self.db.list_collection_names())
        self.assertIsNone(
            self.db.scienti_entity_publications.find_one({"_id": "current"})
        )

    def test_cleanup_can_preserve_the_current_entity_snapshot(self):
        self.manager.publish(
            release_name="release_test",
            collections=self.collections,
            materialization_runs=self.runs,
        )
        pointer = {"_id": "current", "current_run_name": "entities_current"}
        self.db.scienti_entity_publications.insert_one(pointer)
        self.manager.cleanup(
            cleanup_name="cleanup_preserving_entities",
            release_name="release_test",
            candidates=[],
            reset_entity_publication=False,
        )
        self.assertEqual(
            self.db.scienti_entity_publications.find_one({"_id": "current"}),
            pointer,
        )

    def test_publishes_six_entities_after_direct_reference_validation(self):
        self.prepare_identity_snapshots()

        result = self.manager.publish_with_identities(
            release_name="kahi_release", base_release_name="release_test",
            person_run_name="persons_run",
            affiliation_run_name="affiliations_run", batch_size=2,
            progress_every=100,
        )
        repeated = self.manager.publish_with_identities(
            release_name="kahi_release", base_release_name="release_test",
            person_run_name="persons_run",
            affiliation_run_name="affiliations_run", batch_size=2,
            progress_every=100,
        )

        self.assertEqual(result, repeated)
        self.assertEqual(result["entity_count"], 6)
        self.assertEqual(set(result["collections"]), set(ENTITIES) | {
            "persons", "affiliations",
        })
        audit = self.db.scienti_final_release_audits.find_one(
            {"_id": "kahi_release_audit"}
        )
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["critical_anomalies"], 0)
        self.assertEqual(
            self.db.scienti_final_release_publications.find_one(
                {"_id": "current"}
            )["current_release"],
            "kahi_release",
        )

    def test_broken_cross_references_never_replace_current_release(self):
        self.prepare_identity_snapshots()
        self.db[self.collections["projects"]].update_one(
            {"_id": "projects"}, {"$set": {"authors": [{"id": "9999999999"}]}}
        )
        self.db[self.collections["works"]].update_one(
            {"_id": "works"}, {"$set": {"groups": [{"id": "COL9999999"}]}}
        )
        self.db.persons_final.update_one(
            {"_id": PERSON_ID},
            {"$set": {"related_works": [
                {"provenance": "minciencias", "source": "doi",
                 "id": "https://doi.org/10.9999/missing"}
            ]}},
        )

        with self.assertRaisesRegex(RuntimeError, "six-entity release audit failed"):
            self.manager.publish_with_identities(
                release_name="broken_release", base_release_name="release_test",
                person_run_name="persons_run",
                affiliation_run_name="affiliations_run", batch_size=2,
                progress_every=100,
            )

        audit = self.db.scienti_final_release_audits.find_one(
            {"_id": "broken_release_audit"}
        )
        self.assertEqual(audit["critical_counts"][
            "projects_unresolved_author_references"
        ], 1)
        self.assertEqual(audit["critical_counts"][
            "persons_unresolved_related_dois"
        ], 1)
        self.assertEqual(audit["critical_counts"][
            "works_unresolved_group_references"
        ], 1)
        self.assertEqual(
            self.db.scienti_final_release_publications.find_one(
                {"_id": "current"}
            )["current_release"],
            "release_test",
        )

    def test_invalid_source_doi_is_excluded_without_blocking_release(self):
        self.prepare_identity_snapshots()
        self.db[self.collections["works"]].update_one(
            {"_id": "works"}, {"$set": {"doi": "https://doi.org/10.15446/"}}
        )
        self.db.persons_final.update_one(
            {"_id": PERSON_ID}, {"$set": {"related_works": []}}
        )

        result = self.manager.publish_with_identities(
            release_name="quality_release", base_release_name="release_test",
            person_run_name="persons_run", affiliation_run_name="affiliations_run",
            batch_size=2, progress_every=100,
        )

        self.assertEqual(result["status"], "published")
        audit = self.db.scienti_final_release_audits.find_one(
            {"_id": "quality_release_audit"}
        )
        self.assertEqual(audit["critical_anomalies"], 0)
        self.assertEqual(audit["quality_counts"]["excluded_invalid_work_dois"], 1)


if __name__ == "__main__":
    unittest.main()
