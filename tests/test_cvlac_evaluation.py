import unittest

import mongomock

from yuku.cvlac_evaluation import (
    CvlacProfileEvaluation,
    normalize_cod_rh,
    stable_sample,
)


class CvlacEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.db.recognized_researchers.insert_many(
            [{"cod_rh": str(value)} for value in range(1, 11)]
        )
        self.db.cvlac_stage_raw.insert_many(
            [
                {"_id": str(value).zfill(10), "html": f"old {value}"}
                for value in range(1, 6)
            ]
        )

    def test_normalizes_only_valid_codes(self):
        self.assertEqual(normalize_cod_rh("123"), "0000000123")
        self.assertEqual(normalize_cod_rh(""), "")
        self.assertEqual(normalize_cod_rh("ABC"), "")

    def test_sampling_is_deterministic(self):
        values = {str(value) for value in range(100)}
        self.assertEqual(
            stable_sample(values, 10, "seed"),
            stable_sample(reversed(tuple(values)), 10, "seed"),
        )

    def test_prepares_balanced_isolated_cohort_and_reuses_it(self):
        evaluation = CvlacProfileEvaluation(
            self.db,
            run_name="test_eval",
            new_count=3,
            refresh_count=3,
        )
        first = evaluation.prepare_cohort()
        second = evaluation.prepare_cohort()
        self.assertEqual([item["_id"] for item in first], [item["_id"] for item in second])
        self.assertEqual(
            self.db.test_eval_cohort.count_documents({"segment": "new"}), 3
        )
        self.assertEqual(
            self.db.test_eval_cohort.count_documents({"segment": "refresh"}), 3
        )
        self.assertEqual(self.db.test_eval_raw.count_documents({}), 0)
        self.assertEqual(self.db.cvlac_stage_raw.count_documents({}), 5)

    def test_rejects_configuration_change_for_same_run(self):
        CvlacProfileEvaluation(
            self.db,
            run_name="fixed_eval",
            new_count=2,
            refresh_count=2,
        ).prepare_cohort()
        changed = CvlacProfileEvaluation(
            self.db,
            run_name="fixed_eval",
            new_count=3,
            refresh_count=2,
        )
        with self.assertRaises(ValueError):
            changed.prepare_cohort()


if __name__ == "__main__":
    unittest.main()
