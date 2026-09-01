import unittest

import mongomock

from yuku.scienti_auxiliary_audit import AUDITS as AUXILIARY_AUDITS
from yuku.scienti_entities import ENTITY_AUDITS, ENTITY_RUNS
from yuku.scienti_entity_comparison import ENTITY_COMPARISONS
from yuku.scienti_entity_publication import PUBLICATIONS, ScientiEntityPublisher
from yuku.scienti_project_audit import PROJECT_AUDITS


class ScientiEntityPublisherTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.destinations = {
            "works": "works_v3", "projects": "projects_v3",
            "patents": "patents_v3", "events": "events_v3",
        }
        self.db[ENTITY_RUNS].insert_one({
            "_id": "entities_v3", "status": "complete",
            "config": {
                "destinations": self.destinations,
                "normalizer_version": "scienti-entity-normalizer-v3",
            },
        })
        self.db[ENTITY_AUDITS].insert_one({
            "_id": "entities_v3", "status": "passed", "critical_anomalies": 0,
        })
        self.db[ENTITY_COMPARISONS].insert_one({
            "_id": "comparison_v2_v3", "status": "passed",
            "summary": {
                "status": "passed", "old_run_name": "entities_v2",
                "new_run_name": "entities_v3", "critical_findings": 0,
            },
        })
        self.audit_names = {
            "projects": "projects_audit", "works": "works_audit",
            "patents": "patents_audit", "events": "events_audit",
        }
        for entity, name in self.audit_names.items():
            collection = PROJECT_AUDITS if entity == "projects" else AUXILIARY_AUDITS
            self.db[collection].insert_one({
                "_id": name, "status": "passed_with_findings",
                "summary": {
                    "status": "passed_with_findings", "entity_run_name": "entities_v3",
                    "collection": self.destinations[entity], "critical_findings": 0,
                    "warning_findings": 2, "documents": 1,
                },
            })

    def _publisher(self):
        return ScientiEntityPublisher(
            self.db,
            run_name="entities_v3",
            comparison_name="comparison_v2_v3",
            project_audit_name=self.audit_names["projects"],
            works_audit_name=self.audit_names["works"],
            patents_audit_name=self.audit_names["patents"],
            events_audit_name=self.audit_names["events"],
        )

    def _snapshot_publisher(self):
        return ScientiEntityPublisher(
            self.db,
            run_name="entities_v3",
            comparison_name="",
            project_audit_name=self.audit_names["projects"],
            works_audit_name=self.audit_names["works"],
            patents_audit_name=self.audit_names["patents"],
            events_audit_name=self.audit_names["events"],
            allow_snapshot_without_comparison=True,
        )

    def test_publishes_atomic_current_pointer_and_is_idempotent(self):
        first = self._publisher().publish()
        second = self._publisher().publish()
        self.assertEqual(first, second)
        self.assertEqual(first["current_run_name"], "entities_v3")
        self.assertEqual(first["previous_run_name"], "entities_v2")
        self.assertEqual(first["destinations"], self.destinations)
        self.assertEqual(
            self.db[PUBLICATIONS].find_one({"_id": "current"})["current_run_name"],
            "entities_v3",
        )

    def test_refuses_a_semantic_audit_with_critical_findings(self):
        self.db[AUXILIARY_AUDITS].update_one(
            {"_id": "events_audit"},
            {"$set": {"status": "failed", "summary.status": "failed", "summary.critical_findings": 1}},
        )
        with self.assertRaisesRegex(RuntimeError, "events semantic audit did not pass"):
            self._publisher().publish()

    def test_publishes_an_initial_audited_snapshot_without_fake_comparison(self):
        published = self._snapshot_publisher().publish()
        self.assertEqual(published["current_run_name"], "entities_v3")
        self.assertEqual(published["previous_run_name"], "")
        self.assertEqual(published["publication_mode"], "audited_snapshot")
        self.assertIsNone(published["comparison_name"])

    def test_uncompared_publication_requires_explicit_snapshot_opt_in(self):
        with self.assertRaisesRegex(ValueError, "explicitly enabled"):
            ScientiEntityPublisher(
                self.db,
                run_name="entities_v3",
                comparison_name="",
                project_audit_name=self.audit_names["projects"],
                works_audit_name=self.audit_names["works"],
                patents_audit_name=self.audit_names["patents"],
                events_audit_name=self.audit_names["events"],
            )


if __name__ == "__main__":
    unittest.main()
