from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

import mongomock
from bs4 import BeautifulSoup

from yuku.cvlac_related_works import normalize_related_works_document
from yuku.gruplac_related_works import parse_product_row
from yuku.scienti_entities import (
    ENTITY_NORMALIZER_VERSION,
    ENTITY_ROUTER_VERSION,
    ScientiEntityNormalizationRun,
    exact_key,
    iter_cvlac_entities,
    iter_gruplac_entities,
    merge_entity_documents,
    normalize_occurrence,
    route_cvlac,
    route_gruplac,
)


DESTINATIONS = {
    "works": "scienti_works_test",
    "projects": "scienti_projects_test",
    "patents": "scienti_patents_test",
    "events": "scienti_events_test",
}


def group_row(text):
    return BeautifulSoup(
        f"<tr><td class='celdas_1'></td><td class='celdas1'>{text}</td></tr>",
        "lxml",
    ).find("tr")


class ExactRouterTest(unittest.TestCase):
    def test_accepts_only_explicit_project_types(self):
        accepted = route_cvlac(
            "projects", {"project_type": "Investigación y desarrollo"}
        )
        rejected = route_cvlac(
            "projects", {"project_type": "Investigación y desarrollo regional"}
        )
        self.assertEqual(accepted["entity"], "projects")
        self.assertIsNone(rejected)

    def test_exact_key_keeps_accents_significant(self):
        self.assertEqual(exact_key("  TÉSIS\u00a0FINAL "), "tésis final")
        self.assertNotEqual(exact_key("TÉSIS"), exact_key("TESIS"))

    def test_generic_registration_is_source_scoped(self):
        record = {
            "product_type": "Patente de invención",
            "title": "Dispositivo experimental verificable",
            "registration_number": "Pendiente",
            "country": "Colombia",
        }
        route = route_cvlac("patents", record)
        first = normalize_occurrence(
            route, record, source_kind="cvlac", source_id="0000000001",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        second = normalize_occurrence(
            route, record, source_kind="cvlac", source_id="0000000002",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        self.assertNotEqual(first["_id"], second["_id"])
        self.assertNotIn("|registration|", first["source_metadata"]["identity_key"])

    def test_patent_and_trademark_registration_namespaces_do_not_merge(self):
        cv_record = {
            "product_type": "Patente de invención", "title": "Patente uno",
            "registration_number": "69306", "country": "Colombia",
        }
        group_record = {
            "source_section": "Signos distintivos", "product_type": "Marcas",
            "title": "Marca uno", "registration_number": "69306",
            "country": "Colombia",
        }
        cv_document = normalize_occurrence(
            route_cvlac("patents", cv_record), cv_record,
            source_kind="cvlac", source_id="0000000001",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        group_document = normalize_occurrence(
            route_gruplac(group_record), group_record,
            source_kind="gruplac", source_id="COL1",
            source_collection="gr", source_url="", record_index=0,
            group={"group_code": "COL1", "group_name": "Grupo"},
            normalized_at=1,
        )
        self.assertNotEqual(cv_document["_id"], group_document["_id"])
        self.assertIn("|patent|", cv_document["source_metadata"]["identity_key"])
        self.assertIn("|trademark|", group_document["source_metadata"]["identity_key"])

    def test_same_patent_registration_and_country_still_merge(self):
        first_record = {
            "product_type": "Patente de invención",
            "title": "Equipo electromecánico homogenizador de panela",
            "registration_number": "NC2018/0010003", "country": "Colombia",
        }
        second_record = {
            "product_type": "Modelo de utilidad",
            "title": "Equipo electromecánico homogenizador de panela",
            "registration_number": "NC2018/0010003", "country": "Colombia",
        }
        first = normalize_occurrence(
            route_cvlac("patents", first_record), first_record,
            source_kind="cvlac", source_id="0000000001",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        second = normalize_occurrence(
            route_cvlac("patents", second_record), second_record,
            source_kind="cvlac", source_id="0000000002",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        self.assertEqual(first["_id"], second["_id"])
        self.assertEqual(
            first["source_metadata"]["identity_rule"],
            "exact_namespace_registration_country_title",
        )

    def test_same_registration_with_different_exact_titles_does_not_merge(self):
        first_record = {
            "product_type": "Patente de invención",
            "title": "Dispositivo de alimentación para vehículos remotos",
            "registration_number": "NC2022/0007689",
            "country": "Colombia",
        }
        second_record = {
            "product_type": "Patente de invención",
            "title": "Método automatizado para prevención del suicidio",
            "registration_number": "NC2022/0007689",
            "country": "Colombia",
        }
        first = normalize_occurrence(
            route_cvlac("patents", first_record), first_record,
            source_kind="cvlac", source_id="0000000001",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        second = normalize_occurrence(
            route_cvlac("patents", second_record), second_record,
            source_kind="cvlac", source_id="0000000002",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        self.assertNotEqual(first["_id"], second["_id"])

    def test_invalid_event_dates_become_null_and_scope_is_recovered_exactly(self):
        record = {
            "event_type": "Congreso",
            "title": "Congreso verificable de ciencias ambientales",
            "start_date": "2024-02-31",
            "end_date": "2024-03-02",
            "scope": "Internacional Tipo de evento: Congreso Ámbito: Internacional",
        }
        document = normalize_occurrence(
            route_cvlac("events", record), record,
            source_kind="cvlac", source_id="0000000001",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        occurrence = document["source_metadata"]["occurrences"][0]
        self.assertIsNone(occurrence["metadata"]["start_date"])
        self.assertEqual(occurrence["metadata"]["end_date"], "2024-03-02")
        self.assertEqual(occurrence["metadata"]["scope"], "Internacional")
        self.assertIsNone(document["date_held"])
        self.assertIsNone(document["year_held"])
        self.assertEqual(
            {value["kind"] for value in occurrence["normalization_findings"]},
            {"source_invalid_event_start_date", "source_event_scope_normalized"},
        )

    def test_event_end_before_start_keeps_start_and_nulls_end(self):
        record = {
            "event_type": "Congreso",
            "title": "Congreso verificable sobre biodiversidad regional",
            "start_date": "2024-05-10",
            "end_date": "2024-05-09",
            "scope": "Nacional",
        }
        document = normalize_occurrence(
            route_cvlac("events", record), record,
            source_kind="cvlac", source_id="0000000001",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        occurrence = document["source_metadata"]["occurrences"][0]
        self.assertEqual(occurrence["metadata"]["start_date"], "2024-05-10")
        self.assertIsNone(occurrence["metadata"]["end_date"])
        self.assertEqual(
            occurrence["normalization_findings"],
            [{"kind": "source_event_end_before_start", "field": "end_date"}],
        )


class SurgicalGruplacParserTest(unittest.TestCase):
    def test_extracts_thesis_student_advisor_and_program(self):
        record = parse_product_row(
            group_row(
                "1.- <strong>Trabajos de grado de pregrado</strong> : "
                "Una tesis verificable Desde 7 2024 hasta Julio, "
                "Tipo de orientación: Tutor principal "
                "Nombre del estudiante: KARENT SOFIA GUZMAN ARCE "
                "Programa académico: BIOLOGIA Número de páginas: 80, "
                "Valoración: Aprobada, Institución: UNIVERSIDAD DEL VALLE "
                "Tutor(es)/Cotutor(es): YHERSON FRANCHESCO MOLINA HENAO"
            ),
            "Trabajos dirigidos/turorías",
        )
        self.assertEqual(record["students"], ["KARENT SOFIA GUZMAN ARCE"])
        self.assertEqual(record["advisors"], ["YHERSON FRANCHESCO MOLINA HENAO"])
        self.assertEqual(record["academic_program"], "BIOLOGIA")
        self.assertEqual(record["pages"], "80")
        self.assertEqual(record["institution"], "UNIVERSIDAD DEL VALLE")
        self.assertEqual(record["orientation_type"], "Tutor principal")

    def test_extracts_project_type_and_period_without_title_pollution(self):
        record = parse_product_row(
            group_row(
                "1.- <strong>Proyectos</strong> : Investigación y desarrollo : "
                "Proyecto exacto de biodiversidad 2023/2 - Actual"
            ),
            "Proyectos",
        )
        self.assertEqual(record["project_type"], "Investigación y desarrollo")
        self.assertEqual(record["title"], "Proyecto exacto de biodiversidad")
        self.assertEqual(record["start_date"], "2023/2")
        self.assertEqual(record["end_date"], "Actual")

    def test_extracts_event_and_intellectual_property_metadata(self):
        event = parse_product_row(
            group_row(
                "1.- <strong>Congreso</strong> : Evento exacto, BOGOTÁ, "
                "desde 2022-07-06 - hasta 2022-07-08 Ámbito: Nacional, "
                "Tipos de participación: Ponente Instituciones asociadas "
                "Nombre de la institución: UNIVERSIDAD EL BOSQUE "
                "Tipo de vinculación Patrocinadora"
            ),
            "Eventos Científicos",
        )
        trademark = parse_product_row(
            group_row(
                "1.- <strong>Marcas</strong> : QUINOASURE Colombia, 2023, "
                "Número del registro: 732007, Nombre del titular: Jesus Eduardo Bravo"
            ),
            "Signos distintivos",
        )
        self.assertEqual(event["start_date"], "2022-07-06")
        self.assertEqual(event["scope"], "Nacional")
        self.assertEqual(event["participation_types"], ["Ponente"])
        self.assertEqual(
            event["associated_institutions"],
            [{"name": "UNIVERSIDAD EL BOSQUE", "linkage": "Patrocinadora"}],
        )
        self.assertEqual(trademark["registration_number"], "732007")
        self.assertEqual(trademark["holder"], "Jesus Eduardo Bravo")


class SurgicalCvlacParserTest(unittest.TestCase):
    def test_preserves_profile_name_thesis_evidence_and_raw_text(self):
        html = """
        <a name="datos_generales"></a>
        <table><tr><td>Nombre</td><td>MARIA TUTORA</td></tr></table>
        <table><tr><td><h3>Trabajos dirigidos/Tutorías</h3></td></tr>
          <tr><td><b>Trabajo de grado de maestría o especialidad clínica</b></td></tr>
          <tr><td><blockquote>
            MARIA TUTORA, Una tesis clínica verificable UNIVERSIDAD DEL VALLE
            <i>Estado:</i> Tesis concluida, 2024.
            <i>Dirigió como:</i> Tutor principal
            <i>Persona(s) orientada(s):</i> LUIS ESTUDIANTE
          </blockquote></td></tr>
        </table>
        """
        document = normalize_related_works_document("0000000001", html)
        thesis = document["production"][0]
        self.assertEqual(document["profile_name"], "MARIA TUTORA")
        self.assertEqual(thesis["profile_name"], "MARIA TUTORA")
        self.assertEqual(thesis["status"], "Tesis concluida, 2024")
        self.assertIn("Una tesis clínica verificable", thesis["raw_text"])

    def test_preserves_patent_subtype_registration_and_application_fields(self):
        html = """
        <a name="datos_generales"></a>
        <table><tr><td>Nombre</td><td>ANA INVENTORA</td></tr></table>
        <table><tr><td><h3>Patentes</h3></td></tr>
          <tr><td><b>Patente de invención</b></td></tr>
          <tr><td><blockquote>
            NC2019/0003567 - Aparato móvil para dosificación,
            <i>Institución:</i> UNIVERSIDAD EJEMPLO,
            <i>Vía de solicitud:</i> Via Tradicional En: Colombia, 2019-04-10.
            <i>Nombre del solicitante de la patente:</i> Universidad Ejemplo,
            <i>Gaceta Industrial de Publicación:</i> 907,
          </blockquote></td></tr>
        </table>
        """
        patent = normalize_related_works_document("0000000002", html)["patents"][0]
        self.assertEqual(patent["product_type"], "Patente de invención")
        self.assertEqual(patent["registration_number"], "NC2019/0003567")
        self.assertEqual(patent["presentation_date"], "2019-04-10")
        self.assertEqual(patent["applicant"], "Universidad Ejemplo")
        self.assertEqual(patent["request_route"], "Via Tradicional En: Colombia, 2019-04-10")
        self.assertEqual(patent["industrial_publication_gazette"], "907")


class EntityMaterializationTest(unittest.TestCase):
    def test_project_month_names_keep_the_real_month_in_kahi_dates(self):
        record = {
            "project_type": "Investigación y desarrollo",
            "title": "Proyecto mensual verificable",
            "start_date": "Septiembre 2009",
            "end_date": "February 2010",
        }
        document = normalize_occurrence(
            route_cvlac("projects", record), record,
            source_kind="cvlac", source_id="0000000001",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        bogota = ZoneInfo("America/Bogota")
        self.assertEqual(
            document["date_init"],
            int(datetime(2009, 9, 1, tzinfo=bogota).timestamp()),
        )
        self.assertEqual(
            document["date_end"],
            int(datetime(2010, 2, 1, tzinfo=bogota).timestamp()),
        )
        self.assertEqual(document["year_init"], 2009)
        self.assertEqual(document["year_end"], 2010)
        self.assertEqual(
            document["source_metadata"]["normalizer_version"],
            ENTITY_NORMALIZER_VERSION,
        )

    def test_incomplete_or_composite_month_is_not_invented(self):
        record = {
            "project_type": "Investigación y desarrollo",
            "title": "Proyecto con fecha incompleta",
            "start_date": "Abril 2017 Inicio: Abril 2017",
            "end_date": "Julio",
        }
        document = normalize_occurrence(
            route_cvlac("projects", record), record,
            source_kind="cvlac", source_id="0000000001",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        self.assertIsNone(document["date_init"])
        self.assertIsNone(document["date_end"])

    def test_exact_route_without_meaningful_title_is_traced_and_excluded(self):
        profile = {
            "_id": "0000000008",
            "production": [],
            "projects": [
                {
                    "project_type": "Investigación y desarrollo",
                    "title": "...",
                    "raw_text": "Tipo de proyecto: Investigación y desarrollo ...",
                }
            ],
            "patents": [],
            "events": [],
        }
        outcomes = list(iter_cvlac_entities(profile, "source", 1))
        self.assertEqual(outcomes[0][0], "excluded_incomplete")
        self.assertIsNone(outcomes[0][1])
        self.assertEqual(outcomes[0][2]["reason"], "missing_meaningful_title")
        self.assertEqual(outcomes[0][2]["record_index"], 0)

    def test_known_non_thesis_directed_work_stays_in_works(self):
        profile = {
            "_id": "0000000007",
            "production": [
                {
                    "product_type": "Trabajos dirigidos/Tutorías - Iniciación Científica",
                    "title": "Semillero conocido",
                }
            ],
            "projects": [], "patents": [], "events": [],
        }
        outcomes = [value[0] for value in iter_cvlac_entities(profile, "source", 1)]
        self.assertEqual(outcomes, ["works"])

    def test_thesis_uses_student_as_author_and_tutor_as_advisor(self):
        profile = {
            "_id": "0000000007",
            "profile_name": "Maria Esperanza Cuenca Coral",
            "url_persona": "https://example.test/cv/7",
            "production": [
                {
                    "profile_id": "0000000007",
                    "profile_name": "Maria Esperanza Cuenca Coral",
                    "source_section": "Trabajos dirigidos/tutorias",
                    "product_type": "Trabajos dirigidos/Tutorías - Trabajo de grado de maestría o especialidad clínica",
                    "title": "Una tesis económica verificable",
                    "year": 2024,
                    "oriented_people": ["Alexander Villalobos Campo"],
                    "advisor_role": "advisor",
                    "affiliation": "Universidad del Norte",
                }
            ],
            "projects": [], "patents": [], "events": [],
        }
        values = [value for value in iter_cvlac_entities(profile, "cvlac_source", 1) if value[0] == "works"]
        work = values[0][1]
        by_name = {author["full_name"]: author for author in work["authors"]}
        self.assertNotIn("type", by_name["Alexander Villalobos Campo"])
        self.assertEqual(by_name["Maria Esperanza Cuenca Coral"]["type"], "advisor")
        self.assertEqual(by_name["Maria Esperanza Cuenca Coral"]["id"], "0000000007")

    def test_group_names_resolve_only_by_exact_member_name(self):
        group = {
            "_id": "COL0000013",
            "group_code": "COL0000013",
            "group_name": "Grupo exacto",
            "members": [
                {"cod_rh": "0002171065", "full_name": "Karent Sofia Guzman Arce"},
                {"cod_rh": "0001040995", "full_name": "Yherson Franchesco Molina Henao"},
            ],
            "production": [
                {
                    "product_type": "Trabajos de grado de pregrado",
                    "source_section": "Trabajos dirigidos/turorías",
                    "title": "Una tesis biológica verificable",
                    "year": 2024,
                    "students": ["Karent Sofia Guzman Arce"],
                    "advisors": ["Yherson Franchesco Molina Henao"],
                    "raw_text": "",
                }
            ],
        }
        values = [value for value in iter_gruplac_entities(group, "gruplac_source", 1) if value[0] == "works"]
        work = values[0][1]
        by_name = {author["full_name"]: author for author in work["authors"]}
        self.assertEqual(by_name["Karent Sofia Guzman Arce"]["id"], "0002171065")
        self.assertEqual(by_name["Yherson Franchesco Molina Henao"]["id"], "0001040995")
        self.assertEqual(by_name["Yherson Franchesco Molina Henao"]["type"], "advisor")

    def test_exact_student_name_takes_precedence_over_advisor_role(self):
        group = {
            "_id": "COL1", "group_code": "COL1", "group_name": "Grupo exacto",
            "members": [{"cod_rh": "0000000099", "full_name": "Ana Estudiante"}],
            "production": [{
                "source_section": "Trabajos dirigidos/turorías",
                "product_type": "Trabajos de grado de pregrado",
                "title": "Tesis exacta sobre sistemas ambientales",
                "year": 2024,
                "students": ["Ana Estudiante"],
                "advisors": ["ANA ESTUDIANTE"],
                "raw_text": "",
            }],
        }
        document = next(
            value[1] for value in iter_gruplac_entities(group, "gr", 1)
            if value[0] == "works"
        )
        self.assertEqual(len(document["authors"]), 1)
        self.assertNotEqual(document["authors"][0].get("type"), "advisor")
        self.assertEqual(
            document["source_metadata"]["role_resolutions"][0]["rule"],
            "student_precedence_exact_name",
        )

    def test_thesis_merge_removes_cross_source_exact_role_overlap(self):
        student_record = {
            "product_type": "Trabajos dirigidos/Tutorías - Trabajos de grado de pregrado",
            "title": "Tesis exacta sobre calidad del agua",
            "year": 2024,
            "oriented_people": ["Ana Estudiante"],
            "profile_name": "Tutora Verificable",
        }
        advisor_record = {
            "product_type": "Trabajos dirigidos/Tutorías - Trabajos de grado de pregrado",
            "title": "Tesis exacta sobre calidad del agua",
            "year": 2024,
            "oriented_people": ["Tutora Verificable"],
            "profile_name": "Ana Estudiante",
        }
        first = normalize_occurrence(
            route_cvlac("production", student_record), student_record,
            source_kind="cvlac", source_id="0000000001",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        second = normalize_occurrence(
            route_cvlac("production", advisor_record), advisor_record,
            source_kind="cvlac", source_id="0000000002",
            source_collection="cv", source_url="", record_index=0,
            normalized_at=1,
        )
        # These identities differ because their verified students differ, so
        # force the same identity to exercise the defensive merge invariant.
        second["_id"] = first["_id"]
        merged = merge_entity_documents(first, second)
        roles = {
            (author["full_name"], author.get("type", ""))
            for author in merged["authors"]
        }
        self.assertIn(("Ana Estudiante", ""), roles)
        self.assertNotIn(("Ana Estudiante", "advisor"), roles)

    def test_exact_cvlac_and_gruplac_thesis_occurrences_merge_safely(self):
        title = "Tesis exacta sobre biodiversidad del Pacífico"
        cvlac = {
            "_id": "0000000001", "profile_name": "Tutora Uno",
            "production": [{
                "profile_id": "0000000001", "profile_name": "Tutora Uno",
                "product_type": "Trabajos dirigidos/Tutorías - Trabajos de grado de pregrado",
                "title": title, "year": 2024, "oriented_people": ["Ana Estudiante"],
            }],
            "projects": [], "patents": [], "events": [],
        }
        gruplac = {
            "_id": "COL1", "group_code": "COL1", "group_name": "Grupo uno",
            "members": [{"cod_rh": "0000000099", "full_name": "Ana Estudiante"}],
            "production": [{
                "source_section": "Trabajos dirigidos/turorías",
                "product_type": "Trabajos de grado de pregrado",
                "title": title, "year": 2024, "students": ["Ana Estudiante"],
                "advisors": [], "raw_text": "",
            }],
        }
        cv_doc = next(value[1] for value in iter_cvlac_entities(cvlac, "cv", 1) if value[0] == "works")
        gr_doc = next(value[1] for value in iter_gruplac_entities(gruplac, "gr", 1) if value[0] == "works")
        self.assertEqual(cv_doc["_id"], gr_doc["_id"])
        merged = merge_entity_documents(cv_doc, gr_doc)
        self.assertEqual(len(merged["source_metadata"]["occurrences"]), 2)
        student = next(value for value in merged["authors"] if value.get("type") != "advisor")
        self.assertEqual(student["id"], "0000000099")


class EntityNormalizationRunTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.db.cvlac_source.insert_one(
            {
                "_id": "0000000001",
                "profile_name": "Ana Tutora",
                "url_persona": "https://example.test/cv/1",
                "production": [
                    {
                        "profile_id": "0000000001",
                        "profile_name": "Ana Tutora",
                        "source_section": "Trabajos dirigidos/tutorias",
                        "product_type": "Trabajos dirigidos/Tutorías - Trabajos de grado de pregrado",
                        "title": "Tesis exacta de ingeniería ambiental",
                        "year": 2024,
                        "oriented_people": ["Luis Estudiante"],
                    }
                ],
                "projects": [
                    {
                        "profile_id": "0000000001", "profile_name": "Ana Tutora",
                        "project_type": "Investigación y desarrollo",
                        "title": "Proyecto exacto de ingeniería ambiental",
                        "start_date": "2023-01-01", "end_date": "2024-01-01", "year": 2023,
                    }
                ],
                "patents": [
                    {
                        "profile_id": "0000000001", "profile_name": "Ana Tutora",
                        "product_type": "Patente", "title": "Dispositivo exacto para medir agua",
                        "year": 2022, "country": "Colombia", "registration_number": "NC-123",
                    }
                ],
                "events": [
                    {
                        "profile_id": "0000000001", "profile_name": "Ana Tutora",
                        "event_type": "Congreso", "title": "Congreso exacto de ingeniería ambiental",
                        "start_date": "2024-05-01", "year": 2024,
                    }
                ],
            }
        )
        self.db.gruplac_source.insert_one(
            {"_id": "COL1", "group_code": "COL1", "group_name": "Grupo uno", "members": [], "production": []}
        )

    def test_run_writes_four_kahi_shapes_and_is_idempotent(self):
        runner = ScientiEntityNormalizationRun(
            self.db,
            run_name="entities_test",
            cvlac_collection="cvlac_source",
            gruplac_collection="gruplac_source",
            destinations=DESTINATIONS,
            batch_size=1,
        )
        first = runner.run()
        self.assertEqual(first["status"], "complete")
        for entity, collection in DESTINATIONS.items():
            self.assertEqual(self.db[collection].count_documents({}), 1, entity)
            document = self.db[collection].find_one({})
            self.assertEqual(document["source_metadata"]["router_version"], ENTITY_ROUTER_VERSION)
            self.assertEqual(document["source_metadata"]["normalizer_version"], ENTITY_NORMALIZER_VERSION)
            self.assertTrue(document["titles"][0]["title"])
        second = ScientiEntityNormalizationRun(
            self.db,
            run_name="entities_test",
            cvlac_collection="cvlac_source",
            gruplac_collection="gruplac_source",
            destinations=DESTINATIONS,
            batch_size=1,
        ).run()
        self.assertEqual(second["audit"]["critical_anomalies"], 0)

    def test_incomplete_routed_row_is_noncritical_and_auditable(self):
        self.db.cvlac_source.update_one(
            {"_id": "0000000001"},
            {
                "$push": {
                    "events": {
                        "event_type": "Congreso",
                        "title": ".",
                        "raw_text": "Nombre del evento: Tipo de evento: Congreso",
                    }
                }
            },
        )
        runner = ScientiEntityNormalizationRun(
            self.db,
            run_name="entities_incomplete_test",
            cvlac_collection="cvlac_source",
            gruplac_collection="gruplac_source",
            destinations={key: f"{value}_incomplete" for key, value in DESTINATIONS.items()},
            batch_size=1,
        )
        result = runner.run()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            result["audit"]["excluded_incomplete_occurrences"],
            {"cvlac": 1, "gruplac": 0},
        )
        self.assertEqual(result["audit"]["critical_anomalies"], 0)
        stored_run = self.db.scienti_entity_normalization_runs.find_one(
            {"_id": "entities_incomplete_test"}
        )
        self.assertEqual(
            stored_run["routing_examples"][0]["reason"],
            "missing_meaningful_title",
        )


if __name__ == "__main__":
    unittest.main()
