import unittest

from bs4 import BeautifulSoup
import mongomock

from yuku.gruplac_related_works import normalize_gruplac_document
from yuku.scienti_affiliations import (
    AFFILIATION_AUDITS,
    AFFILIATION_FIELDS,
    AFFILIATION_PUBLICATIONS,
    ScientiAffiliationMaterializer,
)


class GruplacGroupMetadataTests(unittest.TestCase):
    def test_normalizer_preserves_public_group_metadata_without_identity_guessing(self):
        html = """
        <span class="celdaEncabezado">Grupo Público</span>
        <table><tr><td class="celdaEncabezado">Datos básicos</td></tr>
          <tr><td class="celdasTitulo">Año y mes de formación</td><td>2001 - 4</td></tr>
        </table>
        <table><tr><td class="celdaEncabezado">Instituciones</td></tr>
          <tr><td>1.- UNIVERSIDAD EJEMPLO - (Avalado)</td></tr>
        </table>
        <table><tr><td class="celdaEncabezado">Plan Estratégico</td></tr>
          <tr><td>Plan de trabajo: Plan verificable Estado del arte: Estado
          Objetivos: Objetivo Retos: Reto Visión: Visión verificable</td></tr>
        </table>
        <table><tr><td class="celdaEncabezado">Líneas de investigación declaradas por el grupo</td></tr>
          <tr><td>1.- Línea verificable</td></tr>
        </table>
        """

        result = normalize_gruplac_document("COL0000001", html)

        self.assertEqual(result["institutions"], [
            {"name": "UNIVERSIDAD EJEMPLO", "endorsed": True}
        ])
        self.assertEqual(result["strategic_plan"]["TXT_PLAN_TRABAJO"], "Plan verificable")
        self.assertEqual(result["strategic_plan"]["TXT_VISION"], "Visión verificable")
        self.assertEqual(result["research_lines"], ["Línea verificable"])


class ScientiAffiliationMaterializerTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.db.recognized_groups.insert_many([
            {
                "codigo_grupo": "COL0000001",
                "nombre_preferido": '"Grupo Uno Histórico"',
                "nombres_historicos": [
                    {"año": 2018, "nombre": "Grupo Uno Anterior"}
                ],
                "instituciones_historicas": [
                    {"año": 2018, "institucion": "Universidad Histórica"}
                ],
                "clasificaciones_historicas": [
                    {"año": 2018, "clasificacion": "B"}
                ],
                "url_gruplac": "https://scienti.example/gruplac?nro=123",
            },
            {"codigo_grupo": "COL0000002", "nombre_grupo": "Grupo Dos"},
        ])
        self.db.gruplac_groups_data.insert_many([
            {
                "cod_grupo_gr": "COL0000001",
                "nme_grupo_gr": "Grupo Uno Abierto",
                "ano_convo": "2024-05-23T00:00:00.000",
                "fcreacion_gr": "2001-04-01T00:00:00.000",
                "inst_aval": "Universidad Actual",
                "nme_clasificacion_gr": "00",
                "orden_clas_gr": "1.0",
                "nme_departamento_gr": "Antioquia",
                "nme_municipio_gr": "Medellín",
                "nme_pais_gr": "Colombia",
                "id_area_con_gr": "1F",
                "nme_gran_area_gr": "Ciencias Naturales",
                "nme_area_gr": "Ciencias Biológicas",
                "nme_prog_colc1_gr": "Programa principal",
            },
            {
                "cod_grupo_gr": "COL0000003",
                "nme_grupo_gr": "Grupo Solo Abierto",
                "ano_convo": "2015-01-01T00:00:00.000",
                "nme_clasificacion_gr": "C",
            },
        ])
        self.db.gruplac_normalized.insert_one({
            "_id": "COL0000001",
            "group_code": "COL0000001",
            "group_name": "Grupo Uno Perfil",
            "nro": "123",
            "url_gruplac": "https://scienti.example/gruplac?nro=123",
            "basic": {"Año y mes de formación": "2001 - 4"},
            "institutions": [
                {"name": "Universidad Actual", "endorsed": True}
            ],
            "strategic_plan": {"TXT_OBJETIVOS": "Objetivo público"},
            "research_lines": ["Biología pública"],
            "parser": {"version": "3.2.0"},
            "source": {"content_sha256": "abc"},
        })
        self.db.scienti_gruplac_normalization_audits.insert_one({
            "_id": "gruplac_audit",
            "status": "passed",
            "config": {
                "normalized_collection": "gruplac_normalized",
                "recognized_groups_collection": "recognized_groups",
                "expected_parser_version": "3.2.0",
            },
            "summary": {"critical_anomalies": 0},
        })

    def materializer(self, run_name="affiliations_v1", target="affiliations_final"):
        return ScientiAffiliationMaterializer(
            self.db,
            run_name=run_name,
            recognized_collection="recognized_groups",
            open_data_collection="gruplac_groups_data",
            gruplac_collection="gruplac_normalized",
            gruplac_audit_name="gruplac_audit",
            target_collection=target,
            batch_size=2,
            progress_every=100,
            expected_groups=3,
        )

    def test_materializes_union_audits_and_publishes_atomically(self):
        first = self.materializer().run()
        second = self.materializer().run()

        self.assertEqual(first, second)
        self.assertEqual(first["documents"], 3)
        self.assertNotIn("__yuku_affiliations_v1_affiliations", self.db.list_collection_names())
        group = self.db.affiliations_final.find_one({"_id": "COL0000001"})
        self.assertEqual(set(group), {"_id"} | AFFILIATION_FIELDS)
        self.assertEqual(group["names"][0]["name"], "Grupo Uno Perfil")
        self.assertIn("Grupo Uno Histórico", group["aliases"])
        self.assertEqual(group["year_established"], 2001)
        self.assertEqual(group["addresses"][0]["city"], "Medellín")
        self.assertEqual(group["ranking"][-1]["rank"], "Reconocido")
        self.assertEqual(group["relations"][0], {
            "id": "", "name": "Universidad Actual", "types": []
        })
        self.assertEqual(group["description"][0]["description"], {
            "TXT_OBJETIVOS": "Objetivo público"
        })
        self.assertEqual(
            self.db[AFFILIATION_AUDITS].find_one({"_id": "affiliations_v1_audit"})["status"],
            "passed",
        )
        self.assertEqual(
            self.db[AFFILIATION_PUBLICATIONS].find_one({"_id": "current"})["collection"],
            "affiliations_final",
        )

    def test_refuses_unproven_normalized_gruplac_source(self):
        self.db.scienti_gruplac_normalization_audits.update_one(
            {"_id": "gruplac_audit"}, {"$set": {"status": "failed"}}
        )
        with self.assertRaisesRegex(RuntimeError, "source audit is not passed"):
            self.materializer().run()

    def test_refuses_resume_after_source_changes(self):
        materializer = self.materializer()
        materializer._prepare()
        self.db.gruplac_groups_data.insert_one({
            "cod_grupo_gr": "COL0000004", "nme_grupo_gr": "Cambio tardío"
        })
        with self.assertRaisesRegex(RuntimeError, "sources changed"):
            materializer.run()


if __name__ == "__main__":
    unittest.main()
