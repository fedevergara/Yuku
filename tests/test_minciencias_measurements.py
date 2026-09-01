import unittest

import mongomock

from yuku.cvlac_work_graph import WORK_GRAPH_PUBLICATIONS
from yuku.minciencias_measurements import (
    MincienciasMeasurementPipeline,
    enrich_graph_work,
    graph_link_index_document,
    normalize_measured_product,
    resolve_measured_product_link,
)


TITLE = "A reliable measured article about tropical plant pathology"


def source_row(
    *,
    convocatoria="16",
    convocatoria_name="Convocatoria 640 de 2013",
    convocatoria_date="2013-10-31T00:00:00.000",
    category="Artículos de investigación Con Calidad A2",
    type_code="ART-ART_A2",
    group="COL0000828",
    owner="0000164771",
    title=TITLE,
    typology="Artículos de investigación",
):
    return {
        "id_convocatoria": convocatoria,
        "nme_convocatoria": convocatoria_name,
        "ano_convo": convocatoria_date,
        "id_producto_pd": "ART-0000164771-72",
        "nme_clase_pd": "Nuevo conocimiento",
        "nme_tipo_medicion_pd": "Nuevo conocimiento Top",
        "nme_tipologia_pd": typology,
        "id_tipo_pd_med": type_code,
        "nme_categoria_pd": category,
        "fcreacion_pd": "2012-08-01T00:00:00.000",
        "nme_producto_pd": title,
        "cod_grupo_gr": group,
        "nme_grupo_gr": "Biotecnología Vegetal",
        "id_persona_pd": owner,
    }


def graph_work(work_id="work-1", *, author="0000164771", group="COL0000828"):
    return {
        "_id": work_id,
        "updated": [{"source": "minciencias", "time": 1}],
        "titles": [{"title": TITLE, "lang": "en", "source": "minciencias"}],
        "doi": "https://doi.org/10.1000/reliable.2012.1",
        "year_published": 2012,
        "authors": [
            {"id": author, "full_name": "Author One", "affiliations": []}
        ],
        "author_count": 1,
        "groups": [{"id": group, "name": "Biotecnología Vegetal", "affiliations": ""}],
        "types": [
            {
                "provenance": "minciencias",
                "source": "impactu",
                "type": "Articulo de revista",
                "level": 0,
            }
        ],
        "external_ids": [],
        "keywords": [],
        "subjects": [],
    }


class MeasuredProductNormalizationTest(unittest.TestCase):
    def test_preserves_history_in_kahi_shape_without_inventing_authors(self):
        rows = [
            source_row(),
            source_row(
                convocatoria="17.0",
                convocatoria_name="Convocatoria 693 de 2014",
                convocatoria_date="2014-10-15T00:00:00.000",
                category="Artículos de investigación Con Calidad A1",
                type_code="ART-ART_A1",
            ),
        ]

        product = normalize_measured_product(rows, normalized_at=10)

        self.assertEqual(product["_id"], "ART-0000164771-72")
        self.assertEqual(product["authors"], [])
        self.assertEqual(product["author_count"], 0)
        self.assertIsNone(product["year_published"])
        self.assertIsNone(product["date_published"])
        self.assertEqual(product["groups"][0]["id"], "COL0000828")
        self.assertEqual(len(product["ranking"]), 6)
        metadata = product["bibliographic_info"]["minciencias"]
        self.assertEqual(metadata["owner_ids"], ["0000164771"])
        self.assertEqual(len(metadata["measurements"]), 2)
        self.assertEqual(metadata["measurements"][0]["convocatoria_id"], "17")
        self.assertTrue(metadata["eligible_for_works"])
        self.assertTrue(
            any(
                value.get("source") == "scienti"
                and value.get("id", {}).get("COD_PRODUCTO") == "72"
                for value in product["external_ids"]
            )
        )

    def test_routes_non_work_products_without_dropping_them(self):
        row = source_row(typology="Patente de invención")
        product = normalize_measured_product([row], normalized_at=10)
        metadata = product["bibliographic_info"]["minciencias"]
        self.assertEqual(metadata["target_entity"], "patents")
        self.assertFalse(metadata["eligible_for_works"])
        self.assertEqual(product["titles"][0]["title"], TITLE)


