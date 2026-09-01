import unittest

import mongomock

from yuku.cvlac_work_graph import CvlacWorkGraphBuilder
from yuku.scienti_routing import route_cvlac, route_gruplac


class ScientiRoutingTest(unittest.TestCase):
    def test_cvlac_channel_contract_is_exact(self):
        self.assertEqual(
            route_cvlac(
                "production",
                {"product_type": "Software", "type_impactu": "Código fuente"},
            )["entity"],
            "works",
        )
        self.assertEqual(
            route_cvlac(
                "production",
                {"product_type": "Patente", "type_impactu": "Patente"},
            )["entity"],
            "patents",
        )
        self.assertEqual(
            route_cvlac(
                "projects", {"project_type": "Investigación y desarrollo"}
            )["entity"],
            "projects",
        )
        self.assertIsNone(
            route_cvlac(
                "projects", {"project_type": "Investigación y desarrollo regional"}
            )
        )
        for patent_type in (
            "Patente de invención", "Modelo de Utilidad", "Otra Patente",
            "Patente de Modelo Industrial", "Patente de Privilegio de Innovación",
            "Patente en el Exterior",
        ):
            with self.subTest(patent_type=patent_type):
                self.assertEqual(
                    route_cvlac("patents", {"product_type": patent_type})["entity"],
                    "patents",
                )

    def test_gruplac_excel_decisions_are_respected(self):
        specialized = (
            ({"source_section": "Proyectos", "product_type": "Proyectos", "project_type": "Investigación y desarrollo"}, "projects"),
            ({"source_section": "Eventos Científicos", "product_type": "Congreso"}, "events"),
            ({"source_section": "Signos distintivos", "product_type": "Marcas"}, "patents"),
        )
        for record, expected in specialized:
            with self.subTest(record=record):
                self.assertEqual(route_gruplac(record)["entity"], expected)

        retained_in_works = (
            {"source_section": "Diseños industriales", "product_type": "Diseño Industrial"},
            {"source_section": "Nuevas variedades vegetal", "product_type": "Variedad vegetal"},
            {"source_section": "Esquemas de trazados de circuito integrado", "product_type": "Esquema de circuito integrado"},
            {"source_section": "Softwares", "product_type": "Computacional"},
        )
        for record in retained_in_works:
            with self.subTest(record=record):
                self.assertEqual(route_gruplac(record)["entity"], "works")

    def test_work_graph_physically_excludes_other_entities(self):
        db = mongomock.MongoClient().dam
        db.cv.insert_one(
            {
                "_id": "1",
                "production": [
                    {"title": "Artículo válido", "type_impactu": "Artículo de revista"},
                    {"title": "Proyecto excluido", "type_impactu": "Proyecto"},
                    {"title": "Patente excluida", "type_impactu": "Patente"},
                ],
            }
        )
        db.groups.insert_one(
            {
                "_id": "COL1",
                "group_code": "COL1",
                "production": [
                    {"title": "Libro válido", "source_section": "Libros", "product_type": "Libro", "type_impactu": "Libro"},
                    {"title": "Diseño válido", "source_section": "Diseños industriales", "product_type": "Diseño Industrial", "type_impactu": "Modelo"},
                    {"title": "Proyecto excluido", "source_section": "Proyectos", "product_type": "Proyectos", "project_type": "Investigación y desarrollo"},
                    {"title": "Evento excluido", "source_section": "Eventos Científicos", "product_type": "Congreso"},
                    {"title": "Marca excluida", "source_section": "Signos distintivos", "product_type": "Marcas"},
                ],
            }
        )
        builder = CvlacWorkGraphBuilder(
            db,
            collection="graph",
            source_collection="cv",
            group_source_collection="groups",
            source_mode="normalized",
        )
        builder._extract_normalized_cvlac_nodes(db.nodes, None)
        builder._extract_gruplac_nodes(db.nodes)

        self.assertEqual(db.nodes.count_documents({}), 3)
        self.assertEqual(
            {value["title"] for value in db.nodes.find({}, {"title": 1})},
            {"Artículo válido", "Libro válido", "Diseño válido"},
        )
        self.assertEqual(builder.metrics["excluded_projects"], 2)
        self.assertEqual(builder.metrics["excluded_patents"], 2)
        self.assertEqual(builder.metrics["excluded_events"], 1)


if __name__ == "__main__":
    unittest.main()
