import unittest

from yuku.cvlac_comparison import compare_records, record_fingerprint


class CvlacComparisonTest(unittest.TestCase):
    def test_list_order_does_not_create_false_change(self):
        old = {
            "profile_id": "1",
            "order": 32,
            "title": "A",
            "authors": ["Ana", "Luis"],
        }
        new = {
            "profile_id": "2",
            "order": 55,
            "title": "A",
            "authors": ["Luis", "Ana"],
        }
        self.assertEqual(record_fingerprint(old), record_fingerprint(new))

    def test_oriented_people_token_order_does_not_create_false_change(self):
        old = {
            "title": "A thesis",
            "oriented_people": ["Natalia Serrano Angie Medina"],
        }
        new = {
            "title": "A thesis",
            "oriented_people": ["Angie Medina Natalia Serrano"],
        }
        self.assertEqual(record_fingerprint(old), record_fingerprint(new))

    def test_detects_added_removed_and_unchanged_records(self):
        old = [
            {"title": "Stable", "year": 2020},
            {"title": "Removed", "year": 2021},
        ]
        new = [
            {"title": "Stable", "year": 2020},
            {"title": "Added", "year": 2025},
        ]
        result = compare_records(old, new)
        self.assertEqual(result["unchanged_count"], 1)
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["modified_record_count"], 0)
        self.assertEqual(result["delta"], 0)

    def test_detects_metadata_change_for_same_doi(self):
        old = [
            {"title": "A stable title", "year": 2024, "doi": ["https://doi.org/10.1/x"]}
        ]
        new = [
            {
                "title": "A stable title",
                "year": 2024,
                "doi": ["https://doi.org/10.1/x"],
                "keywords": ["new"],
            }
        ]
        result = compare_records(old, new)
        self.assertEqual(result["added_count"], 0)
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["modified_record_count"], 1)
        self.assertEqual(result["fingerprint_added_count"], 1)
        self.assertEqual(result["fingerprint_removed_count"], 1)
        self.assertEqual(result["modified_identity_count"], 1)

    def test_matches_minor_title_and_identifier_changes(self):
        old = [
            {
                "type_impactu": "Evento",
                "title": "9 Encuentro de Investigación en Diseño EID9",
                "year": 2021,
            }
        ]
        new = [
            {
                "type_impactu": "Evento",
                "title": "9 Encuentro de Investigación en Diseño 9EID",
                "year": 2021,
            }
        ]
        result = compare_records(old, new, "events")
        self.assertEqual(result["added_count"], 0)
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["modified_record_count"], 1)
        self.assertEqual(result["modified_field_counts"], {"title": 1})

    def test_matches_reclassified_record_with_same_title_and_year(self):
        old = [
            {"type_impactu": "Tesis de posgrado", "title": "A thesis title", "year": 2025}
        ]
        new = [
            {"type_impactu": "Tesis de pregrado", "title": "A thesis title", "year": 2025}
        ]
        result = compare_records(old, new, "production")
        self.assertEqual(result["added_count"], 0)
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["modified_record_count"], 1)
        self.assertEqual(result["modified_field_counts"], {"type_impactu": 1})


if __name__ == "__main__":
    unittest.main()
