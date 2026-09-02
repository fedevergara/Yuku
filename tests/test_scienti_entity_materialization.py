import unittest

import mongomock

from yuku.scienti_entities import ENTITY_AUDITS, ENTITY_RUNS
from yuku.scienti_entity_materialization import (
    MATERIALIZATION_RUNS,
    ScientiEntitySnapshotMaterializer,
)


class ScientiEntitySnapshotMaterializerTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.db.events_source.insert_many([
            {"_id": "event-1", "titles": [{"title": "Evento uno"}]},
            {"_id": "event-2", "titles": [{"title": "Evento dos"}]},
        ])
        self.db.events_source.create_index("authors.id")
        self.db[ENTITY_RUNS].insert_one({
            "_id": "entities_run",
            "status": "complete",
            "config": {"destinations": {"events": "events_source"}},
        })
        self.db[ENTITY_AUDITS].insert_one({
            "_id": "entities_run",
            "status": "passed",
            "critical_anomalies": 0,
        })
        self.db.scienti_entity_publications.insert_one({
            "_id": "entities_run", "status": "current",
        })

    def materializer(self):
        return ScientiEntitySnapshotMaterializer(
            self.db,
            entity="events",
            run_name="events_materialize",
            source_collection="events_source",
            target_collection="events_final",
            entity_run_name="entities_run",
        )

    def test_materializes_exact_snapshot_atomically_and_is_idempotent(self):
        first = self.materializer().run()
        second = self.materializer().run()

        self.assertEqual(first, second)
        self.assertEqual(first["documents"], 2)
        self.assertEqual(self.db.events_final.count_documents({}), 2)
        self.assertNotIn(
            "__yuku_events_materialize_events_materialize",
            self.db.list_collection_names(),
        )
        self.assertEqual(
            self.db[MATERIALIZATION_RUNS].find_one(
                {"_id": "events_materialize"}
            )["status"],
            "complete",
        )

    def test_refuses_unpublished_source(self):
        self.db.scienti_entity_publications.update_one(
            {"_id": "entities_run"}, {"$set": {"status": "superseded"}}
        )
        with self.assertRaisesRegex(RuntimeError, "not the current publication"):
            self.materializer().run()


if __name__ == "__main__":
    unittest.main()
