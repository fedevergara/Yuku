import unittest
from hashlib import sha256

import mongomock

from yuku.cvlac_related_works import normalize_related_works_document
from yuku.scienti_persons import (
    PERSON_AUDITS,
    PERSON_FIELDS,
    PERSON_PUBLICATIONS,
    ScientiPersonMaterializer,
    canonical_doi,
    parse_profile_period,
)


class CvlacPersonProfileTests(unittest.TestCase):
    def test_normalizer_preserves_explicit_public_person_metadata(self):
        html = """
        <div><a name="datos_generales"></a><table>
          <tr><td>Nombre</td><td>Ana María Ejemplo</td></tr>
          <tr><td>Nombre en citaciones</td><td>EJEMPLO, ANA M.</td></tr>
          <tr><td>Sexo</td><td>Femenino</td></tr>
        </table></div>
        <div><a name="red_identificadores"></a><table><tr><td>
          <a href="https://orcid.org/0000-0002-1825-0097">ORCID</a>
        </td></tr></table></div>
        <div><a name="formacion_acad"></a><table><tr><td></td><td>
          <b>Doctorado</b><br/>Universidad Ejemplo<br/>Doctorado en Ciencias<br/>2018 - 2022
        </td></tr></table></div>
        <div><a name="experiencia"></a><table><tr><td></td><td>
          <b>Universidad Ejemplo</b><br/>Dedicación: 40 horas Semanales Enero de 2023
        </td></tr></table></div>
        """

        profile = normalize_related_works_document("0000000001", html)["profile"]

        self.assertEqual(profile["general"]["name"], "Ana María Ejemplo")
        self.assertEqual(profile["general"]["sex"], "Femenino")
        self.assertEqual(profile["identifiers"][0]["url"], (
            "https://orcid.org/0000-0002-1825-0097"
        ))
        self.assertEqual(profile["degrees"][0]["level"], "Doctorado")
        self.assertEqual(
            profile["experiences"][0]["institution"], "Universidad Ejemplo"
        )

    def test_empty_profile_section_does_not_capture_the_following_table(self):
        html = """
        <div><a name="experiencia"></a></div>
        <div><table><tr><td><b>Not an employer</b></td></tr></table></div>
        """

        profile = normalize_related_works_document("0000000001", html)["profile"]

        self.assertEqual(profile["experiences"], [])


class ScientiPersonMaterializerTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        manifest = [
            {"_id": "0000000000", "run_name": "manifest_run",
             "segment": "new", "sources": ["test"]},
            {"_id": "0000000001", "run_name": "manifest_run",
             "segment": "new", "sources": ["test"]},
            {"_id": "0000000002", "run_name": "manifest_run",
             "segment": "refresh", "sources": ["test"]},
        ]
        self.db.manifest.insert_many(manifest)
        digest = sha256()
        for item in manifest:
            digest.update(
                f"{item['_id']}|{item['segment']}|test\n".encode("utf-8")
            )
        self.db.scienti_cvlac_priority_runs.insert_one({
            "_id": "manifest_run", "status": "complete",
            "manifest_status": "ready", "manifest_sha256": digest.hexdigest(),
            "target_count": 3,
            "config": {"manifest_collection": "manifest"},
        })
        self.db.directory.insert_many([
            {"cod_rh": "0000000001", "nombre_completo": "ANA MARIA EJEMPLO",
             "nivel_formacion": "Doctorado"},
            {"cod_rh": "0000000002", "nombre_completo": "JUAN PERSONA"},
        ])
        self.db.recognized.insert_one({
            "cod_rh": "0000000001", "nombre_completo": "Ana María Ejemplo",
            "categorias_historicas": [
                {"año": 2021, "categoria": "Investigador Junior"}
            ],
        })
        self.db.open_data.insert_one({
            "id_persona_pr": "0000000001",
            "ano_convo": "2024-05-23T00:00:00.000",
            "nme_clasificacion_pr": "Investigador Asociado",
            "id_clas_pr": "I", "orden_clas_pr": "2.0",
            "nme_genero_pr": "Femenino", "nme_niv_form_pr": "Doctorado",
            "nme_pais_nac_pr": "Colombia", "nme_departamento_nac_pr": "Antioquia",
            "nme_municipio_nac_pr": "Medellín", "id_area_con_pr": "1F",
            "nme_gran_area_pr": "Ciencias Naturales",
            "nme_area_pr": "Ciencias Biológicas",
        })
        self.db.cvlac_normalized.insert_many([
            {
                "_id": "0000000001", "id_persona_pr": "0000000001",
                "profile_name": "Ana María Ejemplo", "profile_status": "public",
                "profile": {
                    "general": {"name": "Ana María Ejemplo", "sex": "Femenino"},
                    "identifiers": [{
                        "label": "ORCID",
                        "url": "https://orcid.org/0000-0002-1825-0097",
                    }],
                    "degrees": [{"level": "Doctorado"}],
                    "experiences": [{"institution": "Universidad Ejemplo"}],
                },
                "parser": {"version": "3.2.0"},
                "source": {"content_sha256": "one"},
            },
            {
                "_id": "0000000002", "id_persona_pr": "0000000002",
                "profile_name": "", "profile_status": "private", "profile": {},
                "parser": {"version": "3.2.0"},
                "source": {"content_sha256": "two"},
            },
        ])
        self.db.gruplac_normalized.insert_one({
            "_id": "COL0000001", "group_code": "COL0000001",
            "group_name": "Grupo Público",
            "members": [{
                "cod_rh": "0000000001", "full_name": "Ana María Ejemplo",
                "role": "Integrante", "period": "2020/1 - Actual",
            }],
            "parser": {"version": "3.2.0"},
            "source": {"content_sha256": "group"},
        })
        self.db.scienti_cvlac_normalization_audits.insert_one({
            "_id": "cvlac_audit", "status": "passed",
            "config": {"destination_collection": "cvlac_normalized",
                       "parser_version": "3.2.0"},
            "summary": {"critical_anomalies": 0,
                        "parser_version_counts": {"3.2.0": 2}},
        })
        self.db.scienti_gruplac_normalization_audits.insert_one({
            "_id": "gruplac_audit", "status": "passed",
            "config": {"normalized_collection": "gruplac_normalized",
                       "expected_parser_version": "3.2.0"},
            "summary": {"critical_anomalies": 0,
                        "parser_version_counts": {"3.2.0": 1}},
        })
        self.db.affiliations_final.insert_one({
            "_id": "COL0000001", "names": [{"name": "Grupo Público"}]
        })
        self.db.scienti_affiliation_materialization_audits.insert_one({
            "_id": "affiliations_audit", "status": "passed",
            "critical_anomalies": 0, "collection": "affiliations_final",
            "documents": 1,
        })
        self.db.scienti_affiliation_publications.insert_one({
            "_id": "affiliations_run", "status": "published",
            "collection": "affiliations_final", "audit": "affiliations_audit",
            "documents": 1,
        })
        collections = {
            "works": "works_final", "projects": "projects_final",
            "patents": "patents_final", "events": "events_final",
        }
        self.db.works_final.insert_many([
            {
                "_id": "work-1", "doi": "10.1000/TEST",
                "author_count": 1,
                "authors": [{"id": "0000000001", "full_name": "Ana Ejemplo"}],
            },
            {
                "_id": "work-2", "doi": "",
                "authors": [{"id": "0000000002", "full_name": "Juan Persona"}],
            },
        ])
        self.db.projects_final.insert_one({
            "_id": "project-1", "authors": [{"id": "0000000002"}]
        })
        self.db.patents_final.insert_one({"_id": "patent-1", "authors": []})
        self.db.events_final.insert_one({"_id": "event-1", "authors": []})
        self.db.scienti_final_release_publications.insert_one({
            "_id": "release", "status": "published", "audit": "release_audit",
            "collections": collections,
        })
        self.db.scienti_final_release_audits.insert_one({
            "_id": "release_audit", "status": "passed",
            "release_name": "release", "critical_anomalies": 0,
            "collections": collections,
            "evidence": {
                entity: {
                    "collection": collection,
                    "documents": self.db[collection].count_documents({}),
                }
                for entity, collection in collections.items()
            },
        })

    def materializer(
        self, run_name="persons_v1", target_collection="persons_final",
        expected_people=2,
    ):
        return ScientiPersonMaterializer(
            self.db, run_name=run_name, manifest_run_name="manifest_run",
            manifest_collection="manifest", directory_collection="directory",
            recognized_collection="recognized", open_data_collection="open_data",
            cvlac_collection="cvlac_normalized", cvlac_audit_name="cvlac_audit",
            gruplac_collection="gruplac_normalized",
            gruplac_audit_name="gruplac_audit",
            affiliation_run_name="affiliations_run",
            affiliation_collection="affiliations_final",
            final_release_name="release", final_release_audit_name="release_audit",
            target_collection=target_collection, batch_size=2,
            progress_every=100, expected_people=expected_people,
        )

    def test_materializes_full_manifest_with_doi_only_identity_anchors(self):
        first = self.materializer().run()
        second = self.materializer().run()

        self.assertEqual(first, second)
        self.assertEqual(first["documents"], 2)
        ana = self.db.persons_final.find_one({"_id": "0000000001"})
        juan = self.db.persons_final.find_one({"_id": "0000000002"})
        self.assertEqual(set(ana), {"_id"} | PERSON_FIELDS)
        self.assertEqual(ana["sex"], "Mujer")
        self.assertEqual(ana["affiliations"][0]["id"], "COL0000001")
        self.assertTrue(any(item["source"] == "orcid" for item in ana["external_ids"]))
        self.assertEqual(ana["related_works"], [{
            "provenance": "minciencias", "source": "doi",
            "id": "https://doi.org/10.1000/test", "author_count": 1,
        }])
        self.assertEqual(juan["related_works"], [])
        self.assertEqual(
            self.db[PERSON_AUDITS].find_one({"_id": "persons_v1_audit"})["status"],
            "passed",
        )
        self.assertEqual(
            self.db[PERSON_PUBLICATIONS].find_one({"_id": "current"})["collection"],
            "persons_final",
        )
        self.assertNotIn(
            "__yuku_persons_v1_doi_edges", self.db.list_collection_names()
        )
        self.assertNotIn(
            "__yuku_persons_v1_persons", self.db.list_collection_names()
        )
        cleanup = self.db.scienti_person_materialization_runs.find_one(
            {"_id": "persons_v1"}
        )["transient_cleanup"]
        self.assertEqual(cleanup["status"], "complete")

    def test_rejects_unresolved_nonempty_final_author_reference(self):
        self.db.projects_final.update_one(
            {"_id": "project-1"}, {"$set": {"authors": [{"id": "9999999999"}]}}
        )
        with self.assertRaisesRegex(RuntimeError, "audit failed"):
            self.materializer().run()
        audit = self.db[PERSON_AUDITS].find_one({"_id": "persons_v1_audit"})
        self.assertEqual(audit["critical_counts"]["projects_unresolved_references"], 1)

    def test_includes_valid_gruplac_members_outside_cvlac_manifest(self):
        self.db.gruplac_normalized.update_one(
            {"_id": "COL0000001"},
            {"$push": {"members": {
                "cod_rh": "0000000003", "full_name": "Persona GrupLAC",
                "role": "Integrante", "period": "2021 - Actual",
            }}},
        )

        summary = self.materializer(expected_people=3).run()
        person = self.db.persons_final.find_one({"_id": "0000000003"})

        self.assertEqual(summary["documents"], 3)
        self.assertEqual(person["full_name"], "Persona GrupLAC")
        self.assertEqual(person["affiliations"][0]["id"], "COL0000001")
        audit = self.db[PERSON_AUDITS].find_one({"_id": "persons_v1_audit"})
        self.assertEqual(audit["quality_counts"]["gruplac_added_people"], 1)
        self.assertEqual(audit["critical_counts"]["gruplac_unresolved_codes"], 0)

    def test_excludes_invalid_work_doi_without_failing_people(self):
        self.db.works_final.update_one(
            {"_id": "work-2"}, {"$set": {"doi": "https://doi.org/10.15446/"}}
        )

        self.materializer().run()

        audit = self.db[PERSON_AUDITS].find_one({"_id": "persons_v1_audit"})
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["quality_counts"]["excluded_invalid_work_dois"], 1)
        self.assertEqual(
            self.db.persons_final.find_one({"_id": "0000000002"})["related_works"],
            [],
        )

    def test_accepts_only_real_canonical_dois_and_parses_public_period(self):
        self.assertEqual(
            canonical_doi("doi:10.1000/TEST"), "https://doi.org/10.1000/test"
        )
        self.assertEqual(canonical_doi("identifier-without-doi-prefix"), "")
        start, end = parse_profile_period("Enero de 2018 de Actual")
        self.assertGreater(start, 0)
        self.assertEqual(end, -1)


if __name__ == "__main__":
    unittest.main()
