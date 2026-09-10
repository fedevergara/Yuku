import unittest
from unittest.mock import patch

import mongomock

from yuku.cvlac_priority_snapshot import CvlacPrioritySnapshot, FULL_SOURCES


class CvlacPrioritySnapshotTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.db.recognized_researchers.insert_many(
            [
                {"cod_rh": "1", "consulta_scienti": {"coincidencias": ["999"]}},
                {"cod_rh": "2"},
            ]
        )
        self.db.gruplac_production_data.insert_one({"id_persona_pd": "3"})
        self.db.all_researchers.insert_one({"cod_rh": "4"})
        self.db.cvlac_stage_raw.insert_many(
            [{"_id": "0000000001", "html": "old"}]
        )

    def snapshot(self):
        return CvlacPrioritySnapshot(
            self.db,
            run_name="priority_test",
            reference_collection="cvlac_stage_raw",
            raw_collection="cvlac_stage_raw_20260823",
        )

    def test_manifest_excludes_ambiguous_candidates_and_directory_only_ids(self):
        result = self.snapshot().prepare_manifest()
        self.assertEqual(result["target_count"], 3)
        self.assertEqual(result["segment_counts"], {"new": 2, "refresh": 1})
        self.assertEqual(
            sorted(self.db.priority_test_targets.distinct("_id")),
            ["0000000001", "0000000002", "0000000003"],
        )

    def test_ready_manifest_is_immutable_and_reused(self):
        snapshot = self.snapshot()
        first = snapshot.prepare_manifest()
        self.db.recognized_researchers.insert_one({"cod_rh": "5"})
        second = snapshot.prepare_manifest()
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["target_count"], 3)

    def test_runtime_rate_can_change_without_changing_frozen_manifest(self):
        first = self.snapshot()
        expected = first.prepare_manifest()
        changed_runtime = CvlacPrioritySnapshot(
            self.db,
            run_name="priority_test",
            reference_collection="cvlac_stage_raw",
            raw_collection="cvlac_stage_raw_20260823",
            workers=8,
            requests_per_second=4.0,
            fallback_requests_per_second=2.0,
        )
        reused = changed_runtime.prepare_manifest()
        run = self.db.scienti_cvlac_priority_runs.find_one({"_id": "priority_test"})
        self.assertEqual(reused["manifest_sha256"], expected["manifest_sha256"])
        self.assertEqual(run["config"]["requests_per_second"], 4.0)
        self.assertEqual(run["config"]["fallback_requests_per_second"], 2.0)

    def test_full_manifest_includes_directory_and_keeps_union_unique(self):
        snapshot = CvlacPrioritySnapshot(
            self.db,
            run_name="full_test",
            reference_collection="cvlac_stage_raw",
            raw_collection="cvlac_stage_raw_full",
            identifier_sources=FULL_SOURCES,
            copy_reference=True,
        )
        result = snapshot.prepare_manifest()
        self.assertEqual(result["target_count"], 4)
        self.assertEqual(result["segment_counts"], {"new": 3, "refresh": 1})
        self.assertEqual(
            sorted(self.db.full_test_targets.distinct("_id")),
            ["0000000001", "0000000002", "0000000003", "0000000004"],
        )

    def test_rejects_same_output_as_reference(self):
        with self.assertRaises(ValueError):
            CvlacPrioritySnapshot(
                self.db,
                run_name="bad_run",
                reference_collection="cvlac_stage_raw",
                raw_collection="cvlac_stage_raw",
            )

    @patch("yuku.cvlac_priority_snapshot.ScientiProfileDownloader.download")
    def test_completed_snapshot_is_idempotent(self, download):
        def save_all(_kind, targets, *, raw_collection, **_kwargs):
            targets = list(targets)
            for target in targets:
                self.db[raw_collection].insert_one({"_id": target["id"], "html": "new"})
            return {"selected": len(targets), "downloaded": len(targets)}

        download.side_effect = save_all
        snapshot = self.snapshot()
        first = snapshot.run()
        second = snapshot.run()
        self.assertEqual(first["status"], "complete")
        self.assertEqual(second["status"], "complete")
        self.assertEqual(download.call_count, 1)


if __name__ == "__main__":
    unittest.main()
