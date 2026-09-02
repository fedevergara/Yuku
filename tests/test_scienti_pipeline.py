import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mongomock

from yuku.scienti_pipeline import (
    STAGES,
    ScientiFullPipeline,
    load_pipeline_config,
)
from yuku.socrata_snapshot import SocrataSnapshotDownloader


class FakeSocrata:
    def __init__(self, rows):
        self.rows = rows
        self.fail = False

    def get_metadata(self, dataset_id):
        return {
            "id": dataset_id,
            "name": "test",
            "rowsUpdatedAt": 123,
            "columns": [{"cachedContents": {"count": str(len(self.rows))}}],
        }

    def get(self, dataset_id, *, limit, offset, order):
        if self.fail and offset >= 2:
            raise RuntimeError("controlled interruption")
        self.last_order = order
        return self.rows[offset: offset + limit]


class SocrataSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().dam
        self.client = FakeSocrata(
            [{"code": str(value), "name": f"row {value}"} for value in range(5)]
        )

    def test_snapshot_is_complete_and_atomically_published(self):
        result = SocrataSnapshotDownloader(self.db, self.client).download(
            run_name="open_data_test",
            dataset_id="abcd-1234",
            data_collection="source_data",
            metadata_collection="source_dataset_info",
            index_fields=("code",),
            batch_size=100,
        )
        self.assertEqual(result["rows"], 5)
        self.assertEqual(self.db.source_data.count_documents({}), 5)
        self.assertEqual(
            self.db.source_data.find_one({"code": "0"})["_id"],
            "abcd-1234:0000000000",
        )
        self.assertEqual(self.db.source_dataset_info.find_one({})["id"], "abcd-1234")
        self.assertFalse(
            any(name.startswith("__yuku_") for name in self.db.list_collection_names())
        )

    def test_snapshot_resumes_from_last_committed_page(self):
        self.client = FakeSocrata(
            [{"code": str(value), "name": f"row {value}"} for value in range(150)]
        )
        downloader = SocrataSnapshotDownloader(self.db, self.client)
        self.client.fail = True
        with patch("yuku.socrata_snapshot.time.sleep"):
            with self.assertRaises(RuntimeError):
                downloader.download(
                    run_name="open_data_resume",
                    dataset_id="abcd-1234",
                    data_collection="source_data",
                    metadata_collection="source_dataset_info",
                    batch_size=100,
                )
        self.assertEqual(
            self.db["__yuku_source_data_open_data_resume_build"].count_documents({}),
            100,
        )
        self.assertNotIn("source_data", self.db.list_collection_names())
        self.client.fail = False
        result = downloader.download(
            run_name="open_data_resume",
            dataset_id="abcd-1234",
            data_collection="source_data",
            metadata_collection="source_dataset_info",
            batch_size=100,
        )
        self.assertEqual(result["rows"], 150)


