"""Atomic publication of audited Scienti entities that do not need a graph."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any

from yuku.scienti_entities import ENTITY_AUDITS, ENTITY_RUNS


MATERIALIZATION_RUNS = "scienti_entity_materialization_runs"
MATERIALIZATION_VERSION = "scienti-entity-materialization-v1"
RUN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
COLLECTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,119}")


class ScientiEntitySnapshotMaterializer:
    """Copy one immutable audited entity collection into an atomic final snapshot."""

    def __init__(
        self,
        db,
        *,
        entity: str,
        run_name: str,
        source_collection: str,
        target_collection: str,
        entity_run_name: str,
    ):
        if entity != "events":
            raise ValueError("snapshot materialization currently supports events")
        if not RUN_NAME_RE.fullmatch(run_name or ""):
            raise ValueError("invalid materialization run name")
        for value in (source_collection, target_collection):
            if not COLLECTION_RE.fullmatch(value or "") or value.startswith("system."):
                raise ValueError("invalid materialization collection name")
        if source_collection == target_collection:
            raise ValueError("materialization source and target must differ")
        self.db = db
        self.entity = entity
        self.run_name = run_name
        self.source_collection = source_collection
        self.target_collection = target_collection
        self.entity_run_name = entity_run_name
        self.temp_collection = f"__yuku_{run_name}_{entity}_materialize"
        self.runs = db[MATERIALIZATION_RUNS]
        self.config = {
            "materialization_version": MATERIALIZATION_VERSION,
            "entity": entity,
            "entity_run_name": entity_run_name,
            "source_collection": source_collection,
            "target_collection": target_collection,
        }
        self.config_hash = sha256(
            json.dumps(self.config, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _validate_source(self) -> int:
        run = self.db[ENTITY_RUNS].find_one({"_id": self.entity_run_name}) or {}
        destinations = (run.get("config") or {}).get("destinations") or {}
        if (
            run.get("status") != "complete"
            or destinations.get(self.entity) != self.source_collection
        ):
            raise RuntimeError(
                "materialization source is not a completed entity destination"
            )
        audit = self.db[ENTITY_AUDITS].find_one(
            {"_id": self.entity_run_name}
        ) or {}
        if audit.get("status") != "passed" or int(
            audit.get("critical_anomalies", 0) or 0
        ):
            raise RuntimeError("materialization source did not pass its audit")
        publication = self.db["scienti_entity_publications"].find_one(
            {"_id": self.entity_run_name}
        ) or {}
        if publication.get("status") != "current":
            raise RuntimeError("materialization source is not the current publication")
        count = self.db[self.source_collection].count_documents({})
        if count < 1:
            raise RuntimeError("materialization source is empty")
        return count

    def _prepare(self, source_count: int) -> dict[str, Any]:
        previous = self.runs.find_one({"_id": self.run_name})
        if previous and previous.get("config_hash") != self.config_hash:
            raise ValueError("materialization run exists with another configuration")
        if previous and previous.get("status") == "complete":
            if self.target_collection not in self.db.list_collection_names():
                raise RuntimeError("completed materialization target is missing")
            if self.db[self.target_collection].count_documents({}) != source_count:
                raise RuntimeError("completed materialization count changed")
            return previous
        if self.target_collection in self.db.list_collection_names():
            raise RuntimeError("materialization target already exists")
        self.db[self.temp_collection].drop()
        document = {
            "_id": self.run_name,
            "status": "pending",
            "config": deepcopy(self.config),
            "config_hash": self.config_hash,
            "source_count": source_count,
            "created_at": datetime.now(timezone.utc),
        }
        self.runs.replace_one({"_id": self.run_name}, document, upsert=True)
        return document

    def _copy_indexes(self) -> None:
        source = self.db[self.source_collection]
        target = self.db[self.temp_collection]
        for name, specification in source.index_information().items():
            if name == "_id_":
                continue
            options = {
                key: deepcopy(specification[key])
                for key in ("unique", "sparse", "partialFilterExpression")
                if key in specification
            }
            target.create_index(
                specification["key"], name=name, **options
            )

    def _missing_identifier(self, left: str, right: str) -> Any:
        pipeline = [
            {
                "$lookup": {
                    "from": right,
                    "localField": "_id",
                    "foreignField": "_id",
                    "as": "_copy",
                }
            },
            {"$match": {"_copy": {"$eq": []}}},
            {"$limit": 1},
            {"$project": {"_id": 1}},
        ]
        values = list(self.db[left].aggregate(pipeline, allowDiskUse=True))
        return values[0].get("_id") if values else None

    def run(self) -> dict[str, Any]:
        source_count = self._validate_source()
        previous = self._prepare(source_count)
        if previous.get("status") == "complete":
            return deepcopy(previous.get("summary") or {})
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {
                "status": "running",
                "started_at": datetime.now(timezone.utc),
            }},
        )
        try:
            list(self.db[self.source_collection].aggregate(
                [{"$match": {}}, {"$out": self.temp_collection}],
                allowDiskUse=True,
            ))
            copied = self.db[self.temp_collection].count_documents({})
            if copied != source_count:
                raise RuntimeError(
                    f"materialization count mismatch: {copied}/{source_count}"
                )
            missing = self._missing_identifier(
                self.source_collection, self.temp_collection
            )
            extra = self._missing_identifier(
                self.temp_collection, self.source_collection
            )
            if missing is not None or extra is not None:
                raise RuntimeError(
                    f"materialization identity mismatch: missing={missing} extra={extra}"
                )
            self._copy_indexes()
            self.db[self.temp_collection].rename(
                self.target_collection, dropTarget=False
            )
            summary = {
                "status": "complete",
                "entity": self.entity,
                "source_collection": self.source_collection,
                "collection": self.target_collection,
                "documents": source_count,
                "identity_audit": "passed",
                "finished_at": datetime.now(timezone.utc),
            }
            self.runs.update_one(
                {"_id": self.run_name},
                {"$set": {
                    "status": "complete",
                    "summary": summary,
                    "finished_at": summary["finished_at"],
                }},
            )
            stored = self.runs.find_one({"_id": self.run_name}) or {}
            return deepcopy(stored.get("summary") or summary)
        except Exception as error:
            self.runs.update_one(
                {"_id": self.run_name},
                {"$set": {
                    "status": "failed",
                    "failed_at": datetime.now(timezone.utc),
                    "error": f"{type(error).__name__}: {error}",
                }},
            )
            raise