class MeasuredProductLinkTest(unittest.TestCase):
    def setUp(self):
        self.product = normalize_measured_product([source_row()], normalized_at=10)

    def test_links_one_anchored_candidate(self):
        candidate = graph_link_index_document(graph_work())
        link = resolve_measured_product_link(
            self.product, [candidate], graph_collection="graph_v2"
        )
        self.assertEqual(link["status"], "linked")
        self.assertEqual(link["work_id"], "work-1")
        self.assertEqual(link["rule"], "title_year_owner_group")

    def test_keeps_multiple_anchored_candidates_ambiguous(self):
        candidates = [
            graph_link_index_document(graph_work("work-1")),
            graph_link_index_document(graph_work("work-2")),
        ]
        link = resolve_measured_product_link(
            self.product, candidates, graph_collection="graph_v2"
        )
        self.assertEqual(link["status"], "ambiguous")
        self.assertEqual(link["work_id"], "")

    def test_enrichment_never_changes_scraped_authors(self):
        work = graph_work()
        original_authors = work["authors"]
        link = resolve_measured_product_link(
            self.product,
            [graph_link_index_document(work)],
            graph_collection="graph_v2",
        )

        enriched = enrich_graph_work(work, [self.product], [link], enriched_at=20)

        self.assertEqual(enriched["authors"], original_authors)
        self.assertEqual(enriched["author_count"], 1)
        self.assertEqual(
            enriched["bibliographic_info"]["minciencias"]["product_ids"],
            ["ART-0000164771-72"],
        )
        self.assertTrue(
            any(
                value.get("id") == "ART-0000164771-72"
                for value in enriched["external_ids"]
            )
        )


class MeasurementPipelineIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.pipeline = MincienciasMeasurementPipeline(self.db)
        self.db.gruplac_production_data.insert_many(
            [
                source_row(),
                source_row(
                    convocatoria="17",
                    convocatoria_name="Convocatoria 693 de 2014",
                    convocatoria_date="2014-10-15T00:00:00.000",
                    category="Artículos de investigación Con Calidad A1",
                    type_code="ART-ART_A1",
                ),
                {
                    **source_row(
                        title="An official product absent from the scraped graph",
                        owner="0000999999",
                        group="COL0999999",
                    ),
                    "id_producto_pd": "ART-0000999999-1",
                },
            ]
        )
        self.db.graph_v2.insert_many(
            [
                graph_work(),
                graph_work(
                    "scraped-only", author="0000888888", group="COL0888888"
                )
                | {
                    "titles": [
                        {
                            "title": "A scraped product absent from official measurements",
                            "lang": "en",
                            "source": "minciencias",
                        }
                    ],
                    "year_published": 2020,
                },
            ]
        )
        self.db[WORK_GRAPH_PUBLICATIONS].insert_one(
            {
                "_id": "current",
                "current_collection": "graph_v2",
                "current_run_name": "graph_v2_run",
            }
        )

    def test_full_small_pipeline_is_inclusive_and_publishes_atomically(self):
        normalized = self.pipeline.normalize(
            run_name="measure_normalize",
            source_collection="gruplac_production_data",
            destination_collection="measured_products",
            batch_size=1,
            progress_every=100,
        )
        self.assertEqual(normalized["products"], 2)

        linked = self.pipeline.link(
            run_name="measure_link",
            measured_collection="measured_products",
            graph_collection="graph_v2",
            links_collection="measured_links",
            batch_size=1,
            progress_every=100,
        )
        self.assertEqual(linked["status_counts"]["linked"], 1)
        self.assertEqual(linked["status_counts"]["unlinked"], 1)

        materialized = self.pipeline.materialize(
            run_name="measure_graph",
            graph_collection="graph_v2",
            measured_collection="measured_products",
            links_collection="measured_links",
            target_collection="graph_v3",
            batch_size=1,
            progress_every=100,
        )
        self.assertEqual(materialized["works"], 2)
        self.assertEqual(materialized["enriched_works"], 1)
        self.assertEqual(materialized["official_products"], 1)
        self.assertEqual(materialized["unlinked_official_products"], 1)
        self.assertEqual(self.db.graph_v3.count_documents({}), 2)
        self.assertIsNotNone(self.db.graph_v3.find_one({"_id": "scraped-only"}))
        self.assertIsNotNone(
            self.db.measured_products.find_one({"_id": "ART-0000999999-1"})
        )
        current = self.db[WORK_GRAPH_PUBLICATIONS].find_one({"_id": "current"})
        self.assertEqual(current["current_collection"], "graph_v3")
        self.assertEqual(current["previous_collection"], "graph_v2")


if __name__ == "__main__":
    unittest.main()