class FakeYuku:
    def __init__(self):
        self.db = mongomock.MongoClient().dam
        self.client = FakeSocrata([{"value": 1}])
        self.cvlac_call = None
        self.entity_publication_call = None
        self.entity_comparison_call = None
        self.entity_graph_calls = []
        self.entity_materialization_call = None
        self.measurement_link_calls = []
        self.measurement_materialization_calls = []
        self.final_release_call = None
        self.release_cleanup_call = None
        self.bibliographic_audit_call = None
        self.full_graph_call = None

    def download_scienti_cvlac_full_snapshot(self, **kwargs):
        self.cvlac_call = kwargs
        return {"status": "complete", "missing_raw": 0, "target_count": 10}

    def publish_scienti_entities(self, **kwargs):
        self.entity_publication_call = kwargs
        return {"current_run_name": kwargs["run_name"]}

    def compare_scienti_entity_versions(self, **kwargs):
        self.entity_comparison_call = kwargs
        return {
            "status": "passed",
            "critical_findings": 0,
            "old_run_name": kwargs["old_run_name"],
            "new_run_name": kwargs["new_run_name"],
        }

    def create_scienti_entity_graph(self, **kwargs):
        self.entity_graph_calls.append(kwargs)
        return {"status": "complete", "entity": kwargs["entity"]}

    def materialize_scienti_entity_snapshot(self, **kwargs):
        self.entity_materialization_call = kwargs
        return {"status": "complete", "entity": kwargs["entity"]}

    def link_minciencias_measured_products(self, **kwargs):
        self.measurement_link_calls.append(kwargs)
        return {"status": "complete", "target_entity": kwargs["target_entity"]}

    def materialize_minciencias_enriched_graph(self, **kwargs):
        self.measurement_materialization_calls.append(kwargs)
        if kwargs.get("publish_pointer", True):
            entity = kwargs["target_entity"]
            publication = self.db[
                "scienti_work_graph_publications"
                if entity == "works"
                else "scienti_final_entity_publications"
            ]
            current_id = "current" if entity == "works" else f"current_{entity}"
            previous = publication.find_one({"_id": current_id}) or {}
            publication.replace_one(
                {"_id": current_id},
                {
                    "_id": current_id,
                    "current_collection": kwargs["target_collection"],
                    "previous_collection": previous.get("current_collection", ""),
                },
                upsert=True,
            )
        return {"status": "complete", "target_entity": kwargs["target_entity"]}

    def publish_scienti_final_release(self, **kwargs):
        self.final_release_call = kwargs
        return {"status": "published", "_id": kwargs["release_name"]}

    def audit_scienti_bibliographic_enrichment(self, **kwargs):
        self.bibliographic_audit_call = kwargs
        result = {
            "_id": kwargs["audit_name"],
            "status": "passed",
            "critical_anomalies": 0,
        }
        self.db.scienti_bibliographic_enrichment_audits.replace_one(
            {"_id": kwargs["audit_name"]}, result, upsert=True
        )
        return result

    def create_scienti_full_graph(self, **kwargs):
        self.full_graph_call = kwargs
        if kwargs.get("publish_pointer", True):
            self.db.scienti_work_graph_publications.replace_one(
                {"_id": "current"},
                {"_id": "current", "current_collection": kwargs["collection"]},
                upsert=True,
            )
        return {"status": "complete", "collection": kwargs["collection"]}

    def cleanup_scienti_final_release(self, **kwargs):
        self.release_cleanup_call = kwargs
        return {"status": "complete", "removed": []}


