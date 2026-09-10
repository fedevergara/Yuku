import unittest

import mongomock

from yuku.Yuku import Yuku


class CvlacRelatedWorksResumeTest(unittest.TestCase):
    def test_resume_skips_existing_profiles_and_processes_missing_profiles(self):
        db = mongomock.MongoClient().dam
        db.raw_profiles.insert_many(
            [
                {"_id": "0000000001", "html": "<html></html>"},
                {"_id": "0000000002", "html": "<html></html>"},
            ]
        )
        db.normalized_profiles.insert_one(
            {"_id": "0000000001", "production": [{"preserved": True}]}
        )
        yuku = Yuku.__new__(Yuku)
        yuku.db = db

        summary = yuku.create_cvlac_related_works_collection(
            collection="normalized_profiles",
            source_collection="raw_profiles",
            resume=True,
        )

        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["destination_profiles"], 2)
        self.assertEqual(
            db.normalized_profiles.find_one({"_id": "0000000001"})["production"],
            [{"preserved": True}],
        )


if __name__ == "__main__":
    unittest.main()
