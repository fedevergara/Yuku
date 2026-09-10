import unittest

import mongomock

from yuku.scienti_normalization import (
    CVLAC_PARSER_VERSION,
    GRUPLAC_PARSER_VERSION,
    CvlacNormalizationAuditor,
    CvlacNormalizationRun,
    GruplacNormalizationAuditor,
    GruplacNormalizationRun,
    _raw_hash_index,
    _sanitize_unicode_for_bson,
)


class CvlacNormalizationRunTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.db.scienti_cvlac_priority_runs.insert_one(
            {
                "_id": "source_snapshot",
                "status": "complete",
                "target_count": 2,
                "config": {
                    "raw_collection": "raw_profiles",
                    "state_collection": "profile_states",
                },
            }
        )
        self.db.raw_profiles.insert_many(
            [
                {
                    "_id": "0000000001",
                    "html": "<html><body>public profile</body></html>",
                    "url": "https://example.test/1",
                    "content_sha256": "1" * 64,
                    "encoding": "iso-8859-1",
                    "decode_replacement_chars": 0,
                    "http_status": 200,
                },
                {
                    "_id": "0000000002",
                    "html": "<html><body>private profile</body></html>",
                    "url": "https://example.test/2",
                    "content_sha256": "2" * 64,
                    "encoding": "iso-8859-1",
                    "decode_replacement_chars": 0,
                    "http_status": 200,
                },
            ]
        )
        self.db.profile_states.insert_many(
            [
                {
                    "_id": "cvlac:0000000001",
                    "kind": "cvlac",
                    "source_id": "0000000001",
                    "status": "downloaded",
                },
                {
                    "_id": "cvlac:0000000002",
                    "kind": "cvlac",
                    "source_id": "0000000002",
                    "status": "private",
                },
            ]
        )

    def normalization(self, **kwargs):
        options = {
            "run_name": "normalize_test",
            "source_snapshot_run_name": "source_snapshot",
            "destination_collection": "normalized_profiles",
            "batch_size": 2,
            "progress_every": 1,
        }
        options.update(kwargs)
        return CvlacNormalizationRun(self.db, **options)

    def test_versioned_provenance_status_and_idempotent_resume(self):
        first = self.normalization().run()
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["processed_this_attempt"], 2)
        public = self.db.normalized_profiles.find_one({"_id": "0000000001"})
        private = self.db.normalized_profiles.find_one({"_id": "0000000002"})
        self.assertEqual(public["profile_status"], "public")
        self.assertEqual(private["profile_status"], "private")
        self.assertEqual(public["parser"]["version"], CVLAC_PARSER_VERSION)
        self.assertEqual(public["source"]["snapshot_run_name"], "source_snapshot")
        self.assertEqual(public["source"]["content_sha256"], "1" * 64)
        self.assertEqual(private["production"], [])

        second = self.normalization().run()
        self.assertEqual(second["processed_this_attempt"], 0)
        self.assertEqual(second["skipped_this_attempt"], 2)
        self.assertEqual(self.db.normalized_profiles.count_documents({}), 2)

    def test_repairs_paired_and_isolated_unicode_surrogates(self):
        repaired, count = _sanitize_unicode_for_bson(
            {"title": "emoji \ud83d\ude00", "authors": ["invalid \ud800 name"]}
        )
        self.assertEqual(count, 3)
        self.assertEqual(repaired["title"], "emoji 😀")
        self.assertEqual(repaired["authors"], ["invalid � name"])
        repaired["title"].encode("utf-8")
        repaired["authors"][0].encode("utf-8")

    def test_hash_index_falls_back_only_for_legacy_rows(self):
        self.db.hash_source.insert_many(
            [
                {"_id": "valid", "content_sha256": "a" * 64, "html": "ignored"},
                {"_id": "legacy", "html": "legacy html"},
            ]
        )
        hashes = _raw_hash_index(self.db.hash_source)
        self.assertEqual(hashes["valid"], "a" * 64)
        self.assertEqual(len(hashes["legacy"]), 64)
        self.assertNotEqual(hashes["legacy"], hashes["valid"])

    def test_changed_source_hash_is_reprocessed(self):
        self.normalization().run()
        self.db.raw_profiles.update_one(
            {"_id": "0000000001"},
            {"$set": {"html": "changed", "content_sha256": "3" * 64}},
        )
        result = self.normalization().run()
        self.assertEqual(result["processed_this_attempt"], 1)
        self.assertEqual(result["skipped_this_attempt"], 1)
        self.assertEqual(
            self.db.normalized_profiles.find_one({"_id": "0000000001"})["source"][
                "content_sha256"
            ],
            "3" * 64,
        )

    def test_parallel_parser_matches_contract_and_resumes(self):
        parallel = self.normalization(
            run_name="normalize_parallel",
            destination_collection="normalized_parallel",
            workers=2,
        )
        first = parallel.run()
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["workers"], 2)
        self.assertEqual(first["processed_this_attempt"], 2)
        documents = list(self.db.normalized_parallel.find({}).sort("_id", 1))
        self.assertEqual([item["_id"] for item in documents], ["0000000001", "0000000002"])
        self.assertEqual(documents[0]["profile_status"], "public")
        self.assertEqual(documents[1]["profile_status"], "private")
        self.assertEqual(documents[0]["parser"]["version"], CVLAC_PARSER_VERSION)

        second = parallel.run()
        self.assertEqual(second["processed_this_attempt"], 0)
        self.assertEqual(second["skipped_this_attempt"], 2)

    def test_rejects_same_run_name_with_different_destination(self):
        self.normalization().run()
        with self.assertRaises(ValueError):
            self.normalization(destination_collection="another_destination").run()

    def test_audit_passes_exact_normalization_and_detects_tampering(self):
        self.normalization().run()
        audit = CvlacNormalizationAuditor(
            self.db,
            audit_name="audit_test",
            normalization_run_name="normalize_test",
            progress_every=1,
        )
        first = audit.run()
        self.assertEqual(first["status"], "passed")
        self.db.normalized_profiles.update_one(
            {"_id": "0000000001"}, {"$set": {"source.content_sha256": "bad"}}
        )
        second = audit.run()
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["anomaly_counts"]["source_hash_mismatch"], 1)


class GruplacNormalizationAuditorTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.db.recognized_groups.insert_many(
            [
                {"_id": "a", "codigo_grupo": "COL1", "url_gruplac": "https://example/1"},
                {"_id": "b", "codigo_grupo": "COL2", "url_gruplac": "https://example/2"},
            ]
        )
        self.db.group_raw.insert_many(
            [
                {"_id": "COL1", "content_sha256": "1" * 64},
                {"_id": "COL2", "content_sha256": "2" * 64},
            ]
        )
        self.db.group_states.insert_many(
            [
                {
                    "_id": "gruplac:COL1",
                    "kind": "gruplac",
                    "source_id": "COL1",
                    "status": "downloaded",
                },
                {
                    "_id": "gruplac:COL2",
                    "kind": "gruplac",
                    "source_id": "COL2",
                    "status": "incomplete",
                },
            ]
        )
        self.db.group_normalized.insert_many(
            [
                {
                    "_id": "COL1",
                    "group_code": "COL1",
                    "group_name": "Group one",
                    "group_status": "downloaded",
                    "parser": {"version": GRUPLAC_PARSER_VERSION},
                    "source": {"content_sha256": "1" * 64},
                    "members_count": 1,
                    "members": [{"cod_rh": "0000000001"}],
                    "production_count": 1,
                    "production": [
                        {
                            "title": "Work one",
                            "year": 2024,
                            "authors": ["Person"],
                            "doi": ["https://doi.org/10.1000/example"],
                            "isbn": [],
                            "type_impactu": "Articulo de revista",
                        }
                    ],
                },
                {
                    "_id": "COL2",
                    "group_code": "COL2",
                    "group_name": "Group two",
                    "group_status": "incomplete",
                    "parser": {"version": GRUPLAC_PARSER_VERSION},
                    "source": {"content_sha256": "2" * 64},
                    "members_count": 0,
                    "members": [],
                    "production_count": 0,
                    "production": [],
                },
            ]
        )
        self.db.scienti_gruplac_verification_runs.insert_one(
            {
                "_id": "verify_groups",
                "status": "complete",
                "config": {
                    "source_raw_collection": "group_raw",
                    "source_state_collection": "group_states",
                    "normalized_collection": "group_normalized",
                    "target_collection": "verify_groups_targets",
                    "result_collection": "verify_groups_results",
                },
                "summary": {"outcomes": {"confirmed_incomplete": 1}},
            }
        )
        self.db.verify_groups_targets.insert_one({"_id": "COL2"})
        self.db.verify_groups_results.insert_one(
            {
                "_id": "COL2",
                "outcome": "confirmed_incomplete",
                "content_unchanged": True,
            }
        )

    def test_complete_group_collections_pass(self):
        result = GruplacNormalizationAuditor(
            self.db,
            audit_name="group_audit",
            recognized_groups_collection="recognized_groups",
            raw_collection="group_raw",
            state_collection="group_states",
            normalized_collection="group_normalized",
            verification_run_name="verify_groups",
        ).run()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["expected_groups"], 2)
        self.assertEqual(result["content_counts"]["production"], 1)

    def test_group_audit_rejects_title_period_field_misalignment(self):
        self.db.group_normalized.update_one(
            {"_id": "COL1"},
            {
                "$push": {
                    "production": {
                        "title": "desde Marzo 2016 hasta Septiembre 2016",
                        "product_type": "Semillero de investigación sobre física",
                        "source_section": "Estrategias Pedagógicas para el fomento a la CTI",
                        "authors": [],
                        "doi": [],
                        "isbn": [],
                    }
                },
                "$set": {"production_count": 2},
            },
        )
        result = GruplacNormalizationAuditor(
            self.db,
            audit_name="group_audit_bad_title",
            recognized_groups_collection="recognized_groups",
            raw_collection="group_raw",
            state_collection="group_states",
            normalized_collection="group_normalized",
            verification_run_name="verify_groups",
        ).run()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["anomaly_counts"]["production_title_period_misalignment"], 1
        )


class GruplacNormalizationRunTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        timeline_html = """
        <html><span class="celdaEncabezado">Grupo de prueba</span>
        <table><tr><td class="celdaEncabezado">Estrategias Pedagógicas para el fomento a la CTI</td></tr>
        <tr><td class="celdas_1"></td><td class="celdas1">
        1.- <strong>Título correcto</strong>: desde Enero 2020 hasta Diciembre 2021<br/>
        Descripción: Evidencia detallada.</td></tr></table></html>
        """
        self.db.group_raw.insert_many(
            [
                {
                    "_id": "COL1",
                    "html": timeline_html,
                    "url": "https://example.test/1",
                    "content_sha256": "1" * 64,
                },
                {
                    "_id": "COL2",
                    "html": "<html><title>GrupLAC</title></html>",
                    "url": "https://example.test/2",
                    "content_sha256": "2" * 64,
                },
            ]
        )
        self.db.group_states.insert_many(
            [
                {
                    "_id": "gruplac:COL1",
                    "kind": "gruplac",
                    "source_id": "COL1",
                    "status": "downloaded",
                },
                {
                    "_id": "gruplac:COL2",
                    "kind": "gruplac",
                    "source_id": "COL2",
                    "status": "incomplete",
                },
            ]
        )

    def normalization(self):
        return GruplacNormalizationRun(
            self.db,
            run_name="normalize_groups",
            source_raw_collection="group_raw",
            source_state_collection="group_states",
            destination_collection="groups_v2",
            workers=2,
            progress_every=1,
        )

    def test_parallel_versioned_normalization_and_resume(self):
        first = self.normalization().run()
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["destination_groups"], 2)
        group = self.db.groups_v2.find_one({"_id": "COL1"})
        self.assertEqual(group["parser"]["version"], GRUPLAC_PARSER_VERSION)
        self.assertEqual(group["group_status"], "downloaded")
        self.assertEqual(group["source"]["content_sha256"], "1" * 64)
        self.assertEqual(group["production"][0]["title"], "Título correcto")
        self.assertEqual(group["production"][0]["start_date"], "Enero 2020")
        self.assertEqual(group["production"][0]["end_date"], "Diciembre 2021")

        second = self.normalization().run()
        self.assertEqual(second["processed_this_attempt"], 0)
        self.assertEqual(second["skipped_this_attempt"], 2)
        self.assertEqual(self.db.groups_v2.count_documents({}), 2)


if __name__ == "__main__":
    unittest.main()
