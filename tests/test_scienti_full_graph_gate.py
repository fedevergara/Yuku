import unittest
from unittest.mock import Mock, patch

import mongomock

from yuku.Yuku import Yuku


class ScientiFullGraphGateTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.yuku = Yuku.__new__(Yuku)
        self.yuku.db = self.db

    def seed_complete_sources(self):
        self.db.scienti_cvlac_priority_runs.insert_one(
            {
                "_id": "full_run",
                "status": "complete",
                "target_count": 2,
                "config": {"raw_collection": "full_raw"},
            }
        )
        self.db.full_raw.insert_many([{"_id": "1"}, {"_id": "2"}])
        self.db.cvlac_normalized.insert_many([{"_id": "1"}, {"_id": "2"}])
        self.db.scienti_cvlac_normalization_audits.insert_one(
            {
                "_id": "cvlac_audit",
                "status": "passed",
                "config": {"destination_collection": "cvlac_normalized"},
                "summary": {
                    "critical_anomalies": 0,
                    "stored_parser_errors": 0,
                    "source_profiles": 2,
                    "destination_profiles": 2,
                    "parser_version_counts": {"2.0.0": 2},
                },
            }
        )
        self.db.recognized_groups.insert_one(
            {"codigo_grupo": "COL1", "url_gruplac": "https://example.test/group"}
        )
        self.db.group_raw.insert_one({"_id": "COL1"})
        self.db.group_normalized.insert_one({"_id": "COL1"})
        self.db.scienti_gruplac_normalization_audits.insert_one(
            {
                "_id": "group_audit",
                "status": "passed",
                "config": {
                    "raw_collection": "group_raw",
                    "normalized_collection": "group_normalized",
                },
                "summary": {
                    "critical_anomalies": 0,
                    "stored_parser_errors": 0,
                    "expected_groups": 1,
                    "raw_groups": 1,
                    "normalized_groups": 1,
                    "parser_version_counts": {"2.0.2": 1},
                },
            }
        )

    def invoke(self):
        return self.yuku.create_scienti_full_graph(
            run_name="full_run",
            graph_run_name="graph_run",
            collection="final_graph_v1",
            cvlac_related_collection="cvlac_normalized",
            cvlac_audit_name="cvlac_audit",
            gruplac_raw_collection="group_raw",
            gruplac_related_collection="group_normalized",
            gruplac_audit_name="group_audit",
        )

    def test_rejects_graph_while_full_download_is_running(self):
        self.db.scienti_cvlac_priority_runs.insert_one(
            {"_id": "full_run", "status": "downloading"}
        )
        with self.assertRaises(RuntimeError):
            self.invoke()

    def test_rejects_graph_without_passed_audits(self):
        self.seed_complete_sources()
        self.db.scienti_gruplac_normalization_audits.update_one(
            {"_id": "group_audit"}, {"$set": {"status": "failed"}}
        )
        with self.assertRaises(RuntimeError):
            self.invoke()

    @patch("yuku.Yuku.CheckpointedNormalizedWorkGraphBuilder")
    def test_builds_from_normalized_sources_only_after_both_audits(self, builder_cls):
        self.seed_complete_sources()
        builder = Mock()
        builder.build_checkpointed.return_value = {"works": 3}
        builder_cls.return_value = builder

        self.assertEqual(self.invoke(), {"works": 3})
        builder_cls.assert_called_once()
        options = builder_cls.call_args.kwargs
        self.assertEqual(options["source_collection"], "cvlac_normalized")
        self.assertEqual(options["group_source_collection"], "group_normalized")
        self.assertEqual(options["recognized_groups_collection"], "recognized_groups")
        self.assertEqual(options["gate"]["cvlac_profiles"], 2)
        self.assertEqual(options["gate"]["gruplac_groups"], 1)
        builder.build_checkpointed.assert_called_once_with("graph_run")


if __name__ == "__main__":
    unittest.main()
