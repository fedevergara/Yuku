from copy import deepcopy
import unittest

import mongomock

from yuku.scienti_entities import ENTITY_KEYS, ENTITY_NORMALIZER_VERSION, ENTITY_RUNS, _timestamp
from yuku.scienti_entity_comparison import (
    ENTITY_COMPARISONS,
    ScientiEntityVersionComparator,
    _old_timestamp,
)


class ScientiEntityVersionComparatorTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.old_destinations = {entity: f"old_{entity}" for entity in ENTITY_KEYS}
        self.new_destinations = {entity: f"new_{entity}" for entity in ENTITY_KEYS}
        for name, destinations in (
            ("entities_old", self.old_destinations),
            ("entities_new", self.new_destinations),
        ):
            self.db[ENTITY_RUNS].insert_one(
                {
                    "_id": name,
                    "status": "complete",
                    "config_hash": f"hash-{name}",
                    "config": {"destinations": destinations},
                }
            )
        for entity in ENTITY_KEYS:
            occurrence = {
                "id": f"occ-{entity}",
                "source_kind": "cvlac",
                "source_id": "0000000001",
                "metadata": {},
            }
            old = {
                "_id": f"id-{entity}",
                "titles": [{"title": f"Registro {entity}"}],
                "updated": [{"source": "scienti", "time": 1}],
                "source_metadata": {"occurrences": [occurrence]},
            }
            new = deepcopy(old)
            new["updated"][0]["time"] = 2
            new["source_metadata"]["normalizer_version"] = ENTITY_NORMALIZER_VERSION
            if entity == "projects":
                occurrence["metadata"].update(
                    {"start_date": "Septiembre 2009", "end_date": "Febrero 2010"}
                )
                old["date_init"] = _old_timestamp("Septiembre 2009")
                old["date_end"] = _old_timestamp("Febrero 2010")
                new["date_init"] = _timestamp("Septiembre 2009")
                new["date_end"] = _timestamp("Febrero 2010")
            elif entity == "events":
                occurrence["metadata"]["start_date"] = "Marzo 2020"
                old["date_held"] = _old_timestamp("Marzo 2020")
                new["date_held"] = _timestamp("Marzo 2020")
            new["source_metadata"]["occurrences"] = deepcopy(
                old["source_metadata"]["occurrences"]
            )
            self.db[self.old_destinations[entity]].insert_one(old)
            self.db[self.new_destinations[entity]].insert_one(new)

    def _run(self, name="comparison_test"):
        return ScientiEntityVersionComparator(
            self.db,
            comparison_name=name,
            old_run_name="entities_old",
            new_run_name="entities_new",
            progress_every=1,
            batch_size=2,
            example_limit=3,
        ).run()

    def test_only_declared_version_date_and_update_changes_pass(self):
        first = self._run()
        second = self._run()
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "passed")
        self.assertEqual(first["critical_findings"], 0)
        self.assertEqual(first["metrics"]["projects"]["date_init.changed_expected"], 1)
        self.assertEqual(first["metrics"]["events"]["date_held.changed_expected"], 1)
        stored = self.db[ENTITY_COMPARISONS].find_one({"_id": "comparison_test"})
        self.assertEqual(stored["execution_attempts"], 1)

    def test_any_other_metadata_change_fails(self):
        self.db[self.new_destinations["patents"]].update_one(
            {"_id": "id-patents"}, {"$set": {"titles.0.title": "Título alterado"}}
        )
        result = self._run()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["finding_counts"]["patents.unexpected_metadata_difference"], 1
        )

    def _enable_v3_transition(self):
        self.db[ENTITY_RUNS].update_one(
            {"_id": "entities_old"},
            {"$set": {"config.normalizer_version": "scienti-entity-normalizer-v2"}},
        )
        self.db[ENTITY_RUNS].update_one(
            {"_id": "entities_new"},
            {"$set": {"config.normalizer_version": "scienti-entity-normalizer-v3"}},
        )
        for entity in ENTITY_KEYS:
            self.db[self.old_destinations[entity]].update_one(
                {}, {"$set": {"source_metadata.normalizer_version": "scienti-entity-normalizer-v2"}}
            )
            self.db[self.new_destinations[entity]].update_one(
                {}, {"$set": {"source_metadata.role_resolutions": []}}
            )
        self.db[self.old_destinations["works"]].update_one(
            {}, {"$set": {"authors": [], "author_count": 0}}
        )
        self.db[self.new_destinations["works"]].update_one(
            {}, {"$set": {"authors": [], "author_count": 0}}
        )
        old_project = self.db[self.old_destinations["projects"]].find_one({})
        self.db[self.new_destinations["projects"]].update_one(
            {},
            {"$set": {
                "date_init": old_project.get("date_init"),
                "date_end": old_project.get("date_end"),
            }},
        )

    def test_v3_exact_roles_and_event_sanitization_are_verified(self):
        self._enable_v3_transition()
        old_work = self.db[self.old_destinations["works"]].find_one({})
        old_work["authors"] = [
            {"id": "", "full_name": "Ana Estudiante", "affiliations": []},
            {"id": "", "full_name": "ANA ESTUDIANTE", "affiliations": [], "type": "advisor"},
        ]
        old_work["author_count"] = 2
        self.db[self.old_destinations["works"]].replace_one({"_id": old_work["_id"]}, old_work)
        new_work = deepcopy(old_work)
        new_work["updated"][0]["time"] = 2
        new_work["authors"] = [old_work["authors"][0]]
        new_work["author_count"] = 1
        new_work["source_metadata"]["normalizer_version"] = ENTITY_NORMALIZER_VERSION
        new_work["source_metadata"]["role_resolutions"] = [{
            "rule": "student_precedence_exact_name",
            "name": "ANA ESTUDIANTE",
            "discarded_advisor_id": "",
        }]
        self.db[self.new_destinations["works"]].replace_one({"_id": new_work["_id"]}, new_work)

        old_event = self.db[self.old_destinations["events"]].find_one({})
        old_event["source_metadata"]["occurrences"][0]["metadata"] = {
            "start_date": "2024-05-06",
            "end_date": "2024-05-05",
            "scope": "Internacional Tipo de evento: Congreso",
        }
        old_event["date_held"] = _timestamp("2024-05-06")
        old_event["year_held"] = 2024
        self.db[self.old_destinations["events"]].replace_one({"_id": old_event["_id"]}, old_event)
        new_event = deepcopy(old_event)
        new_event["updated"][0]["time"] = 2
        new_event["source_metadata"]["normalizer_version"] = ENTITY_NORMALIZER_VERSION
        new_event["source_metadata"]["role_resolutions"] = []
        occurrence = new_event["source_metadata"]["occurrences"][0]
        occurrence["metadata"]["end_date"] = None
        occurrence["metadata"]["scope"] = "Internacional"
        occurrence["normalization_findings"] = [
            {"kind": "source_event_end_before_start", "field": "end_date"},
            {"kind": "source_event_scope_normalized", "field": "scope"},
        ]
        self.db[self.new_destinations["events"]].replace_one({"_id": new_event["_id"]}, new_event)

        result = self._run("semantic_v3_comparison")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["transition_profile"], "semantic_hardening_v3")
        self.assertEqual(result["critical_findings"], 0)

    def test_v3_invalid_event_reidentification_preserves_occurrence(self):
        self._enable_v3_transition()
        old_event = self.db[self.old_destinations["events"]].find_one({})
        old_event["source_metadata"]["identity_key"] = "event|congreso|registro|2024-02-31"
        old_event["source_metadata"]["identity_rule"] = "exact_type_title_start_date"
        old_event["source_metadata"]["occurrences"][0]["metadata"] = {
            "start_date": "2024-02-31", "end_date": "", "scope": "Nacional"
        }
        self.db[self.old_destinations["events"]].replace_one({"_id": old_event["_id"]}, old_event)
        new_event = deepcopy(old_event)
        new_event["_id"] = "reidentified-event"
        new_event["updated"][0]["time"] = 2
        new_event["source_metadata"]["normalizer_version"] = ENTITY_NORMALIZER_VERSION
        new_event["source_metadata"]["role_resolutions"] = []
        new_event["source_metadata"]["identity_key"] = "source|events|cvlac|0000000001|0|registro"
        new_event["source_metadata"]["identity_rule"] = "source_scoped_insufficient_anchors"
        occurrence = new_event["source_metadata"]["occurrences"][0]
        occurrence["metadata"] = {
            "start_date": None, "end_date": None, "scope": "Nacional"
        }
        occurrence["normalization_findings"] = [
            {"kind": "source_invalid_event_start_date", "field": "start_date"}
        ]
        self.db[self.new_destinations["events"]].delete_many({})
        self.db[self.new_destinations["events"]].insert_one(new_event)
        result = self._run("semantic_v3_reidentification")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["metrics"]["events"]["reidentified_occurrences_verified"], 1
        )


if __name__ == "__main__":
    unittest.main()
