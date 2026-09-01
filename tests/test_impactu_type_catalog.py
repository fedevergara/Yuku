import unittest
from collections import Counter

from yuku.impactu_type_catalog import get_impactu_catalog
from yuku.scienti_routing import route_minciencias


class ImpactuTypeCatalogTest(unittest.TestCase):
    def test_catalog_is_complete_versioned_and_reproducible(self):
        catalog = get_impactu_catalog()
        self.assertEqual(catalog.version, "1.0.0")
        self.assertEqual(len(catalog), 606)
        self.assertEqual(
            catalog.source_sha256,
            "659bee83ffe9cef7edb67b49e04f1ec650a0e79fbdbc3df0b06ec1164ab96406",
        )
        self.assertEqual(
            Counter(value["entity"] for value in catalog.payload["mappings"]),
            {"works": 556, "patents": 21, "projects": 15, "events": 14},
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


if __name__ == "__main__":
    unittest.main()
