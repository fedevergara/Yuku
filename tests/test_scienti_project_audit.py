from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo

import mongomock

from yuku.scienti_entities import ENTITY_RUNS
from yuku.scienti_project_audit import (
    PROJECT_AUDITS,
    PROJECT_ANOMALIES,
    ScientiProjectSemanticAudit,
    date_evidence_conflicts,
    definitely_ends_before,
    parse_scienti_date,
)


BOGOTA = ZoneInfo("America/Bogota")


class ScientiDateTests(unittest.TestCase):
    def test_explicit_scienti_formats_preserve_precision(self):
        month = parse_scienti_date("Septiembre 2009")
        numeric = parse_scienti_date("2009/9")
        day = parse_scienti_date("2009-09-14")
        current = parse_scienti_date("Actual")
        self.assertEqual((month["year"], month["month"], month["precision"]), (2009, 9, "month"))
        self.assertEqual((numeric["year"], numeric["month"]), (2009, 9))
        self.assertEqual(day["precision"], "day")
        self.assertEqual(current["status"], "open")
        self.assertFalse(date_evidence_conflicts([month, numeric]))

    def test_conflicts_and_impossible_ranges_are_detected_conservatively(self):
        start = parse_scienti_date("Noviembre 2020")
        end = parse_scienti_date("2019")
        other = parse_scienti_date("2020/10")
        self.assertTrue(date_evidence_conflicts([start, other]))
        self.assertTrue(definitely_ends_before(start, end))
        self.assertEqual(parse_scienti_date("31/31/2020")["status"], "invalid")


class ScientiProjectSemanticAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.collection = "projects"
        self.entity_run = "entities_test"
        self.db[ENTITY_RUNS].insert_one(
            {
                "_id": self.entity_run,
                "status": "complete",
                "config": {
                    "router_version": "router-test",
                    "destinations": {"projects": self.collection},
                },
                "finished_at": datetime.now(timezone.utc),
            }
        )

    @staticmethod
    def _epoch(year, month, day):
        return int(datetime(year, month, day, tzinfo=BOGOTA).timestamp())

    def _project(self, project_id="p1"):
        return {
            "_id": project_id,
            "titles": [{"title": "Proyecto exacto", "lang": "es", "source": "scienti"}],
            "abstract": "Resumen verificable.",
            "types": [{"type": "Investigación y desarrollo", "source": "scienti"}],
            "date_init": self._epoch(2018, 1, 2),
            "date_end": self._epoch(2019, 2, 3),
            "year_init": 2018,
            "year_end": 2019,
            "author_count": 1,
            "authors": [{"id": "0000000123", "full_name": "Autora Ejemplo", "affiliations": []}],
            "groups": [{"id": "COL0000001", "name": "Grupo ejemplo"}],
            "source_metadata": {
                "target_entity": "projects",
                "identity_key": "project|investigacion|proyecto exacto|2018",
                "author_identity_conflicts": [],
                "occurrences": [
                    {
                        "id": f"occ-{project_id}",
                        "source_kind": "cvlac",
                        "source_id": "0000000123",
                        "metadata": {
                            "project_type": "Investigación y desarrollo",
                            "start_date": "2018-01-02",
                            "end_date": "2019-02-03",
                            "summary": "Resumen verificable.",
                        },
                    }
                ],
            },
        }

    def _run(self, audit_name="audit_test"):
        return ScientiProjectSemanticAudit(
            self.db,
            audit_name=audit_name,
            collection=self.collection,
            entity_run_name=self.entity_run,
            progress_every=100,
            batch_size=20,
            example_limit=5,
        ).run()

    def test_clean_project_passes_and_completed_audit_is_idempotent(self):
        self.db[self.collection].insert_one(self._project())
        first = self._run()
        second = self._run()
        self.assertEqual(first["status"], "passed")
        self.assertEqual(first["projects"], 1)
        self.assertEqual(first, second)
        stored = self.db[PROJECT_AUDITS].find_one({"_id": "audit_test"})
        self.assertEqual(stored["execution_attempts"], 1)
        self.assertEqual(stored["status"], "passed")

    def test_precision_loss_is_warning_with_bounded_example(self):
        project = self._project()
        project["source_metadata"]["occurrences"][0]["metadata"]["start_date"] = "Febrero 2018"
        self.db[self.collection].insert_one(project)
        result = self._run()
        self.assertEqual(result["status"], "passed_with_findings")
        self.assertEqual(result["finding_counts"]["top_start_loses_precision"], 1)
        example = self.db[PROJECT_ANOMALIES].find_one(
            {"audit_name": "audit_test", "kind": "top_start_loses_precision"}
        )
        self.assertEqual(example["project_id"], "p1")

    def test_structural_contradiction_fails_semantic_gate(self):
        project = self._project()
        project["author_count"] = 2
        project["source_metadata"]["occurrences"].append(
            dict(project["source_metadata"]["occurrences"][0])
        )
        self.db[self.collection].insert_one(project)
        result = self._run()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["finding_counts"]["author_count_mismatch"], 1)
        self.assertEqual(result["finding_counts"]["duplicate_occurrence_ids"], 1)
        self.assertEqual(result["critical_findings"], 2)


if __name__ == "__main__":
    unittest.main()