class ScientiPipelineTests(unittest.TestCase):
    def _config(self, directory: str, **extra):
        value = {
            "run_name": "scienti_test",
            "snapshot_tag": "test",
            "convocations": {
                "mode": "json",
                "researchers_json": str(Path(directory) / "investigadores.json"),
                "groups_json": str(Path(directory) / "grupos.json"),
            },
        }
        value.update(extra)
        path = Path(directory) / "pipeline.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return load_pipeline_config(path)

    def test_order_places_gruplac_evidence_before_cvlac_manifest(self):
        self.assertLess(STAGES.index("gruplac_normalize"), STAGES.index("cvlac_download"))
        self.assertLess(STAGES.index("cvlac_audit"), STAGES.index("base_graph"))
        self.assertLess(STAGES.index("entities_normalize"), STAGES.index("projects_semantic_audit"))
        self.assertLess(STAGES.index("projects_semantic_audit"), STAGES.index("base_graph"))
        self.assertLess(STAGES.index("works_semantic_audit"), STAGES.index("base_graph"))
        self.assertLess(STAGES.index("patents_semantic_audit"), STAGES.index("base_graph"))
        self.assertLess(STAGES.index("events_semantic_audit"), STAGES.index("base_graph"))
        self.assertLess(STAGES.index("events_semantic_audit"), STAGES.index("entities_publish"))
        self.assertLess(STAGES.index("events_semantic_audit"), STAGES.index("entities_compare"))
        self.assertLess(STAGES.index("entities_compare"), STAGES.index("entities_publish"))
        self.assertLess(STAGES.index("entities_publish"), STAGES.index("base_graph"))
        self.assertLess(STAGES.index("entities_publish"), STAGES.index("projects_graph"))
        self.assertLess(STAGES.index("projects_graph"), STAGES.index("patents_graph"))
        self.assertLess(STAGES.index("patents_graph"), STAGES.index("events_materialize"))
        self.assertLess(STAGES.index("events_materialize"), STAGES.index("base_graph"))
        self.assertLess(STAGES.index("measurements_normalize"), STAGES.index("measurements_link"))
        self.assertLess(STAGES.index("measurements_link"), STAGES.index("final_graph"))
        self.assertLess(STAGES.index("final_graph"), STAGES.index("projects_measurements_link"))
        self.assertLess(STAGES.index("projects_measurements_link"), STAGES.index("projects_final"))
        self.assertLess(STAGES.index("projects_final"), STAGES.index("patents_measurements_link"))
        self.assertLess(STAGES.index("patents_measurements_link"), STAGES.index("patents_final"))
        self.assertLess(STAGES.index("patents_final"), STAGES.index("events_measurements_link"))
        self.assertLess(STAGES.index("events_measurements_link"), STAGES.index("events_final"))
        self.assertLess(STAGES.index("events_final"), STAGES.index("bibliographic_audit"))
        self.assertLess(STAGES.index("bibliographic_audit"), STAGES.index("final_release"))
        self.assertLess(STAGES.index("events_final"), STAGES.index("final_release"))
        self.assertLess(STAGES.index("final_release"), STAGES.index("cleanup"))
        self.assertEqual(STAGES[-1], "cleanup")

    def test_json_is_default_and_derived_collections_are_versioned(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            value = {
                "run_name": "scienti_default_json",
                "snapshot_tag": "snapshot_1",
                "convocations": {
                    "researchers_json": str(Path(directory) / "investigadores.json"),
                    "groups_json": str(Path(directory) / "grupos.json"),
                },
            }
            path = Path(directory) / "pipeline.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            config = load_pipeline_config(path)
            self.assertEqual(config["convocations"]["mode"], "json")
            self.assertEqual(
                config["collections"]["measured_products"],
                "minciencias_measured_products_snapshot_1",
            )
            self.assertEqual(
                config["collections"]["measurement_links"],
                "minciencias_measured_product_links_snapshot_1",
            )
            self.assertEqual(
                config["collections"]["projects_graph"],
                "scienti_projects_graph_snapshot_1",
            )
            self.assertEqual(
                config["collections"]["patents_graph"],
                "scienti_patents_graph_snapshot_1",
            )
            self.assertEqual(
                config["collections"]["final_graph"],
                "scienti_works_final_snapshot_1",
            )
            self.assertEqual(
                config["collections"]["projects_final"],
                "scienti_projects_final_snapshot_1",
            )
            self.assertEqual(
                config["collections"]["patents_final"],
                "scienti_patents_final_snapshot_1",
            )
            self.assertEqual(
                config["collections"]["events_final"],
                "scienti_events_final_snapshot_1",
            )

    def test_project_and_patent_graph_stages_use_published_normalizations(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            pipeline = ScientiFullPipeline(yuku, self._config(directory))
            project = pipeline._entity_graph("projects")
            patent = pipeline._entity_graph("patents")
            self.assertEqual(project["entity"], "projects")
            self.assertEqual(patent["entity"], "patents")
            self.assertEqual(
                yuku.entity_graph_calls[0]["source_collection"],
                pipeline.names["entity_projects"],
            )
            self.assertEqual(
                yuku.entity_graph_calls[1]["target_collection"],
                pipeline.names["patents_graph"],
            )

    def test_events_are_materialized_into_an_intermediate_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            pipeline = ScientiFullPipeline(yuku, self._config(directory))

            result = pipeline._events_materialize()

            self.assertEqual(result["entity"], "events")
            self.assertEqual(
                yuku.entity_materialization_call["source_collection"],
                pipeline.names["entity_events"],
            )
            self.assertEqual(
                yuku.entity_materialization_call["target_collection"],
                pipeline.names["events_snapshot"],
            )

    def test_official_measurements_route_to_all_four_final_entities(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            pipeline = ScientiFullPipeline(yuku, self._config(directory))
            for entity in ("works", "projects", "patents", "events"):
                pipeline._measurements_link(entity)
                pipeline._final_measurement_entity(entity)
            self.assertEqual(
                [value["target_entity"] for value in yuku.measurement_link_calls],
                ["works", "projects", "patents", "events"],
            )
            self.assertEqual(
                [value["target_collection"] for value in yuku.measurement_materialization_calls],
                [
                    pipeline.names["final_graph"],
                    pipeline.names["projects_final"],
                    pipeline.names["patents_final"],
                    pipeline.names["events_final"],
                ],
            )
            self.assertTrue(
                all(
                    value["publish_pointer"] is False
                    for value in yuku.measurement_materialization_calls
                )
            )

    def test_final_release_is_joint_and_cleanup_protects_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            pipeline = ScientiFullPipeline(yuku, self._config(directory))
            pipeline._bibliographic_audit()
            pipeline._final_release()
            self.assertEqual(
                set(yuku.final_release_call["collections"]),
                {"works", "projects", "patents", "events"},
            )
            previous_collections = {
                entity: f"previous_{entity}"
                for entity in ("works", "projects", "patents", "events")
            }
            yuku.db.scienti_final_release_publications.insert_many(
                [
                    {
                        "_id": "current",
                        "current_release": "release_test",
                        "previous_release": "release_previous",
                    },
                    {
                        "_id": "release_previous",
                        "collections": previous_collections,
                    },
                ]
            )
            current_entity_collections = {
                entity: pipeline.names[f"entity_{entity}"]
                for entity in ("works", "projects", "patents", "events")
            }
            previous_entity_collections = {
                entity: f"previous_entity_{entity}"
                for entity in ("works", "projects", "patents", "events")
            }
            yuku.db.scienti_entity_publications.insert_many(
                [
                    {
                        "_id": "current",
                        "current_run_name": "scienti_test_entities",
                        "previous_run_name": "previous_entities",
                        "destinations": current_entity_collections,
                    },
                    {
                        "_id": "previous_entities",
                        "record_type": "release",
                        "destinations": previous_entity_collections,
                    },
                ]
            )
            pipeline._cleanup()
            self.assertIn(
                pipeline.names["cvlac_raw"], yuku.release_cleanup_call["protected"]
            )
            self.assertIn(
                pipeline.names["base_graph"], yuku.release_cleanup_call["candidates"]
            )
            self.assertNotIn(
                pipeline.names["final_graph"], yuku.release_cleanup_call["candidates"]
            )
            self.assertTrue(
                set(previous_collections.values()).issubset(
                    yuku.release_cleanup_call["candidates"]
                )
            )
            self.assertTrue(
                set(current_entity_collections.values()).issubset(
                    yuku.release_cleanup_call["protected"]
                )
            )
            self.assertTrue(
                set(previous_entity_collections.values()).issubset(
                    yuku.release_cleanup_call["candidates"]
                )
            )
            self.assertFalse(
                yuku.release_cleanup_call["reset_entity_publication"]
            )

    def test_final_release_requires_the_bibliographic_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            pipeline = ScientiFullPipeline(yuku, self._config(directory))
            previous = {
                "_id": "current",
                "current_collection": "preceding_safe_graph",
            }
            yuku.db.scienti_work_graph_publications.insert_one(previous)
            pipeline._final_measurement_entity("works")

            with self.assertRaises(RuntimeError):
                pipeline._final_release()

            self.assertIsNone(yuku.final_release_call)
            self.assertEqual(
                yuku.db.scienti_work_graph_publications.find_one({"_id": "current"}),
                previous,
            )

    def test_joint_release_updates_legacy_pointers_only_after_all_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            expected_previous = {}
            for entity in ("works", "projects", "patents", "events"):
                collection = (
                    "scienti_work_graph_publications"
                    if entity == "works"
                    else "scienti_final_entity_publications"
                )
                current_id = "current" if entity == "works" else f"current_{entity}"
                pointer = {
                    "_id": current_id,
                    "current_collection": f"preceding_safe_{entity}",
                }
                yuku.db[collection].insert_one(pointer)
                expected_previous[entity] = pointer
            pipeline = ScientiFullPipeline(yuku, self._config(directory))

            for entity in ("works", "projects", "patents", "events"):
                pipeline._final_measurement_entity(entity)
                collection = (
                    "scienti_work_graph_publications"
                    if entity == "works"
                    else "scienti_final_entity_publications"
                )
                current_id = "current" if entity == "works" else f"current_{entity}"
                self.assertEqual(
                    yuku.db[collection].find_one({"_id": current_id}),
                    expected_previous[entity],
                )

            pipeline._bibliographic_audit()
            pipeline._final_release()

            final_names = {
                "works": "final_graph",
                "projects": "projects_final",
                "patents": "patents_final",
                "events": "events_final",
            }
            for entity, name_key in final_names.items():
                collection = (
                    "scienti_work_graph_publications"
                    if entity == "works"
                    else "scienti_final_entity_publications"
                )
                current_id = "current" if entity == "works" else f"current_{entity}"
                pointer = yuku.db[collection].find_one({"_id": current_id})
                self.assertEqual(pointer["current_collection"], pipeline.names[name_key])
                self.assertEqual(
                    pointer["previous_collection"],
                    f"preceding_safe_{entity}",
                )

    def test_base_graph_build_restores_the_preceding_public_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            previous = {
                "_id": "current",
                "current_collection": "preceding_safe_graph",
            }
            yuku.db.scienti_work_graph_publications.insert_one(previous)
            pipeline = ScientiFullPipeline(yuku, self._config(directory))

            result = pipeline._base_graph()

            self.assertEqual(result["collection"], pipeline.names["base_graph"])
            self.assertEqual(
                yuku.db.scienti_work_graph_publications.find_one({"_id": "current"}),
                previous,
            )
            self.assertFalse(yuku.full_graph_call["publish_pointer"])

    def test_dry_run_validates_complete_plan_without_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            pipeline = ScientiFullPipeline(FakeYuku(), self._config(directory))
            result = pipeline.run(dry_run=True)
            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["selected_stages"], list(STAGES))

    def test_cvlac_manifest_includes_group_members_and_ambiguous_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            config = self._config(directory)
            pipeline = ScientiFullPipeline(yuku, config)
            result = pipeline._cvlac_download()
            self.assertEqual(result["status"], "complete")
            sources = yuku.cvlac_call["identifier_sources"]
            self.assertIn(
                (config["collections"]["gruplac_normalized"], "members.cod_rh"),
                sources,
            )
            self.assertIn(
                (
                    config["collections"]["recognized_researchers"],
                    "consulta_scienti.coincidencias",
                ),
                sources,
            )

    def test_stage_cannot_run_before_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            pipeline = ScientiFullPipeline(FakeYuku(), self._config(directory))
            pipeline._prepare()
            with self.assertRaises(RuntimeError):
                pipeline._run_stage("cvlac_download", lambda: {})

    def test_entity_publication_is_an_explicit_audited_snapshot_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            pipeline = ScientiFullPipeline(yuku, self._config(directory))
            result = pipeline._entities_publish()
            self.assertEqual(result["current_run_name"], "scienti_test_entities")
            self.assertTrue(
                yuku.entity_publication_call["allow_snapshot_without_comparison"]
            )
            self.assertEqual(yuku.entity_publication_call["comparison_name"], "")

    def test_existing_release_is_compared_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "investigadores.json").write_text(
                '[{"investigador_id":"INV-1"}]', encoding="utf-8"
            )
            Path(directory, "grupos.json").write_text(
                '[{"grupo_id":"GRP-1"}]', encoding="utf-8"
            )
            yuku = FakeYuku()
            yuku.db.scienti_entity_publications.insert_one({
                "_id": "current", "current_run_name": "previous_entities",
            })
            pipeline = ScientiFullPipeline(yuku, self._config(directory))

            comparison = pipeline._entities_compare()
            publication = pipeline._entities_publish()

            self.assertEqual(comparison["status"], "passed")
            self.assertEqual(
                yuku.entity_comparison_call["old_run_name"], "previous_entities"
            )
            self.assertEqual(
                yuku.entity_comparison_call["new_run_name"],
                "scienti_test_entities",
            )
            self.assertEqual(
                yuku.entity_publication_call["comparison_name"],
                "scienti_test_entity_compare",
            )
            self.assertFalse(
                yuku.entity_publication_call["allow_snapshot_without_comparison"]
            )
            self.assertEqual(publication["current_run_name"], "scienti_test_entities")


if __name__ == "__main__":
    unittest.main()
