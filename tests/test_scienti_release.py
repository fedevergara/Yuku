import unittest

import mongomock

from yuku.scienti_release import ScientiFinalReleaseManager


ENTITIES = ("works", "projects", "patents", "events")


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


if __name__ == "__main__":
    unittest.main()
