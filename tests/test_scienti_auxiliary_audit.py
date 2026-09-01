from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo

import mongomock

from yuku.scienti_auxiliary_audit import AUDITS, ScientiAuxiliarySemanticAudit
from yuku.scienti_entities import ENTITY_NORMALIZER_VERSION, ENTITY_RUNS


BOGOTA = ZoneInfo("America/Bogota")


class ScientiAuxiliarySemanticAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.destinations = {
            "works": "works", "projects": "projects", "patents": "patents", "events": "events"
        }
        self.db[ENTITY_RUNS].insert_one(
            {
                "_id": "entities_test",
                "status": "complete",
                "finished_at": datetime.now(timezone.utc),
                "config": {
                    "router_version": "router-test",
                    "normalizer_version": ENTITY_NORMALIZER_VERSION,
                    "destinations": self.destinations,
                },
            }
        )

    @staticmethod
    def _base(entity, identity, family, occurrence):
        return {
            "_id": f"id-{entity}",
            "titles": [{"title": f"Registro verificable {entity}"}],
            "updated": [{"source": "scienti", "time": 1}],
            "types": [{"source": "scienti", "type": occurrence["product_type"]}],
            "author_count": 1,
            "authors": [{"id": "0000000001", "full_name": "Persona Verificable", "affiliations": []}],
            "groups": [],
            "source_metadata": {
                "target_entity": entity,
                "normalizer_version": ENTITY_NORMALIZER_VERSION,
                "family": family,
                "identity_key": identity,
                "identity_rule": "exact",
                "author_identity_conflicts": [],
                "occurrences": [occurrence],
            },
        }

    @staticmethod
    def _occ(identifier, product_type, route_rule, metadata, namespace=None):
        value = {
            "id": identifier,
            "source_kind": "cvlac",
            "source_id": "0000000001",
            "route_rule": route_rule,
            "product_type": product_type,
            "metadata": metadata,
        }
        if namespace:
            value["identity_namespace"] = namespace
        return value

    def _run(self, entity, name=None):
        return ScientiAuxiliarySemanticAudit(
            self.db,
            audit_name=name or f"audit_{entity}",
            entity=entity,
            collection=self.destinations[entity],
            entity_run_name="entities_test",
            progress_every=100,
            batch_size=20,
            example_limit=5,
        ).run()

    def test_clean_thesis_passes_and_is_idempotent(self):
        occurrence = self._occ(
            "occ-work", "Trabajos de grado de pregrado", "cvlac_exact_thesis_type",
            {"institution": "Universidad verificable"},
        )
        document = self._base(
            "works", "thesis|undergraduate_thesis|registro|2024|estudiante",
            "undergraduate_thesis", occurrence,
        )
        document["types"].append({"source": "impactu", "type": "Tesis de Pregrado"})
        document["date_published"] = int(datetime(2024, 1, 1, tzinfo=BOGOTA).timestamp())
        document["year_published"] = 2024
        document["authors"] = [
            {"id": "0000000002", "full_name": "Estudiante Verificable", "affiliations": []},
            {"id": "0000000001", "full_name": "Asesora Verificable", "affiliations": [], "type": "advisor"},
        ]
        document["author_count"] = 2
        self.db.works.insert_one(document)
        first = self._run("works")
        second = self._run("works")
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "passed")
        self.assertEqual(self.db[AUDITS].find_one({"_id": "audit_works"})["execution_attempts"], 1)

    def test_clean_general_work_passes_without_thesis_findings(self):
        occurrence = self._occ(
            "occ-work", "Artículo científico", "cvlac_production_channel",
            {"institution": "Universidad verificable"},
        )
        document = self._base(
            "works", "source|works|cvlac|0000000001|0|registro verificable",
            "work", occurrence,
        )
        document["date_published"] = int(datetime(2024, 1, 1, tzinfo=BOGOTA).timestamp())
        document["year_published"] = 2024
        self.db.works.insert_one(document)

        result = self._run("works")

        self.assertEqual(result["status"], "passed")
        self.assertNotIn("wrong_family", result["finding_counts"])
        self.assertNotIn("source_scoped_identity", result["finding_counts"])
        self.assertNotIn("missing_student", result["finding_counts"])
        self.assertNotIn("missing_advisor", result["finding_counts"])

    def test_graduate_degree_work_accepts_docencia_impactu_type(self):
        occurrence = self._occ(
            "occ-monograph",
            "Monografía de conclusión de curso de perfeccionamiento/especialización",
            "gruplac_exact_thesis_type",
            {"institution": "Universidad verificable"},
        )
        document = self._base(
            "works",
            "thesis|graduate_degree_work|registro verificable|2024|estudiante",
            "graduate_degree_work",
            occurrence,
        )
        document["types"].append({"source": "impactu", "type": "Docencia"})
        document["authors"] = [
            {"id": "0000000002", "full_name": "Estudiante Verificable", "affiliations": []},
            {"id": "0000000001", "full_name": "Asesora Verificable", "affiliations": [], "type": "advisor"},
        ]
        document["author_count"] = 2
        self.db.works.insert_one(document)

        result = self._run("works")

        self.assertEqual(result["status"], "passed")
        self.assertNotIn("unexpected_impactu_thesis_type", result["finding_counts"])

    def test_clean_patent_passes(self):
        occurrence = self._occ(
            "occ-patent", "Patente de invención", "cvlac_exact_patent_type",
            {
                "registration_number": "NC-123",
                "presentation_date": "2024-02-03",
                "country": "Colombia",
                "applicant": "Universidad verificable",
            },
            namespace="patent",
        )
        document = self._base(
            "patents", "ip|patent|registration|nc-123|country|colombia|title|registro",
            "patent", occurrence,
        )
        self.db.patents.insert_one(document)
        result = self._run("patents")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["critical_findings"], 0)

    def test_clean_event_passes(self):
        occurrence = self._occ(
            "occ-event", "Congreso", "cvlac_exact_event_type",
            {"event_type": "Congreso", "scope": "Nacional", "start_date": "2024-05-06", "end_date": "2024-05-07"},
        )
        document = self._base(
            "events", "event|congreso|registro verificable|2024-05-06", "event", occurrence,
        )
        document["date_held"] = int(datetime(2024, 5, 6, tzinfo=BOGOTA).timestamp())
        document["year_held"] = 2024
        self.db.events.insert_one(document)
        result = self._run("events")
        self.assertEqual(result["status"], "passed")

    def test_missing_patent_metadata_is_warned_without_inference(self):
        occurrence = self._occ(
            "occ-patent", "Patente de invención", "cvlac_exact_patent_type",
            {"country": "Colombia"}, namespace="patent",
        )
        document = self._base(
            "patents", "ip|patent|source|cvlac|1|registro", "patent", occurrence,
        )
        self.db.patents.insert_one(document)
        result = self._run("patents")
        self.assertEqual(result["status"], "passed_with_findings")
        self.assertEqual(result["finding_counts"]["missing_registration_number"], 1)
        self.assertEqual(result["finding_counts"]["missing_ip_rights_holder"], 1)
        self.assertEqual(result["finding_counts"]["missing_patent_presentation_date"], 1)
        self.assertNotIn("applicant", occurrence["metadata"])

    def test_sanitized_event_source_problem_is_a_warning_not_invalid_data(self):
        occurrence = self._occ(
            "occ-event", "Congreso", "cvlac_exact_event_type",
            {"event_type": "Congreso", "scope": "Nacional", "start_date": None, "end_date": None},
        )
        occurrence["normalization_findings"] = [
            {"kind": "source_invalid_event_start_date", "field": "start_date"}
        ]
        document = self._base(
            "events", "source|events|cvlac|1|registro", "event", occurrence,
        )
        document["date_held"] = None
        document["year_held"] = None
        self.db.events.insert_one(document)
        result = self._run("events")
        self.assertEqual(result["status"], "passed_with_findings")
        self.assertEqual(result["finding_counts"]["source_invalid_event_start_date"], 1)
        self.assertNotIn("invalid_event_start_date", result["finding_counts"])

    def test_mixed_patent_namespaces_fail(self):
        occurrence = self._occ(
            "occ-patent", "Patente", "cvlac_exact_patent_type",
            {"registration_number": "NC-123"}, namespace="patent",
        )
        document = self._base(
            "patents", "ip|patent|registration|nc-123|country|unknown|title|registro",
            "patent", occurrence,
        )
        second = dict(occurrence)
        second.update({"id": "occ-trademark", "identity_namespace": "trademark"})
        document["source_metadata"]["occurrences"].append(second)
        self.db.patents.insert_one(document)
        result = self._run("patents")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["finding_counts"]["mixed_ip_namespaces"], 1)

    def test_presentation_only_duplicate_author_is_warning(self):
        occurrence = self._occ(
            "occ-work", "Trabajos de grado de pregrado", "cvlac_exact_thesis_type", {},
        )
        document = self._base(
            "works", "thesis|undergraduate_thesis|registro|2024|estudiante",
            "undergraduate_thesis", occurrence,
        )
        document["types"].append({"source": "impactu", "type": "Tesis de Pregrado"})
        document["authors"] = [
            {"id": "", "full_name": "Mónica Rodríguez", "affiliations": [], "type": "advisor"},
            {"id": "0000000001", "full_name": "MONICA RODRIGUEZ", "affiliations": [], "type": "advisor"},
        ]
        document["author_count"] = 2
        self.db.works.insert_one(document)
        result = self._run("works")
        self.assertEqual(result["status"], "passed_with_findings")
        self.assertEqual(result["finding_counts"]["probable_duplicate_author_names"], 1)


if __name__ == "__main__":
    unittest.main()
