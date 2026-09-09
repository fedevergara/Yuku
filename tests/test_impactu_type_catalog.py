import unittest
from collections import Counter

from kahi_impactu_type_catalog import get_impactu_catalog
from yuku.scienti_routing import route_minciencias


class ImpactuTypeCatalogTest(unittest.TestCase):
    def test_catalog_is_complete_versioned_and_reproducible(self):
        catalog = get_impactu_catalog()
        self.assertEqual(catalog.version, "1.1.0")
        self.assertEqual(len(catalog), 800)
        self.assertEqual(
            catalog.source_sha256,
            "7288727e73be698acaf6ba81799d36d56b37f9265b163a6e9a85f4489c0ecb91",
        )
        self.assertEqual(
            Counter(value["entity"] for value in catalog.payload["mappings"]),
            {"works": 731, "patents": 33, "projects": 20, "events": 16},
        )

    def test_auxiliary_sheets_are_available_through_the_shared_catalog(self):
        catalog = get_impactu_catalog()

        self.assertEqual(
            catalog.lookup("redcol", "td")["type_impactu"],
            "Tesis de posgrado",
        )
        self.assertEqual(catalog.lookup("coar", "c_12cc")["entity"], "works")
        self.assertEqual(
            catalog.lookup("eu-repo", "conferencePaper")["entity"], "works"
        )

    def test_minciencias_routes_use_exact_composite_types(self):
        examples = {
            ("Nuevo conocimiento", "Artículos de investigación"): "works",
            ("Nuevo conocimiento", "Patente de invención"): "patents",
            ("Formación de recurso humano", "Proyecto de Investigacion y Desarrollo"): "projects",
            ("Apropiación social del conocimiento", "Evento científico"): "events",
        }
        for native_type, expected in examples.items():
            with self.subTest(native_type=native_type):
                self.assertEqual(route_minciencias(*native_type)["entity"], expected)
        self.assertIsNone(
            route_minciencias(
                "Nuevo conocimiento", "Patente de invención aproximada"
            )
        )
        self.assertEqual(
            route_minciencias(
                "Apropiación social del conocimiento y divulgación pública de la ciencia",
                "Libros de Formación",
            )["impactu_type"],
            "Libro",
        )


if __name__ == "__main__":
    unittest.main()
