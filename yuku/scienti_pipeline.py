"""Safe end-to-end orchestration of the public Minciencias/Scienti pipeline."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from yuku.scienti_search import read_json_array
from yuku.scienti_bibliographic_audit import BIBLIOGRAPHIC_AUDITS
from yuku.socrata_snapshot import SocrataSnapshotDownloader


PIPELINE_RUNS = "scienti_pipeline_runs"
PIPELINE_LOCKS = "scienti_pipeline_locks"
PIPELINE_VERSION = "scienti-full-pipeline-v14"


STAGES = (
    "open_data_researchers",
    "open_data_groups",
    "open_data_production",
    "convocations",
    "researcher_directory",
    "resolve_researchers",
    "resolve_groups",
    "gruplac_download",
    "gruplac_normalize",
    "gruplac_verify",
    "gruplac_audit",
    "cvlac_download",
    "cvlac_normalize",
    "cvlac_audit",
    "entities_normalize",
    "projects_semantic_audit",
    "works_semantic_audit",
    "patents_semantic_audit",
    "events_semantic_audit",
    "entities_compare",
    "entities_publish",
    "projects_graph",
    "patents_graph",
    "events_materialize",
    "base_graph",
    "measurements_normalize",
    "measurements_link",
    "final_graph",
    "projects_measurements_link",
    "projects_final",
    "patents_measurements_link",
    "patents_final",
    "events_measurements_link",
    "events_final",
    "bibliographic_audit",
    "final_release",
    "cleanup",
)


DEFAULT_DATASETS = {
    "researchers": {
        "id": "bqtm-4y2h",
        "data_collection": "cvlac_data",
        "metadata_collection": "cvlac_dataset_info",
        "indexes": ["id_persona_pr", "id_convocatoria"],
    },
    "groups": {
        "id": "hrhc-c4wu",
        "data_collection": "gruplac_groups_data",
        "metadata_collection": "gruplac_groups_dataset_info",
        "indexes": ["cod_grupo_gr", "id_convocatoria"],
    },
    "production": {
        "id": "33dq-ab5a",
        "data_collection": "gruplac_production_data",
        "metadata_collection": "gruplac_production_dataset_info",
        "indexes": [
            "id_producto_pd",
            "id_persona_pd",
            "cod_grupo_gr",
            "nme_tipologia_pd",
        ],
    },
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    output = deepcopy(target)
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(output.get(key), dict):
            output[key] = _merge(output[key], value)
        else:
            output[key] = deepcopy(value)
    return output


def _fingerprint(value: Any) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def load_pipeline_config(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("the pipeline configuration must be a JSON object")
    run_name = str(value.get("run_name") or "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", run_name):
        raise ValueError("run_name must contain only letters, numbers and underscores")
    tag = str(value.get("snapshot_tag") or run_name)
    if not re.fullmatch(r"[A-Za-z0-9_]{1,48}", tag):
        raise ValueError("snapshot_tag must contain only letters, numbers and underscores")

    defaults: dict[str, Any] = {
        "pipeline_version": PIPELINE_VERSION,
        "run_name": run_name,
        "snapshot_tag": tag,
        "open_data": {
            "refresh": False,
            "batch_size": 20000,
            "datasets": DEFAULT_DATASETS,
        },
        "convocations": {
            "mode": "json",
            "input_dir": "",
            "excel": "",
            "researchers_json": "",
            "groups_json": "",
        },
        "network": {
            "directory_workers": 4,
            "profile_workers": 8,
            "requests_per_second": 4.0,
            "fallback_requests_per_second": 2.0,
            "fallback_after_errors": 5,
            "identifier_delay": 0.75,
            "max_download_passes": 3,
        },
        "processing": {
            "cvlac_workers": 8,
            "gruplac_workers": 8,
            "normalization_batch_size": 50,
            "graph_batch_size": 500,
            "graph_partitions": 64,
            "measurement_batch_size": 1000,
            "entity_batch_size": 500,
            "entity_graph_batch_size": 1000,
            "entity_graph_max_candidate_group": 100,
            "semantic_audit_batch_size": 2000,
            "semantic_audit_example_limit": 100,
            "progress_every": 10000,
            "verify_incomplete_gruplac": True,
            "cleanup_transient": True,
        },
        "collections": {
            "all_researchers": "all_researchers",
            "directory_pages": "scienti_researcher_directory_pages",
            "recognized_researchers": "recognized_researchers",
            "recognized_groups": "recognized_groups",
            "cvlac_seed_raw": "cvlac_stage_raw",
            "cvlac_raw": f"cvlac_stage_raw_full_{tag}",
            "cvlac_normalized": f"cvlac_related_works_full_{tag}",
            "gruplac_raw": f"gruplac_stage_raw_{tag}",
            "gruplac_downloads": f"gruplac_downloads_{tag}",
            "gruplac_normalized": f"gruplac_related_works_{tag}",
            "entity_works": f"scienti_works_normalized_{tag}",
            "entity_projects": f"scienti_projects_normalized_{tag}",
            "entity_patents": f"scienti_patents_normalized_{tag}",
            "entity_events": f"scienti_events_normalized_{tag}",
            "projects_graph": f"scienti_projects_graph_{tag}",
            "patents_graph": f"scienti_patents_graph_{tag}",
            "events_snapshot": f"scienti_events_snapshot_{tag}",
            "base_graph": f"cvlac_works_graph_base_{tag}",
            "measured_products": f"minciencias_measured_products_{tag}",
            "measurement_links": f"minciencias_measured_product_links_{tag}",
            "projects_measurement_links": f"minciencias_measured_project_links_{tag}",
            "patents_measurement_links": f"minciencias_measured_patent_links_{tag}",
            "events_measurement_links": f"minciencias_measured_event_links_{tag}",
            "final_graph": f"scienti_works_final_{tag}",
            "projects_final": f"scienti_projects_final_{tag}",
            "patents_final": f"scienti_patents_final_{tag}",
            "events_final": f"scienti_events_final_{tag}",
        },
    }
    config = _merge(defaults, value)
    config["config_path"] = str(source)
    collections = config["collections"]
    if len(set(collections.values())) != len(collections):
        raise ValueError("pipeline collection names must be distinct")
    if collections["base_graph"] == collections["final_graph"]:
        raise ValueError("base_graph and final_graph must differ")
    mode = str(config["convocations"].get("mode") or "").lower()
    if mode not in {"pdf", "json"}:
        raise ValueError("convocations.mode must be 'pdf' or 'json'")
    config["convocations"]["mode"] = mode
    for key in ("researchers_json", "groups_json"):
        raw = str(config["convocations"].get(key) or "")
        if not raw:
            raise ValueError(f"convocations.{key} is required")
        config["convocations"][key] = str(Path(raw).expanduser().resolve())
    if mode == "pdf":
        for key in ("input_dir", "excel"):
            raw = str(config["convocations"].get(key) or "")
            if not raw:
                raise ValueError(f"convocations.{key} is required in PDF mode")
            config["convocations"][key] = str(Path(raw).expanduser().resolve())
    return config


def _json_source_summary(path: str, id_field: str) -> dict[str, Any]:
    records = read_json_array(path)
    identifiers = [str(record.get(id_field) or "") for record in records]
    if any(not value for value in identifiers):
        raise ValueError(f"{path}: every record requires {id_field}")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{path}: {id_field} is not unique")
    return {
        "path": str(Path(path).resolve()),
        "records": len(records),
        "sha256": sha256(Path(path).read_bytes()).hexdigest(),
    }


class ScientiFullPipeline:
    """Execute the complete Scienti workflow with strict stage dependencies."""

    def __init__(self, yuku, config: dict[str, Any]):
        self.yuku = yuku
        self.db = yuku.db
        self.config = deepcopy(config)
        self.run_name = self.config["run_name"]
        self.runs = self.db[PIPELINE_RUNS]
        self.locks = self.db[PIPELINE_LOCKS]
        self.config_hash = _fingerprint(
            {key: value for key, value in self.config.items() if key != "config_path"}
        )

    @property
    def names(self) -> dict[str, str]:
        return self.config["collections"]

    def _prepare(self) -> dict[str, Any]:
        existing = self.runs.find_one({"_id": self.run_name})
        if existing and existing.get("config_hash") != self.config_hash:
            raise ValueError(
                f"pipeline {self.run_name!r} already exists with another configuration"
            )
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$setOnInsert": {
                    "created_at": utc_now(),
                    "pipeline_version": PIPELINE_VERSION,
                    "config": self.config,
                    "config_hash": self.config_hash,
                    "stages": {},
                }
            },
            upsert=True,
        )
        return self.runs.find_one({"_id": self.run_name}) or {}

    def _acquire_lock(self) -> None:
        now = utc_now()
        expires = now + timedelta(days=7)
        try:
            lock = self.locks.find_one_and_update(
                {
                    "_id": "full_scienti_pipeline",
                    "$or": [
                        {"owner": self.run_name},
                        {"expires_at": {"$lt": now}},
                        {"owner": {"$exists": False}},
                    ],
                },
                {
                    "$set": {
                        "owner": self.run_name,
                        "acquired_at": now,
                        "expires_at": expires,
                    }
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            lock = None
        if not lock or lock.get("owner") != self.run_name:
            current = self.locks.find_one({"_id": "full_scienti_pipeline"}) or {}
            raise RuntimeError(
                f"another full pipeline owns the lock: {current.get('owner')}"
            )

    def _release_lock(self) -> None:
        self.locks.delete_one(
            {"_id": "full_scienti_pipeline", "owner": self.run_name}
        )

    def plan(self) -> list[dict[str, Any]]:
        run = self.runs.find_one({"_id": self.run_name}) or {}
        states = run.get("stages") or {}
        return [
            {
                "position": position,
                "stage": stage,
                "status": (states.get(stage) or {}).get("status", "pending"),
            }
            for position, stage in enumerate(STAGES, start=1)
        ]

    def preflight(self) -> dict[str, Any]:
        """Validate local inputs and immutable names without mutating data."""
        section = self.config["convocations"]
        source_summary: dict[str, Any]
        if section["mode"] == "pdf":
            input_dir = Path(section["input_dir"])
            if not input_dir.is_dir():
                raise ValueError(f"convocation PDF directory was not found: {input_dir}")
            pdf_count = sum(1 for _ in input_dir.rglob("*.pdf"))
            if pdf_count < 1:
                raise ValueError("the convocation directory contains no PDF files")
            missing_tools = [
                name
                for name in ("pdfinfo", "pdftotext", "pdftohtml", "gs")
                if not shutil.which(name)
            ]
            if missing_tools:
                raise RuntimeError(
                    "missing PDF extraction dependencies: " + ", ".join(missing_tools)
                )
            source_summary = {"mode": "pdf", "pdf_files": pdf_count}
        else:
            source_summary = {
                "mode": "json",
                "researchers": _json_source_summary(
                    section["researchers_json"], "investigador_id"
                ),
                "groups": _json_source_summary(section["groups_json"], "grupo_id"),
            }
        dataset_ids = {
            key: value.get("id")
            for key, value in self.config["open_data"]["datasets"].items()
        }
        if set(dataset_ids) != {"researchers", "groups", "production"}:
            raise ValueError("open_data.datasets must define researchers, groups and production")
        if any(not re.fullmatch(r"[a-z0-9]{4}-[a-z0-9]{4}", str(value or "")) for value in dataset_ids.values()):
            raise ValueError(f"invalid Socrata dataset identifiers: {dataset_ids}")
        return {
            "pipeline_version": PIPELINE_VERSION,
            "run_name": self.run_name,
            "config_hash": self.config_hash,
            "convocations": source_summary,
            "datasets": dataset_ids,
            "collections": deepcopy(self.names),
            "stages": list(STAGES),
        }

    def _run_stage(
        self,
        name: str,
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        run = self.runs.find_one({"_id": self.run_name}) or {}
        previous = (run.get("stages") or {}).get(name) or {}
        if previous.get("status") == "complete":
            print(
                f"{utc_now().isoformat()} INFO: pipeline stage={name} already complete",
                flush=True,
            )
            return previous.get("summary") or {}
        position = STAGES.index(name)
        incomplete_dependencies = [
            stage
            for stage in STAGES[:position]
            if (((run.get("stages") or {}).get(stage) or {}).get("status") != "complete")
        ]
        if incomplete_dependencies:
            raise RuntimeError(
                f"stage {name} has incomplete dependencies: {incomplete_dependencies}"
            )
        path = f"stages.{name}"
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$set": {
                    "status": "running",
                    "current_stage": name,
                    f"{path}.status": "running",
                    f"{path}.started_at": utc_now(),
                    "last_progress_at": utc_now(),
                },
                "$inc": {f"{path}.attempts": 1},
                "$unset": {f"{path}.error": ""},
            },
        )
        print(
            f"{utc_now().isoformat()} INFO: pipeline stage={name} started",
            flush=True,
        )
        try:
            summary = action() or {}
            self.runs.update_one(
                {"_id": self.run_name},
                {
                    "$set": {
                        f"{path}.status": "complete",
                        f"{path}.finished_at": utc_now(),
                        f"{path}.summary": summary,
                        "last_progress_at": utc_now(),
                    }
                },
            )
            print(
                f"{utc_now().isoformat()} INFO: pipeline stage={name} complete",
                flush=True,
            )
            return summary
        except Exception as error:
            self.runs.update_one(
                {"_id": self.run_name},
                {
                    "$set": {
                        "status": "failed",
                        f"{path}.status": "failed",
                        f"{path}.failed_at": utc_now(),
                        f"{path}.error": f"{type(error).__name__}: {error}",
                    }
                },
            )
            raise

    def _open_data(self, key: str) -> dict[str, Any]:
        section = self.config["open_data"]
        spec = section["datasets"][key]
        return SocrataSnapshotDownloader(self.db, self.yuku.client).download(
            run_name=f"{self.run_name}_open_data_{key}",
            dataset_id=spec["id"],
            data_collection=spec["data_collection"],
            metadata_collection=spec["metadata_collection"],
            index_fields=spec.get("indexes") or (),
            batch_size=int(section["batch_size"]),
            refresh=bool(section.get("refresh")),
        )

    def _convocations(self) -> dict[str, Any]:
        section = self.config["convocations"]
        if section["mode"] == "pdf":
            input_dir = Path(section["input_dir"])
            if not input_dir.is_dir():
                raise ValueError(f"convocation PDF directory was not found: {input_dir}")
            processor = Path(__file__).with_name("minciencias_convocations.py")
            subprocess.run(
                [
                    sys.executable,
                    str(processor),
                    str(input_dir),
                    "--salida",
                    section["excel"],
                    "--investigadores-json",
                    section["researchers_json"],
                    "--grupos-json",
                    section["groups_json"],
                ],
                check=True,
            )
        researchers = _json_source_summary(
            section["researchers_json"], "investigador_id"
        )
        groups = _json_source_summary(section["groups_json"], "grupo_id")
        return {"mode": section["mode"], "researchers": researchers, "groups": groups}

    def _directory(self) -> dict[str, Any]:
        network = self.config["network"]
        result = self.yuku.download_all_researchers_directory(
            collection=self.names["all_researchers"],
            pages_collection=self.names["directory_pages"],
            workers=int(network["directory_workers"]),
            requests_per_second=float(network["requests_per_second"]),
            limit_pages=0,
            refresh=False,
        )
        count = self.db[self.names["all_researchers"]].count_documents({})
        if count < 1:
            raise RuntimeError("the public researcher directory is empty")
        return {**result, "researchers": count}

    def _resolve_researchers(self) -> dict[str, Any]:
        result = self.yuku.recognize_researchers_json(
            self.config["convocations"]["researchers_json"],
            collection=self.names["recognized_researchers"],
            delay=float(self.config["network"]["identifier_delay"]),
        )
        if int(result.get("error", 0)):
            raise RuntimeError(f"researcher identifier lookup has errors: {result}")
        return result

    def _resolve_groups(self) -> dict[str, Any]:
        result = self.yuku.recognize_groups_json(
            self.config["convocations"]["groups_json"],
            collection=self.names["recognized_groups"],
            delay=float(self.config["network"]["identifier_delay"]),
        )
        transport_errors = self.db[self.names["recognized_groups"]].count_documents(
            {"consulta_scienti.error": {"$exists": True}}
        )
        if transport_errors:
            raise RuntimeError(
                f"group identifier lookup has {transport_errors} transport errors"
            )
        return {**result, "transport_errors": 0}

    def _gruplac_download(self) -> dict[str, Any]:
        network = self.config["network"]
        result = self.yuku.download_scienti_gruplac_profiles(
            source_collection=self.names["recognized_groups"],
            raw_collection=self.names["gruplac_raw"],
            state_collection=self.names["gruplac_downloads"],
            workers=int(network["profile_workers"]),
            requests_per_second=float(network["requests_per_second"]),
        )
        expected = self.db[self.names["recognized_groups"]].count_documents(
            {"url_gruplac": {"$type": "string"}}
        )
        actual = self.db[self.names["gruplac_raw"]].count_documents({})
        if actual != expected:
            raise RuntimeError(
                f"GrupLAC snapshot coverage is {actual}/{expected}, rerun to retry"
            )
        return {**result, "expected_groups": expected, "raw_groups": actual}

    def _gruplac_normalize(self) -> dict[str, Any]:
        processing = self.config["processing"]
        return self.yuku.normalize_scienti_gruplac_snapshot(
            run_name=f"{self.run_name}_gruplac_normalize",
            source_raw_collection=self.names["gruplac_raw"],
            source_state_collection=self.names["gruplac_downloads"],
            destination_collection=self.names["gruplac_normalized"],
            workers=int(processing["gruplac_workers"]),
            progress_every=50,
        )

    def _gruplac_verify(self) -> dict[str, Any]:
        if not self.config["processing"].get("verify_incomplete_gruplac", True):
            return {"status": "skipped_by_configuration"}
        return self.yuku.verify_scienti_gruplac_incomplete(
            run_name=f"{self.run_name}_gruplac_verify",
            source_raw_collection=self.names["gruplac_raw"],
            source_state_collection=self.names["gruplac_downloads"],
            normalized_collection=self.names["gruplac_normalized"],
            workers=1,
            requests_per_second=0.25,
        )

    def _gruplac_audit(self) -> dict[str, Any]:
        verify_name = (
            f"{self.run_name}_gruplac_verify"
            if self.config["processing"].get("verify_incomplete_gruplac", True)
            else None
        )
        result = self.yuku.audit_scienti_gruplac_normalization(
            audit_name=f"{self.run_name}_gruplac_audit",
            recognized_groups_collection=self.names["recognized_groups"],
            raw_collection=self.names["gruplac_raw"],
            state_collection=self.names["gruplac_downloads"],
            normalized_collection=self.names["gruplac_normalized"],
            verification_run_name=verify_name,
        )
        if int(result.get("critical_anomalies", 0)):
            raise RuntimeError(f"critical GrupLAC audit anomalies: {result}")
        return result

    def _cvlac_download(self) -> dict[str, Any]:
        datasets = self.config["open_data"]["datasets"]
        network = self.config["network"]
        sources = (
            (self.names["recognized_researchers"], "cod_rh"),
            (self.names["recognized_researchers"], "consulta_scienti.coincidencias"),
            (datasets["production"]["data_collection"], "id_persona_pd"),
            (datasets["researchers"]["data_collection"], "id_persona_pr"),
            (self.names["recognized_groups"], "cod_rh_lider"),
            (self.names["gruplac_normalized"], "members.cod_rh"),
            (self.names["all_researchers"], "cod_rh"),
        )
        result = self.yuku.download_scienti_cvlac_full_snapshot(
            run_name=f"{self.run_name}_cvlac",
            seed_collection=self.names["cvlac_seed_raw"],
            raw_collection=self.names["cvlac_raw"],
            workers=int(network["profile_workers"]),
            requests_per_second=float(network["requests_per_second"]),
            max_passes=int(network["max_download_passes"]),
            copy_seed=self.names["cvlac_seed_raw"] in self.db.list_collection_names(),
            fallback_requests_per_second=float(
                network["fallback_requests_per_second"]
            ),
            fallback_after_errors=int(network["fallback_after_errors"]),
            identifier_sources=sources,
        )
        if result.get("status") != "complete" or int(result.get("missing_raw", 0)):
            raise RuntimeError(f"CVLAC snapshot is incomplete: {result}")
        return result

    def _cvlac_normalize(self) -> dict[str, Any]:
        processing = self.config["processing"]
        return self.yuku.normalize_scienti_cvlac_snapshot(
            run_name=f"{self.run_name}_cvlac_normalize",
            source_snapshot_run_name=f"{self.run_name}_cvlac",
            destination_collection=self.names["cvlac_normalized"],
            batch_size=int(processing["normalization_batch_size"]),
            progress_every=500,
            workers=int(processing["cvlac_workers"]),
        )

    def _cvlac_audit(self) -> dict[str, Any]:
        result = self.yuku.audit_scienti_cvlac_normalization(
            audit_name=f"{self.run_name}_cvlac_audit",
            normalization_run_name=f"{self.run_name}_cvlac_normalize",
            progress_every=10000,
        )
        if int(result.get("critical_anomalies", 0)):
            raise RuntimeError(f"critical CVLAC audit anomalies: {result}")
        return result

    def _base_graph(self) -> dict[str, Any]:
        processing = self.config["processing"]
        return self.yuku.create_scienti_full_graph(
            run_name=f"{self.run_name}_cvlac",
            graph_run_name=f"{self.run_name}_base_graph",
            collection=self.names["base_graph"],
            cvlac_related_collection=self.names["cvlac_normalized"],
            cvlac_audit_name=f"{self.run_name}_cvlac_audit",
            gruplac_raw_collection=self.names["gruplac_raw"],
            gruplac_related_collection=self.names["gruplac_normalized"],
            gruplac_audit_name=f"{self.run_name}_gruplac_audit",
            recognized_groups_collection=self.names["recognized_groups"],
            batch_size=int(processing["graph_batch_size"]),
            candidate_partitions=int(processing["graph_partitions"]),
            publish_pointer=False,
        )

    def _entity_graph(self, entity: str) -> dict[str, Any]:
        processing = self.config["processing"]
        return self.yuku.create_scienti_entity_graph(
            entity=entity,
            run_name=f"{self.run_name}_{entity}_graph",
            source_collection=self.names[f"entity_{entity}"],
            target_collection=self.names[f"{entity}_graph"],
            entity_run_name=f"{self.run_name}_entities",
            batch_size=int(processing.get("entity_graph_batch_size", 1000)),
            progress_every=int(processing["progress_every"]),
            max_candidate_group=int(
                processing.get("entity_graph_max_candidate_group", 100)
            ),
        )

    def _events_materialize(self) -> dict[str, Any]:
        return self.yuku.materialize_scienti_entity_snapshot(
            entity="events",
            run_name=f"{self.run_name}_events_materialize",
            source_collection=self.names["entity_events"],
            target_collection=self.names["events_snapshot"],
            entity_run_name=f"{self.run_name}_entities",
        )

    def _entities_normalize(self) -> dict[str, Any]:
        processing = self.config["processing"]
        return self.yuku.normalize_scienti_entities(
            run_name=f"{self.run_name}_entities",
            cvlac_collection=self.names["cvlac_normalized"],
            gruplac_collection=self.names["gruplac_normalized"],
            works_collection=self.names["entity_works"],
            projects_collection=self.names["entity_projects"],
            patents_collection=self.names["entity_patents"],
            events_collection=self.names["entity_events"],
            batch_size=int(processing["entity_batch_size"]),
            progress_every=int(processing["progress_every"]),
        )

    def _projects_semantic_audit(self) -> dict[str, Any]:
        processing = self.config["processing"]
        result = self.yuku.audit_scienti_projects_semantics(
            audit_name=f"{self.run_name}_projects_semantic_audit",
            collection=self.names["entity_projects"],
            entity_run_name=f"{self.run_name}_entities",
            progress_every=int(processing["progress_every"]),
            batch_size=int(processing["semantic_audit_batch_size"]),
            example_limit=int(processing["semantic_audit_example_limit"]),
        )
        if int(result.get("critical_findings", 0)):
            raise RuntimeError(f"critical project semantic findings: {result}")
        return result

    def _auxiliary_semantic_audit(self, entity: str) -> dict[str, Any]:
        processing = self.config["processing"]
        collection = self.names[f"entity_{entity}"]
        result = self.yuku.audit_scienti_auxiliary_semantics(
            audit_name=f"{self.run_name}_{entity}_semantic_audit",
            entity=entity,
            collection=collection,
            entity_run_name=f"{self.run_name}_entities",
            progress_every=int(processing["progress_every"]),
            batch_size=int(processing["semantic_audit_batch_size"]),
            example_limit=int(processing["semantic_audit_example_limit"]),
        )
        if int(result.get("critical_findings", 0)):
            raise RuntimeError(f"critical {entity} semantic findings: {result}")
        return result

    def _entities_publish(self) -> dict[str, Any]:
        """Publish one audited temporal snapshot after every semantic gate."""
        target_run = f"{self.run_name}_entities"
        current = self.db["scienti_entity_publications"].find_one(
            {"_id": "current"}
        ) or {}
        current_run = str(current.get("current_run_name") or "")
        comparison_name = (
            self._entity_comparison_name()
            if current_run and current_run != target_run
            else ""
        )
        return self.yuku.publish_scienti_entities(
            run_name=target_run,
            comparison_name=comparison_name,
            project_audit_name=f"{self.run_name}_projects_semantic_audit",
            works_audit_name=f"{self.run_name}_works_semantic_audit",
            patents_audit_name=f"{self.run_name}_patents_semantic_audit",
            events_audit_name=f"{self.run_name}_events_semantic_audit",
            allow_snapshot_without_comparison=not comparison_name,
        )

    def _entity_comparison_name(self) -> str:
        return f"{self.run_name}_entity_compare"

    def _entities_compare(self) -> dict[str, Any]:
        target_run = f"{self.run_name}_entities"
        current = self.db["scienti_entity_publications"].find_one(
            {"_id": "current"}
        ) or {}
        current_run = str(current.get("current_run_name") or "")
        if not current_run:
            return {
                "status": "not_required_initial_snapshot",
                "new_run_name": target_run,
            }
        if current_run == target_run:
            return {
                "status": "already_current",
                "old_run_name": current_run,
                "new_run_name": target_run,
            }
        processing = self.config["processing"]
        result = self.yuku.compare_scienti_entity_versions(
            comparison_name=self._entity_comparison_name(),
            old_run_name=current_run,
            new_run_name=target_run,
            progress_every=int(processing["progress_every"]),
            batch_size=int(processing["semantic_audit_batch_size"]),
            example_limit=int(processing["semantic_audit_example_limit"]),
        )
        if result.get("status") != "passed" or int(
            result.get("critical_findings", 0) or 0
        ):
            raise RuntimeError(f"entity version comparison did not pass: {result}")
        return result

    def _measurements_normalize(self) -> dict[str, Any]:
        dataset = self.config["open_data"]["datasets"]["production"]
        return self.yuku.normalize_minciencias_measured_products(
            run_name=f"{self.run_name}_measurements_normalize",
            source_collection=dataset["data_collection"],
            destination_collection=self.names["measured_products"],
            batch_size=int(self.config["processing"]["measurement_batch_size"]),
            progress_every=int(self.config["processing"]["progress_every"]),
        )

    def _measurements_link(self, target_entity: str = "works") -> dict[str, Any]:
        graph_names = {
            "works": "base_graph",
            "projects": "projects_graph",
            "patents": "patents_graph",
            "events": "events_snapshot",
        }
        link_names = {
            "works": "measurement_links",
            "projects": "projects_measurement_links",
            "patents": "patents_measurement_links",
            "events": "events_measurement_links",
        }
        return self.yuku.link_minciencias_measured_products(
            run_name=f"{self.run_name}_{target_entity}_measurements_link",
            measured_collection=self.names["measured_products"],
            graph_collection=self.names[graph_names[target_entity]],
            links_collection=self.names[link_names[target_entity]],
            target_entity=target_entity,
            batch_size=int(self.config["processing"]["measurement_batch_size"]),
            progress_every=int(self.config["processing"]["progress_every"]),
        )

    def _final_measurement_entity(self, target_entity: str) -> dict[str, Any]:
        graph_names = {
            "works": "base_graph",
            "projects": "projects_graph",
            "patents": "patents_graph",
            "events": "events_snapshot",
        }
        link_names = {
            "works": "measurement_links",
            "projects": "projects_measurement_links",
            "patents": "patents_measurement_links",
            "events": "events_measurement_links",
        }
        final_names = {
            "works": "final_graph",
            "projects": "projects_final",
            "patents": "patents_final",
            "events": "events_final",
        }
        return self.yuku.materialize_minciencias_enriched_graph(
            run_name=f"{self.run_name}_{target_entity}_final",
            graph_collection=self.names[graph_names[target_entity]],
            measured_collection=self.names["measured_products"],
            links_collection=self.names[link_names[target_entity]],
            target_collection=self.names[final_names[target_entity]],
            target_entity=target_entity,
            batch_size=int(self.config["processing"]["measurement_batch_size"]),
            progress_every=int(self.config["processing"]["progress_every"]),
            publish_pointer=False,
        )

    def _publish_legacy_final_pointers(self, release: dict[str, Any]) -> None:
        """Expose legacy entity pointers only after the joint release passes."""
        published_at = int(
            release.get("published_at") or datetime.now(timezone.utc).timestamp()
        )
        final_names = {
            "works": "final_graph",
            "projects": "projects_final",
            "patents": "patents_final",
            "events": "events_final",
        }
        for entity, name_key in final_names.items():
            publication_name = (
                "scienti_work_graph_publications"
                if entity == "works"
                else "scienti_final_entity_publications"
            )
            current_id = "current" if entity == "works" else f"current_{entity}"
            publications = self.db[publication_name]
            current = publications.find_one({"_id": current_id}) or {}
            target = self.names[name_key]
            previous = str(current.get("current_collection") or "")
            if previous == target:
                previous = str(current.get("previous_collection") or "")
            publications.replace_one(
                {"_id": current_id},
                {
                    "_id": current_id,
                    "target_entity": entity,
                    "current_collection": target,
                    "current_run_name": f"{self.run_name}_{entity}_final",
                    "previous_collection": previous,
                    "published_at": published_at,
                },
                upsert=True,
            )

    def _final_graph(self) -> dict[str, Any]:
        return self._final_measurement_entity("works")

    def _bibliographic_audit(self) -> dict[str, Any]:
        audit_name = f"{self.run_name}_bibliographic_audit"
        result = self.yuku.audit_scienti_bibliographic_enrichment(
            audit_name=audit_name,
            run_name=self.run_name,
            base_collection=self.names["base_graph"],
            final_collection=self.names["final_graph"],
            materialization_run_name=f"{self.run_name}_works_final",
        )
        if result.get("status") != "passed" or int(
            result.get("critical_anomalies") or 0
        ):
            raise RuntimeError(f"critical bibliographic audit anomalies: {result}")
        return result

    def _final_release(self) -> dict[str, Any]:
        tag = self.config["snapshot_tag"]
        audit = self.db[BIBLIOGRAPHIC_AUDITS].find_one(
            {"_id": f"{self.run_name}_bibliographic_audit"}
        ) or {}
        if audit.get("status") != "passed" or int(
            audit.get("critical_anomalies") or 0
        ):
            raise RuntimeError("bibliographic enrichment audit has not passed")
        release = self.yuku.publish_scienti_final_release(
            release_name=f"release_{tag}",
            audit_name=f"release_{tag}_audit",
            collections={
                "works": self.names["final_graph"],
                "projects": self.names["projects_final"],
                "patents": self.names["patents_final"],
                "events": self.names["events_final"],
            },
            materialization_runs={
                entity: f"{self.run_name}_{entity}_final"
                for entity in ("works", "projects", "patents", "events")
            },
        )
        self._publish_legacy_final_pointers(release)
        return release

    def _cleanup(self) -> dict[str, Any]:
        if not self.config["processing"].get("cleanup_transient", True):
            return {"status": "skipped_by_configuration", "removed": []}
        derived = [
            self.names[key]
            for key in (
                "cvlac_normalized",
                "gruplac_normalized",
                "projects_graph",
                "patents_graph",
                "events_snapshot",
                "base_graph",
                "measured_products",
                "measurement_links",
                "projects_measurement_links",
                "patents_measurement_links",
                "events_measurement_links",
            )
        ]
        release_name = f"release_{self.config['snapshot_tag']}"
        publications = self.db["scienti_final_release_publications"]
        current = publications.find_one({"_id": "current"}) or {}
        if current.get("current_release") == release_name:
            previous_name = str(current.get("previous_release") or "")
            previous = publications.find_one({"_id": previous_name}) or {}
            current_finals = {
                self.names[key]
                for key in (
                    "final_graph",
                    "projects_final",
                    "patents_final",
                    "events_final",
                )
            }
            derived.extend(
                name
                for name in (previous.get("collections") or {}).values()
                if name and name not in current_finals
            )
        protected = [
            self.names[key]
            for key in (
                "all_researchers",
                "directory_pages",
                "recognized_researchers",
                "recognized_groups",
                "cvlac_seed_raw",
                "cvlac_raw",
                "gruplac_raw",
                "gruplac_downloads",
                "final_graph",
                "projects_final",
                "patents_final",
                "events_final",
            )
        ]
        entity_publications = self.db["scienti_entity_publications"]
        current_entities = entity_publications.find_one({"_id": "current"}) or {}
        current_entity_destinations = {
            str(name)
            for name in (current_entities.get("destinations") or {}).values()
            if str(name)
        }
        protected.extend(sorted(current_entity_destinations))
        previous_entity_run = str(current_entities.get("previous_run_name") or "")
        previous_entities = (
            entity_publications.find_one(
                {"_id": previous_entity_run, "record_type": "release"}
            )
            if previous_entity_run
            else {}
        ) or {}
        derived.extend(
            name
            for name in (previous_entities.get("destinations") or {}).values()
            if name and name not in current_entity_destinations
        )
        for dataset in self.config["open_data"]["datasets"].values():
            protected.extend(
                (dataset["data_collection"], dataset["metadata_collection"])
            )
        release_cleanup = self.yuku.cleanup_scienti_final_release(
            cleanup_name=f"cleanup_{self.config['snapshot_tag']}",
            release_name=release_name,
            candidates=derived,
            protected=protected,
            reset_entity_publication=False,
        )
        candidates = []
        verify_run = f"{self.run_name}_gruplac_verify"
        if self.config["processing"].get("verify_incomplete_gruplac", True):
            candidates.extend(
                f"{verify_run}_{suffix}"
                for suffix in ("targets", "raw", "downloads", "results")
            )
        candidates.extend(
            (
                f"{self.names['cvlac_normalized']}_errors",
                f"{self.names['gruplac_normalized']}_errors",
            )
        )
        removed = []
        retained_nonempty = []
        for name in candidates:
            if name not in self.db.list_collection_names():
                continue
            count = self.db[name].count_documents({})
            is_verification_artifact = name.startswith(f"{verify_run}_")
            if is_verification_artifact or count == 0:
                self.db[name].drop()
                removed.append(name)
            else:
                retained_nonempty.append({"collection": name, "count": count})
        return {
            "status": "complete",
            "release_cleanup": release_cleanup,
            "removed": removed,
            "retained_nonempty": retained_nonempty,
        }

    def run(
        self,
        *,
        from_stage: str = "",
        to_stage: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._prepare()
        if from_stage and from_stage not in STAGES:
            raise ValueError(f"unknown from_stage: {from_stage}")
        if to_stage and to_stage not in STAGES:
            raise ValueError(f"unknown to_stage: {to_stage}")
        start = STAGES.index(from_stage) if from_stage else 0
        stop = STAGES.index(to_stage) + 1 if to_stage else len(STAGES)
        if start >= stop:
            raise ValueError("from_stage must precede or equal to_stage")
        if dry_run:
            return {
                "run_name": self.run_name,
                "status": "validated",
                "config_hash": self.config_hash,
                "selected_stages": list(STAGES[start:stop]),
                "preflight": self.preflight(),
                "plan": self.plan(),
            }

        actions: dict[str, Callable[[], dict[str, Any]]] = {
            "open_data_researchers": lambda: self._open_data("researchers"),
            "open_data_groups": lambda: self._open_data("groups"),
            "open_data_production": lambda: self._open_data("production"),
            "convocations": self._convocations,
            "researcher_directory": self._directory,
            "resolve_researchers": self._resolve_researchers,
            "resolve_groups": self._resolve_groups,
            "gruplac_download": self._gruplac_download,
            "gruplac_normalize": self._gruplac_normalize,
            "gruplac_verify": self._gruplac_verify,
            "gruplac_audit": self._gruplac_audit,
            "cvlac_download": self._cvlac_download,
            "cvlac_normalize": self._cvlac_normalize,
            "cvlac_audit": self._cvlac_audit,
            "entities_normalize": self._entities_normalize,
            "projects_semantic_audit": self._projects_semantic_audit,
            "works_semantic_audit": lambda: self._auxiliary_semantic_audit("works"),
            "patents_semantic_audit": lambda: self._auxiliary_semantic_audit("patents"),
            "events_semantic_audit": lambda: self._auxiliary_semantic_audit("events"),
            "entities_compare": self._entities_compare,
            "entities_publish": self._entities_publish,
            "projects_graph": lambda: self._entity_graph("projects"),
            "patents_graph": lambda: self._entity_graph("patents"),
            "events_materialize": self._events_materialize,
            "base_graph": self._base_graph,
            "measurements_normalize": self._measurements_normalize,
            "measurements_link": self._measurements_link,
            "final_graph": self._final_graph,
            "projects_measurements_link": lambda: self._measurements_link("projects"),
            "projects_final": lambda: self._final_measurement_entity("projects"),
            "patents_measurements_link": lambda: self._measurements_link("patents"),
            "patents_final": lambda: self._final_measurement_entity("patents"),
            "events_measurements_link": lambda: self._measurements_link("events"),
            "events_final": lambda: self._final_measurement_entity("events"),
            "bibliographic_audit": self._bibliographic_audit,
            "final_release": self._final_release,
            "cleanup": self._cleanup,
        }
        self._acquire_lock()
        try:
            summaries = {}
            for stage in STAGES[start:stop]:
                summaries[stage] = self._run_stage(stage, actions[stage])
            complete = stop == len(STAGES)
            status = "complete" if complete else "partial_complete"
            self.runs.update_one(
                {"_id": self.run_name},
                {
                    "$set": {
                        "status": status,
                        "current_stage": None,
                        "finished_at" if complete else "last_progress_at": utc_now(),
                    }
                },
            )
            return {
                "run_name": self.run_name,
                "status": status,
                "selected_stages": list(STAGES[start:stop]),
                "summaries": summaries,
            }
        finally:
            self._release_lock()


def pipeline_status(db, run_name: str) -> dict[str, Any]:
    run = db[PIPELINE_RUNS].find_one({"_id": run_name})
    if not run:
        raise ValueError(f"pipeline {run_name!r} was not found")
    pipeline = object.__new__(ScientiFullPipeline)
    pipeline.db = db
    pipeline.runs = db[PIPELINE_RUNS]
    pipeline.run_name = run_name
    return {
        "run_name": run_name,
        "status": run.get("status"),
        "current_stage": run.get("current_stage"),
        "last_progress_at": run.get("last_progress_at"),
        "plan": [
            {
                "position": position,
                "stage": stage,
                "status": ((run.get("stages") or {}).get(stage) or {}).get(
                    "status", "pending"
                ),
                "summary": ((run.get("stages") or {}).get(stage) or {}).get(
                    "summary"
                ),
                "error": ((run.get("stages") or {}).get(stage) or {}).get("error"),
            }
            for position, stage in enumerate(STAGES, start=1)
        ],
    }
