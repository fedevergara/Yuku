"""Checkpointed, atomically published snapshots from Socrata datasets."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
from typing import Any, Iterable

from pymongo import ASCENDING, ReplaceOne


OPEN_DATA_RUNS = "scienti_open_data_runs"
COLLECTION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_name(value: str, label: str) -> None:
    if not value or not COLLECTION_RE.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")


def _metadata_fingerprint(metadata: dict[str, Any]) -> str:
    selected = {
        key: metadata.get(key)
        for key in (
            "id",
            "name",
            "rowsUpdatedAt",
            "viewLastModified",
            "publicationDate",
        )
    }
    return sha256(
        json.dumps(selected, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _expected_rows(metadata: dict[str, Any]) -> int:
    counts: list[int] = []
    for column in metadata.get("columns") or []:
        value = (column.get("cachedContents") or {}).get("count")
        try:
            counts.append(int(value))
        except (TypeError, ValueError):
            continue
    if not counts or max(counts) < 1:
        raise RuntimeError("Socrata metadata does not expose a positive row count")
    return max(counts)


def _bulk_replace(collection, documents: Iterable[dict[str, Any]]) -> None:
    documents = list(documents)
    operations = [
        ReplaceOne({"_id": document["_id"]}, document, upsert=True)
        for document in documents
    ]
    if operations:
        try:
            collection.bulk_write(operations, ordered=False)
        except (TypeError, NotImplementedError):
            # Compatibility with older mongomock releases used in tests.
            for document in documents:
                collection.replace_one(
                    {"_id": document["_id"]}, document, upsert=True
                )


class SocrataSnapshotDownloader:
    """Download a stable dataset snapshot without exposing partial data.

    Pages are written to a deterministic staging collection. Checkpoints are
    advanced only after the page is stored, so rerunning after a crash safely
    overwrites at most one page. The public collection is renamed only after
    exact row-count validation and index creation.
    """

    def __init__(self, db, client):
        self.db = db
        self.client = client
        self.runs = db[OPEN_DATA_RUNS]

    def _fetch_page(
        self,
        dataset_id: str,
        *,
        limit: int,
        offset: int,
        max_attempts: int = 5,
    ) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return list(
                    self.client.get(
                        dataset_id,
                        limit=limit,
                        offset=offset,
                        order=":id",
                    )
                )
            except Exception as error:  # pragma: no cover - network-specific
                last_error = error
                if attempt == max_attempts:
                    break
                time.sleep(min(30, 2 ** (attempt - 1)))
        assert last_error is not None
        raise last_error

    def download(
        self,
        *,
        run_name: str,
        dataset_id: str,
        data_collection: str,
        metadata_collection: str,
        index_fields: Iterable[str] = (),
        batch_size: int = 20000,
        refresh: bool = False,
    ) -> dict[str, Any]:
        for value, label in (
            (run_name, "run_name"),
            (dataset_id, "dataset_id"),
            (data_collection, "data_collection"),
            (metadata_collection, "metadata_collection"),
        ):
            _validate_name(value, label)
        if batch_size < 100 or batch_size > 50000:
            raise ValueError("Socrata batch_size must be between 100 and 50000")
        if data_collection == metadata_collection:
            raise ValueError("data and metadata collections must differ")

        metadata = deepcopy(self.client.get_metadata(dataset_id))
        if str(metadata.get("id") or "") != dataset_id:
            raise RuntimeError("Socrata returned metadata for a different dataset")
        expected = _expected_rows(metadata)
        fingerprint = _metadata_fingerprint(metadata)
        fields = tuple(dict.fromkeys(str(value) for value in index_fields if value))
        config = {
            "dataset_id": dataset_id,
            "data_collection": data_collection,
            "metadata_collection": metadata_collection,
            "batch_size": batch_size,
            "index_fields": list(fields),
            "metadata_fingerprint": fingerprint,
            "expected_rows": expected,
            "refresh": bool(refresh),
        }
        run = self.runs.find_one({"_id": run_name})
        if run and run.get("config") != config:
            raise ValueError(
                f"open-data run {run_name!r} already exists with different inputs"
            )
        if run and run.get("status") == "complete":
            actual = self.db[data_collection].count_documents({})
            if actual != expected:
                raise RuntimeError(
                    f"completed snapshot {data_collection!r} has {actual} rows, "
                    f"expected {expected}"
                )
            return run.get("summary") or {}

        names = set(self.db.list_collection_names())
        if data_collection in names and not run:
            old_metadata = self.db[metadata_collection].find_one({}) or {}
            same_snapshot = (
                str(old_metadata.get("id") or "") == dataset_id
                and _metadata_fingerprint(old_metadata) == fingerprint
                and self.db[data_collection].count_documents({}) == expected
            )
            if same_snapshot and not refresh:
                summary = {
                    "run_name": run_name,
                    "status": "complete",
                    "dataset_id": dataset_id,
                    "collection": data_collection,
                    "rows": expected,
                    "adopted_existing": True,
                }
                self.runs.insert_one(
                    {
                        "_id": run_name,
                        "kind": "socrata_snapshot",
                        "status": "complete",
                        "config": config,
                        "summary": summary,
                        "created_at": utc_now(),
                        "finished_at": utc_now(),
                    }
                )
                return summary
            if not refresh:
                raise ValueError(
                    f"{data_collection!r} already exists but is not the requested "
                    "snapshot; enable refresh to publish a replacement"
                )

        staging = f"__yuku_{data_collection}_{run_name}_build"
        metadata_staging = f"__yuku_{metadata_collection}_{run_name}_build"
        if not run:
            self.runs.insert_one(
                {
                    "_id": run_name,
                    "kind": "socrata_snapshot",
                    "status": "created",
                    "config": config,
                    "staging_collection": staging,
                    "metadata_staging_collection": metadata_staging,
                    "offset": 0,
                    "created_at": utc_now(),
                }
            )
            run = self.runs.find_one({"_id": run_name}) or {}
        elif str(run.get("staging_collection") or "") != staging:
            raise RuntimeError("open-data staging collection does not match its run")

        if run.get("data_published_at"):
            return self._finish_publication(
                run_name=run_name,
                data_collection=data_collection,
                metadata_collection=metadata_collection,
                metadata_staging=metadata_staging,
                expected=expected,
                dataset_id=dataset_id,
            )

        offset = int(run.get("offset") or 0)
        if offset and staging not in self.db.list_collection_names():
            raise RuntimeError("checkpoint exists but Socrata staging data is missing")
        self.runs.update_one(
            {"_id": run_name},
            {
                "$set": {"status": "running", "last_started_at": utc_now()},
                "$inc": {"execution_attempts": 1},
                "$unset": {"error": ""},
            },
        )
        destination = self.db[staging]
        try:
            while offset < expected:
                rows = self._fetch_page(
                    dataset_id,
                    limit=min(batch_size, expected - offset),
                    offset=offset,
                )
                if not rows:
                    raise RuntimeError(
                        f"Socrata returned no rows at offset {offset}/{expected}"
                    )
                documents = []
                for position, row in enumerate(rows, start=offset):
                    document = dict(row)
                    document["_id"] = "{}:{:010d}".format(dataset_id, position)
                    documents.append(document)
                _bulk_replace(destination, documents)
                offset += len(rows)
                self.runs.update_one(
                    {"_id": run_name},
                    {
                        "$set": {
                            "offset": offset,
                            "last_checkpoint_at": utc_now(),
                        }
                    },
                )
                print(
                    f"{utc_now().isoformat()} INFO: Socrata snapshot "
                    f"run={run_name} rows={offset}/{expected}",
                    flush=True,
                )
                if len(rows) < min(batch_size, expected - (offset - len(rows))):
                    break

            actual = destination.count_documents({})
            if actual != expected or offset != expected:
                raise RuntimeError(
                    f"Socrata snapshot audit failed: stored={actual}, "
                    f"checkpoint={offset}, expected={expected}"
                )
            for field in fields:
                destination.create_index([(field, ASCENDING)])
            metadata["_id"] = dataset_id
            self.db[metadata_staging].replace_one(
                {"_id": dataset_id}, metadata, upsert=True
            )
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"rename_started_at": utc_now()}},
            )
            destination.rename(data_collection, dropTarget=refresh)
            self.runs.update_one(
                {"_id": run_name},
                {"$set": {"data_published_at": utc_now()}},
            )
            return self._finish_publication(
                run_name=run_name,
                data_collection=data_collection,
                metadata_collection=metadata_collection,
                metadata_staging=metadata_staging,
                expected=expected,
                dataset_id=dataset_id,
            )
        except Exception as error:
            self.runs.update_one(
                {"_id": run_name},
                {
                    "$set": {
                        "status": "failed",
                        "failed_at": utc_now(),
                        "error": f"{type(error).__name__}: {error}",
                    }
                },
            )
            raise

    def _finish_publication(
        self,
        *,
        run_name: str,
        data_collection: str,
        metadata_collection: str,
        metadata_staging: str,
        expected: int,
        dataset_id: str,
    ) -> dict[str, Any]:
        if self.db[data_collection].count_documents({}) != expected:
            raise RuntimeError("published Socrata data failed its final row audit")
        if metadata_staging in self.db.list_collection_names():
            self.db[metadata_staging].rename(metadata_collection, dropTarget=True)
        metadata = self.db[metadata_collection].find_one({}) or {}
        if str(metadata.get("id") or "") != dataset_id:
            raise RuntimeError("published Socrata metadata does not match the data")
        summary = {
            "run_name": run_name,
            "status": "complete",
            "dataset_id": dataset_id,
            "collection": data_collection,
            "metadata_collection": metadata_collection,
            "rows": expected,
            "critical_anomalies": 0,
        }
        self.runs.update_one(
            {"_id": run_name},
            {
                "$set": {
                    "status": "complete",
                    "finished_at": utc_now(),
                    "summary": summary,
                }
            },
        )
        return summary
