import unittest

import mongomock

from yuku.scienti_entity_graph import (
    ENTITY_GRAPH_REVIEWS,
    ScientiExactEntityGraphBuilder,
)
from yuku.scienti_entities import ENTITY_AUDITS, ENTITY_RUNS


def entity_document(
    entity,
    value_id,
    *,
    title,
    author="0000000001",
    group="",
    year=None,
    registration="",
    country="Colombia",
    identity_rule="source_scoped_insufficient_anchors",
):
    metadata = {"project_type": "Investigación y desarrollo"}
    namespace = ""
    if entity == "patents":
        metadata = {
            "registration_number": registration,
            "country": country,
            "year": year,
        }
        namespace = "patent"
    occurrence = {
        "id": f"occ-{value_id}",
        "source_kind": "cvlac",
        "source_id": author,
        "identity_namespace": namespace,
        "metadata": metadata,
    }
    document = {
        "_id": value_id,
        "updated": [],
        "titles": [{"title": title, "lang": "es", "source": "scienti"}],
        "types": [{
            "source": "scienti",
            "type": (
                "Investigación y desarrollo"
                if entity == "projects"
                else "Patente de invención"
            ),
            "level": 1,
        }],
        "external_ids": [],
        "external_urls": [],
        "authors": ([{"id": author, "full_name": f"Autor {author}", "affiliations": []}] if author else []),
        "author_count": 1 if author else 0,
        "groups": ([{"id": group, "name": "Grupo"}] if group else []),
        "ranking": [],
        "source_metadata": {
            "target_entity": entity,
            "family": "project" if entity == "projects" else "patent",
            "identity_key": f"identity-{value_id}",
            "identity_rule": identity_rule,
            "occurrences": [occurrence],
            "author_identity_conflicts": [],
            "role_resolutions": [],
        },
    }
    if entity == "projects":
        document.update({
            "abstract": "",
            "date_init": None,
            "date_end": None,
            "year_init": year,
            "year_end": None,
        })
    return document


class ScientiExactEntityGraphTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam

    def test_project_graph_links_only_shared_stable_identity_without_date_conflict(self):
        title = "Evaluación integral de ecosistemas tropicales colombianos"
        self.db.projects.insert_many([
            entity_document(
                "projects", "p1", title=title, year=2020,
                identity_rule="exact_type_title_year",
            ),
            entity_document("projects", "p2", title=title, year=None),
            entity_document(
                "projects", "p3", title=title, year=2021,
                identity_rule="exact_type_title_year",
            ),
            entity_document(
                "projects", "p4", title=title, author="0000000002", year=None,
            ),
            entity_document(
                "projects", "p5", title="Prueba", author="0000000001", year=2020,
                identity_rule="exact_type_title_year",
            ),
            entity_document(
                "projects", "p6", title="Prueba", author="0000000001", year=None,
            ),
        ])
        builder = ScientiExactEntityGraphBuilder(
            self.db,
            entity="projects",
            run_name="projects_graph_test",
            source_collection="projects",
            target_collection="projects_final",
            batch_size=2,
        )
        result = builder.run()

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["source_documents"], 6)
        self.assertEqual(result["components"], 5)
        merged = self.db.projects_final.find_one({
            "source_metadata.entity_graph.member_count": 2
        })
        self.assertEqual(merged["_id"], "p1")
        self.assertEqual(
            merged["source_metadata"]["entity_graph"]["member_ids"],
            ["p1", "p2"],
        )
        self.assertEqual(self.db.projects_final.count_documents({"titles.0.title": "Prueba"}), 2)
        self.assertGreater(
            self.db[ENTITY_GRAPH_REVIEWS].count_documents(
                {"run_name": "projects_graph_test", "reason": "conflicting_years"}
            ),
            0,
        )
        self.assertEqual(builder.run()["components"], 5)

    def test_patent_graph_blocks_conflicting_registrations(self):
        title = "Dispositivo automatizado para monitoreo ambiental continuo"
        self.db.patents.insert_many([
            entity_document(
                "patents", "a1", title=title, year=2020,
                registration="NC2020/001", identity_rule="exact_namespace_registration_country_title",
            ),
            entity_document(
                "patents", "a2", title=title, year=None, registration="",
            ),
            entity_document(
                "patents", "a3", title=title, year=2020,
                registration="NC2020/999", identity_rule="exact_namespace_registration_country_title",
            ),
        ])
        result = ScientiExactEntityGraphBuilder(
            self.db,
            entity="patents",
            run_name="patents_graph_test",
            source_collection="patents",
            target_collection="patents_final",
            batch_size=2,
        ).run()

        self.assertEqual(result["components"], 2)
        self.assertEqual(
            self.db.patents_final.count_documents(
                {"source_metadata.entity_graph.member_count": 2}
            ),
            1,
        )
        self.assertGreater(
            self.db[ENTITY_GRAPH_REVIEWS].count_documents(
                {"run_name": "patents_graph_test", "reason": "conflicting_registrations"}
            ),
            0,
        )

    def test_pipeline_gate_requires_completed_audited_publication(self):
        self.db.projects.insert_one(
            entity_document(
                "projects", "p1",
                title="Caracterización regional de sistemas productivos sostenibles",
                year=2024,
                identity_rule="exact_type_title_year",
            )
        )
        builder = ScientiExactEntityGraphBuilder(
            self.db,
            entity="projects",
            run_name="gated_project_graph",
            source_collection="projects",
            target_collection="projects_final",
            entity_run_name="entities_v4",
        )
        with self.assertRaises(RuntimeError):
            builder.run()

        self.db[ENTITY_RUNS].insert_one({
            "_id": "entities_v4",
            "status": "complete",
            "config": {"destinations": {"projects": "projects"}},
        })
        self.db[ENTITY_AUDITS].insert_one({
            "_id": "entities_v4", "status": "passed", "critical_anomalies": 0,
        })
        self.db.scienti_entity_publications.insert_one({
            "_id": "entities_v4", "status": "current",
        })
        self.assertEqual(builder.run()["status"], "complete")


if __name__ == "__main__":
    unittest.main()
