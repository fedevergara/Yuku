import unittest
from unittest.mock import patch

import mongomock

from yuku.cvlac_work_graph import (
    CHECKPOINT_STAGES,
    CheckpointedNormalizedWorkGraphBuilder,
    CvlacWorkGraphBuilder,
    WORK_GRAPH_PUBLICATIONS,
    group_affiliation_evidence_fingerprint,
)


def product(title, *, doi=""):
    return {
        "title": title,
        "year": 2024,
        "type_impactu": "Articulo de revista",
        "product_type": "Artículo de investigación",
        "authors": ["Profile One"],
        "keywords": [],
        "areas": [],
        "advisor_role": "",
        "oriented_people": [],
        "doi": [doi] if doi else [],
        "issn": [],
        "isbn": [],
        "affiliation": "",
        "country": "Colombia",
    }


class CheckpointedNormalizedWorkGraphTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        title = "A sufficiently specific shared scientific article title"
        self.db.cvlac_normalized.insert_many(
            [
                {
                    "_id": "0000000001",
                    "profile_status": "public",
                    "production_counts": 1,
                    "production": [product(title)],
                },
                {
                    "_id": "0000000002",
                    "profile_status": "public",
                    "production_counts": 1,
                    "production": [product(title)],
                },
            ]
        )
        self.db.groups_normalized.insert_one(
            {
                "_id": "COL1",
                "group_code": "COL1",
                "group_name": "Group one",
                "members": [
                    {
                        "cod_rh": "0000000001",
                        "full_name": "Profile One",
                        "period": "2020/1 - Actual",
                    }
                ],
                "production_count": 1,
                "production": [product(title)],
            }
        )
        self.db.recognized_groups.insert_one(
            {
                "codigo_grupo": "COL1",
                "instituciones_historicas": [
                    {"año": 2021, "institucion": "UNIVERSITY ONE"},
                    {"año": 2024, "institucion": "UNIVERSITY ONE"},
                ],
            }
        )
        self.db.all_researchers.insert_many(
            [
                {"cod_rh": "0000000001", "nombre_completo": "Profile One"},
                {"cod_rh": "0000000002", "nombre_completo": "Profile Two"},
            ]
        )
        self.gate = {
            "snapshot_run_name": "snapshot",
            "cvlac_normalized_collection": "cvlac_normalized",
            "cvlac_audit_name": "cvlac_audit",
            "cvlac_parser_versions": {"2.0.0": 2},
            "cvlac_profiles": 2,
            "recognized_groups_collection": "recognized_groups",
            "gruplac_raw_collection": "groups_raw",
            "gruplac_normalized_collection": "groups_normalized",
            "gruplac_audit_name": "group_audit",
            "gruplac_parser_versions": {"2.0.2": 1},
            "gruplac_groups": 1,
            "validated_at": 1,
        }
        evidence = group_affiliation_evidence_fingerprint(
            self.db, "recognized_groups", "groups_normalized"
        )
        self.gate["affiliation_evidence_sha256"] = evidence["sha256"]

    def builder(self):
        return CheckpointedNormalizedWorkGraphBuilder(
            self.db,
            collection="works_graph_v1",
            source_collection="cvlac_normalized",
            group_source_collection="groups_normalized",
            gate=self.gate,
            batch_size=2,
            candidate_partitions=4,
        )

    def test_publishes_version_without_replacing_previous_collection(self):
        self.db.works_graph_previous.insert_one({"_id": "old-work"})
        self.db[WORK_GRAPH_PUBLICATIONS].insert_one(
            {
                "_id": "current",
                "current_collection": "works_graph_previous",
                "current_run_name": "previous_run",
            }
        )

        result = self.builder().build_checkpointed("graph_run")

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["works"], 1)
        self.assertEqual(self.db.works_graph_v1.count_documents({}), 1)
        work = self.db.works_graph_v1.find_one({})
        self.assertEqual(work["groups"][0]["affiliations"], "UNIVERSITY ONE")
        self.assertEqual(
            work["authors"][0]["affiliations"],
            [
                {
                    "institution": "UNIVERSITY ONE",
                    "source": "gruplac",
                    "group_code": "COL1",
                    "membership_period": "2020/1 - Actual",
                    "product_in_group": True,
                }
            ],
        )
        self.assertEqual(self.db.works_graph_previous.count_documents({}), 1)
        pointer = self.db[WORK_GRAPH_PUBLICATIONS].find_one({"_id": "current"})
        self.assertEqual(pointer["current_collection"], "works_graph_v1")
        self.assertEqual(pointer["previous_collection"], "works_graph_previous")
        run = self.db.works_graph_v1_graph_runs.find_one({"_id": "graph_run"})
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["audit"]["critical_anomalies"], 0)
        for stage in CHECKPOINT_STAGES:
            self.assertEqual(run["stages"][stage]["status"], "complete")
        partitions = run["stages"]["connect_title_year"]["partitions"]
        self.assertEqual(len(partitions), 4)
        self.assertTrue(
            all(state["status"] == "complete" for state in partitions.values())
        )
        self.assertEqual(
            run["stages"]["connect_title_year"]["result"]["edges"], 2
        )
        self.assertNotIn(run["artifacts"]["nodes"], self.db.list_collection_names())
        self.assertNotIn(run["artifacts"]["edges"], self.db.list_collection_names())

    def test_compact_partitioned_builder_matches_classic_graph_output(self):
        classic = CvlacWorkGraphBuilder(
            self.db,
            collection="classic_graph",
            source_collection="cvlac_normalized",
            group_source_collection="groups_normalized",
            source_mode="normalized",
            batch_size=2,
        )
        classic.build()

        self.builder().build_checkpointed("equivalence_run")

        classic_work = self.db.classic_graph.find_one({})
        compact_work = self.db.works_graph_v1.find_one({})
        classic_work.pop("updated", None)
        compact_work.pop("updated", None)
        self.assertEqual(compact_work, classic_work)

    def test_resumes_after_materialization_failure_without_repeating_prior_stages(self):
        first = self.builder()
        with patch.object(first, "_materialize", side_effect=RuntimeError("forced")):
            with self.assertRaisesRegex(RuntimeError, "forced"):
                first.build_checkpointed("resume_run")
        failed = self.db.works_graph_v1_graph_runs.find_one({"_id": "resume_run"})
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stages"]["extract_nodes"]["status"], "complete")
        self.assertEqual(failed["stages"]["connect_doi"]["status"], "complete")
        self.assertEqual(
            failed["stages"]["connect_title_year"]["status"], "complete"
        )
        self.assertEqual(failed["stages"]["materialize"]["status"], "failed")
        self.assertIn(failed["artifacts"]["nodes"], self.db.list_collection_names())

        self.gate["validated_at"] = 2
        result = self.builder().build_checkpointed("resume_run")
        self.assertEqual(result["status"], "complete")
        resumed = self.db.works_graph_v1_graph_runs.find_one({"_id": "resume_run"})
        self.assertEqual(resumed["stages"]["extract_nodes"]["attempts"], 1)
        self.assertEqual(resumed["stages"]["connect_doi"]["attempts"], 1)
        self.assertEqual(resumed["stages"]["connect_title_year"]["attempts"], 1)
        self.assertEqual(resumed["stages"]["materialize"]["attempts"], 2)

    def test_resumes_title_candidates_after_last_completed_partition(self):
        first = self.builder()
        original = first._connect_title_groups

        def fail_partition(collection, partition=None):
            if partition == 1:
                raise RuntimeError("forced partition failure")
            return original(collection, partition=partition)

        with patch.object(first, "_connect_title_groups", side_effect=fail_partition):
            with self.assertRaisesRegex(RuntimeError, "forced partition failure"):
                first.build_checkpointed("partition_resume_run")

        failed = self.db.works_graph_v1_graph_runs.find_one(
            {"_id": "partition_resume_run"}
        )
        partitions = failed["stages"]["connect_title_year"]["partitions"]
        self.assertEqual(partitions["p0000"]["status"], "complete")
        self.assertEqual(partitions["p0001"]["status"], "failed")

        result = self.builder().build_checkpointed("partition_resume_run")

        self.assertEqual(result["status"], "complete")
        resumed = self.db.works_graph_v1_graph_runs.find_one(
            {"_id": "partition_resume_run"}
        )
        partitions = resumed["stages"]["connect_title_year"]["partitions"]
        self.assertEqual(partitions["p0000"]["attempts"], 1)
        self.assertEqual(partitions["p0001"]["attempts"], 2)
        self.assertEqual(partitions["p0002"]["attempts"], 1)
        self.assertEqual(partitions["p0003"]["attempts"], 1)

    def test_failed_audit_never_publishes_or_replaces_previous_graph(self):
        self.db.works_graph_previous.insert_one({"_id": "old-work"})
        self.db[WORK_GRAPH_PUBLICATIONS].insert_one(
            {
                "_id": "current",
                "current_collection": "works_graph_previous",
                "current_run_name": "previous_run",
            }
        )
        self.db.cvlac_normalized.update_one(
            {"_id": "0000000001"}, {"$set": {"production_counts": 2}}
        )

        with self.assertRaisesRegex(RuntimeError, "work graph audit failed"):
            self.builder().build_checkpointed("failed_audit_run")

        run = self.db.works_graph_v1_graph_runs.find_one(
            {"_id": "failed_audit_run"}
        )
        self.assertEqual(run["stages"]["audit"]["status"], "failed")
        self.assertGreater(run["audit"]["critical_anomalies"], 0)
        self.assertNotIn("publish", run["stages"])
        self.assertNotIn("works_graph_v1", self.db.list_collection_names())
        self.assertIn(run["artifacts"]["output"], self.db.list_collection_names())
        pointer = self.db[WORK_GRAPH_PUBLICATIONS].find_one({"_id": "current"})
        self.assertEqual(pointer["current_collection"], "works_graph_previous")

    def test_refuses_publication_if_group_affiliation_evidence_changes(self):
        builder = self.builder()
        original = builder._materialize

        def mutate_evidence_after_materialization(*args, **kwargs):
            original(*args, **kwargs)
            self.db.recognized_groups.update_one(
                {"codigo_grupo": "COL1"},
                {
                    "$set": {
                        "instituciones_historicas.1.institucion": "CHANGED UNIVERSITY"
                    }
                },
            )

        with patch.object(
            builder,
            "_materialize",
            side_effect=mutate_evidence_after_materialization,
        ):
            with self.assertRaisesRegex(RuntimeError, "work graph audit failed"):
                builder.build_checkpointed("changed_affiliation_evidence")

        run = self.db.works_graph_v1_graph_runs.find_one(
            {"_id": "changed_affiliation_evidence"}
        )
        self.assertEqual(
            run["audit"]["critical_counts"]["affiliation_evidence_changed"], 1
        )
        self.assertNotIn("publish", run["stages"])

    def test_refuses_to_overwrite_existing_target_for_a_new_run(self):
        self.db.works_graph_v1.insert_one({"_id": "existing"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.builder().build_checkpointed("new_run")


if __name__ == "__main__":
    unittest.main()
