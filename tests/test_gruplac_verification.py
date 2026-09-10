import unittest

import mongomock

from yuku.gruplac_verification import GruplacIncompleteVerifier


class GruplacIncompleteVerifierTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        for code in ("COL1", "COL2"):
            self.db.group_raw.insert_one(
                {
                    "_id": code,
                    "url": "https://example.test/{}".format(code),
                    "html": f"reference {code}",
                    "content_sha256": f"old-{code}",
                }
            )
            self.db.group_states.insert_one(
                {
                    "_id": "gruplac:{}".format(code),
                    "kind": "gruplac",
                    "source_id": code,
                    "status": "incomplete",
                }
            )
        self.verifier = GruplacIncompleteVerifier(
            self.db,
            run_name="verify_groups",
            source_raw_collection="group_raw",
            source_state_collection="group_states",
            normalized_collection="group_normalized",
        )

    def test_target_is_frozen_across_restarts(self):
        first = self.verifier.prepare_targets()
        self.db.group_raw.insert_one(
            {"_id": "COL3", "url": "https://example.test/COL3", "html": "old"}
        )
        self.db.group_states.insert_one(
            {
                "_id": "gruplac:COL3",
                "kind": "gruplac",
                "source_id": "COL3",
                "status": "incomplete",
            }
        )
        second = self.verifier.prepare_targets()
        self.assertEqual([item["_id"] for item in first], [item["_id"] for item in second])
        self.assertEqual(len(second), 2)

    def test_promotes_only_structurally_complete_retry(self):
        targets = self.verifier.prepare_targets()
        self.db.verify_groups_raw.insert_many(
            [
                {
                    "_id": "COL1",
                    "url": "https://example.test/COL1",
                    "html": "<html><td class='celdaEncabezado'>Datos básicos</td></html>",
                    "content_sha256": "new-COL1",
                },
                {
                    "_id": "COL2",
                    "url": "https://example.test/COL2",
                    "html": "still incomplete",
                    "content_sha256": "old-COL2",
                },
            ]
        )
        self.db.verify_groups_downloads.insert_many(
            [
                {"_id": "gruplac:COL1", "status": "downloaded"},
                {"_id": "gruplac:COL2", "status": "incomplete"},
            ]
        )
        summary = self.verifier.compare_and_promote(targets)
        self.assertEqual(
            summary["outcomes"],
            {"confirmed_incomplete": 1, "promoted_downloaded": 1},
        )
        self.assertEqual(self.db.group_raw.find_one({"_id": "COL1"})["content_sha256"], "new-COL1")
        self.assertEqual(self.db.group_raw.find_one({"_id": "COL2"})["content_sha256"], "old-COL2")
        self.assertEqual(self.db.group_normalized.count_documents({}), 1)


if __name__ == "__main__":
    unittest.main()
