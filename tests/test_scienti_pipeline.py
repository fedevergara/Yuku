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
        self.entity_graph_calls = []

    def download_scienti_cvlac_full_snapshot(self, **kwargs):
        self.cvlac_call = kwargs
        return {"status": "complete", "missing_raw": 0, "target_count": 10}

    def publish_scienti_entities(self, **kwargs):
        self.entity_publication_call = kwargs
        return {"current_run_name": kwargs["run_name"]}

    def create_scienti_entity_graph(self, **kwargs):
        self.entity_graph_calls.append(kwargs)
        return {"status": "complete", "entity": kwargs["entity"]}


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
        self.assertLess(STAGES.index("entities_publish"), STAGES.index("base_graph"))
        self.assertLess(STAGES.index("entities_publish"), STAGES.index("projects_graph"))
        self.assertLess(STAGES.index("projects_graph"), STAGES.index("patents_graph"))
        self.assertLess(STAGES.index("patents_graph"), STAGES.index("base_graph"))
        self.assertLess(STAGES.index("measurements_normalize"), STAGES.index("measurements_link"))
        self.assertLess(STAGES.index("measurements_link"), STAGES.index("final_graph"))
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
                "scienti_projects_final_snapshot_1",
            )
            self.assertEqual(
                config["collections"]["patents_graph"],
                "scienti_patents_final_snapshot_1",
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


if __name__ == "__main__":
    unittest.main()
