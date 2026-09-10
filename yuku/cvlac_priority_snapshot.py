from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import re

from pymongo import ASCENDING

from yuku.cvlac_evaluation import PRIORITY_SOURCES, normalize_cod_rh
from yuku.scienti_profiles import ScientiProfileDownloader


FULL_SOURCES = PRIORITY_SOURCES + (("all_researchers", "cod_rh"),)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CvlacPrioritySnapshot:
    """Freeze and download the strong Scienti CVLAC identifier universe."""

    RUNS_COLLECTION = "scienti_cvlac_priority_runs"

    def __init__(
        self,
        db,
        *,
        run_name: str,
        reference_collection: str,
        raw_collection: str,
        workers: int = 4,
        requests_per_second: float = 2.0,
        max_passes: int = 3,
        identifier_sources=PRIORITY_SOURCES,
        copy_reference: bool = False,
        fallback_requests_per_second: float | None = None,
        fallback_after_errors: int = 5,
    ):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", run_name or ""):
            raise ValueError("run_name must contain only letters, numbers and underscores")
        if raw_collection == reference_collection:
            raise ValueError("raw_collection must differ from reference_collection")
        if max_passes < 1:
            raise ValueError("max_passes must be positive")
        self.db = db
        self.run_name = run_name
        self.reference_collection = reference_collection
        self.raw_collection = raw_collection
        self.workers = workers
        self.requests_per_second = requests_per_second
        self.max_passes = max_passes
        self.identifier_sources = tuple(identifier_sources)
        self.copy_reference = copy_reference
        self.fallback_requests_per_second = fallback_requests_per_second
        self.fallback_after_errors = fallback_after_errors
        self.manifest_collection = f"{run_name}_targets"
        self.state_collection = f"{run_name}_downloads"
        self.runs = db[self.RUNS_COLLECTION]

    @property
    def config(self) -> dict:
        config = {
            "reference_collection": self.reference_collection,
            "raw_collection": self.raw_collection,
            "manifest_collection": self.manifest_collection,
            "state_collection": self.state_collection,
            "priority_sources": [list(item) for item in self.identifier_sources],
            "workers": self.workers,
            "requests_per_second": self.requests_per_second,
            "max_passes": self.max_passes,
        }
        # Preserve the configuration shape of priority runs created before
        # complete-directory snapshots were introduced.
        if self.copy_reference:
            config["copy_reference"] = True
        if self.fallback_requests_per_second:
            config["fallback_requests_per_second"] = self.fallback_requests_per_second
            config["fallback_after_errors"] = self.fallback_after_errors
        return config

    @staticmethod
    def _immutable_config(config: dict) -> dict:
        keys = {
            "reference_collection",
            "raw_collection",
            "manifest_collection",
            "state_collection",
            "priority_sources",
            "copy_reference",
        }
        return {key: config[key] for key in keys if key in config}

    def _priority_index(self) -> dict[str, set[str]]:
        available = set(self.db.list_collection_names())
        sources: dict[str, set[str]] = {}
        for collection, field in self.identifier_sources:
            if collection not in available:
                continue
            label = f"{collection}.{field}"
            for value in self.db[collection].distinct(field):
                code = normalize_cod_rh(value)
                if code:
                    sources.setdefault(code, set()).add(label)
        return sources

    def _check_config(self) -> dict | None:
        run = self.runs.find_one({"_id": self.run_name})
        if run and self._immutable_config(run.get("config") or {}) != self._immutable_config(
            self.config
        ):
            raise ValueError(
                f"priority run {self.run_name!r} already exists with a different config"
            )
        if run and run.get("config") != self.config:
            self.runs.update_one(
                {"_id": self.run_name},
                {
                    "$set": {"config": self.config, "runtime_config_updated_at": utc_now()},
                    "$push": {
                        "runtime_config_history": {
                            "changed_at": utc_now(),
                            "workers": self.workers,
                            "requests_per_second": self.requests_per_second,
                            "max_passes": self.max_passes,
                            "fallback_requests_per_second": self.fallback_requests_per_second,
                            "fallback_after_errors": self.fallback_after_errors,
                        }
                    },
                },
            )
            run = self.runs.find_one({"_id": self.run_name})
        return run

    def prepare_manifest(self) -> dict:
        run = self._check_config()
        if run and run.get("manifest_status") == "ready":
            expected = int(run["target_count"])
            actual = self.db[self.manifest_collection].count_documents(
                {"run_name": self.run_name}
            )
            if actual != expected:
                raise RuntimeError(
                    f"frozen manifest has {actual} records, expected {expected}"
                )
            return {
                "target_count": expected,
                "segment_counts": run["segment_counts"],
                "manifest_sha256": run["manifest_sha256"],
                "reused": True,
            }

        if self.reference_collection not in self.db.list_collection_names():
            raise ValueError(
                f"reference collection {self.reference_collection!r} was not found"
            )
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$setOnInsert": {"created_at": utc_now(), "config": self.config},
                "$set": {"manifest_status": "preparing", "updated_at": utc_now()},
            },
            upsert=True,
        )

        sources = self._priority_index()
        if not sources:
            raise RuntimeError("no strong priority COD_RH identifiers were found")
        reference_ids = {
            normalize_cod_rh(value)
            for value in self.db[self.reference_collection].distinct("_id")
        }
        reference_ids.discard("")

        manifest = self.db[self.manifest_collection]
        # This is safe only while the run has not reached the immutable
        # manifest_ready state. It lets a process killed during preparation
        # reconstruct a complete manifest rather than retaining a partial one.
        manifest.delete_many({"run_name": self.run_name})
        digest = sha256()
        counts = Counter()
        batch = []
        for rank, code in enumerate(sorted(sources), start=1):
            segment = "refresh" if code in reference_ids else "new"
            labels = sorted(sources[code])
            digest.update(f"{code}|{segment}|{','.join(labels)}\n".encode("utf-8"))
            counts[segment] += 1
            batch.append(
                {
                    "_id": code,
                    "run_name": self.run_name,
                    "rank": rank,
                    "segment": segment,
                    "sources": labels,
                    "selected_at": utc_now(),
                }
            )
            if len(batch) == 1000:
                manifest.insert_many(batch, ordered=True)
                batch = []
        if batch:
            manifest.insert_many(batch, ordered=True)
        manifest.create_index([("run_name", ASCENDING), ("rank", ASCENDING)])
        target_count = sum(counts.values())
        actual = manifest.count_documents({"run_name": self.run_name})
        if actual != target_count:
            raise RuntimeError(f"manifest has {actual} records, expected {target_count}")

        result = {
            "target_count": target_count,
            "segment_counts": dict(sorted(counts.items())),
            "manifest_sha256": digest.hexdigest(),
            "reused": False,
        }
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$set": {
                    "manifest_status": "ready",
                    "target_count": result["target_count"],
                    "segment_counts": result["segment_counts"],
                    "manifest_sha256": result["manifest_sha256"],
                    "manifest_finished_at": utc_now(),
                    "status": "ready",
                }
            },
        )
        return result

    def _target_codes(self):
        cursor = self.db[self.manifest_collection].find(
            {"run_name": self.run_name}, {"_id": 1}
        ).sort("rank", ASCENDING)
        for item in cursor:
            yield item["_id"]

    def copy_reference_snapshot(self) -> dict:
        """Seed the versioned destination without transferring HTML through Python."""
        if not self.copy_reference:
            return {"enabled": False, "source_count": 0, "destination_count": 0}
        source = self.db[self.reference_collection]
        destination = self.db[self.raw_collection]
        source_count = source.count_documents({})
        list(
            source.aggregate(
                [
                    {
                        "$merge": {
                            "into": self.raw_collection,
                            "on": "_id",
                            "whenMatched": "keepExisting",
                            "whenNotMatched": "insert",
                        }
                    }
                ],
                allowDiskUse=True,
            )
        )
        destination_count = destination.count_documents({})
        if destination_count < source_count:
            raise RuntimeError(
                f"destination has {destination_count} records after copying "
                f"{source_count} reference records"
            )
        result = {
            "enabled": True,
            "source_count": source_count,
            "destination_count": destination_count,
            "copied_at": utc_now(),
        }
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"seed_copy": result, "last_progress_at": utc_now()}},
        )
        print(
            f"{utc_now().isoformat()} INFO: reference snapshot copied idempotently, "
            f"source={self.reference_collection} source_count={source_count} "
            f"destination={self.raw_collection} destination_count={destination_count}",
            flush=True,
        )
        return result

    def _summary(self) -> dict:
        target_count = self.db[self.manifest_collection].count_documents(
            {"run_name": self.run_name}
        )
        raw_count = self.db[self.raw_collection].count_documents({})
        status_counts = {
            item["_id"]: item["count"]
            for item in self.db[self.state_collection].aggregate(
                [
                    {"$match": {"kind": "cvlac"}},
                    {"$group": {"_id": "$status", "count": {"$sum": 1}}},
                ]
            )
        }
        return {
            "target_count": target_count,
            "raw_count": raw_count,
            "missing_raw": max(0, target_count - raw_count),
            "status_counts": dict(sorted(status_counts.items())),
        }

    def run(self) -> dict:
        manifest = self.prepare_manifest()
        self.copy_reference_snapshot()
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$set": {"status": "downloading", "last_started_at": utc_now()},
                "$inc": {"execution_attempts": 1},
                "$unset": {"error": ""},
            },
        )
        print(
            f"{utc_now().isoformat()} INFO: frozen priority manifest ready, "
            f"run={self.run_name} targets={manifest['target_count']} "
            f"segments={manifest['segment_counts']} sha256={manifest['manifest_sha256']} "
            f"raw_collection={self.raw_collection}",
            flush=True,
        )
        downloader = ScientiProfileDownloader(
            self.db,
            workers=self.workers,
            requests_per_second=self.requests_per_second,
            state_collection=self.state_collection,
        )
        passes = []
        for pass_number in range(1, self.max_passes + 1):
            before = self._summary()
            if before["missing_raw"] == 0:
                break
            print(
                f"{utc_now().isoformat()} INFO: starting priority pass "
                f"{pass_number}/{self.max_passes}, missing_raw={before['missing_raw']}",
                flush=True,
            )
            counts = downloader.download(
                "cvlac",
                downloader.cvlac_targets(self._target_codes()),
                raw_collection=self.raw_collection,
                progress_every=100,
                fallback_requests_per_second=self.fallback_requests_per_second,
                fallback_after_errors=self.fallback_after_errors,
            )
            after = self._summary()
            passes.append({"pass": pass_number, "counts": counts, "summary": after})
            self.runs.update_one(
                {"_id": self.run_name},
                {"$set": {"passes": passes, "last_progress_at": utc_now()}},
            )

        summary = self._summary()
        status = "complete" if summary["missing_raw"] == 0 else "complete_with_errors"
        result = {"run_name": self.run_name, "status": status, **summary}
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$set": {
                    "status": status,
                    "summary": summary,
                    "passes": passes,
                    "finished_at": utc_now(),
                }
            },
        )
        print(
            f"{utc_now().isoformat()} INFO: priority run finished, {result}",
            flush=True,
        )
        return result

    def fail(self, error: Exception) -> None:
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$set": {
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                    "failed_at": utc_now(),
                }
            },
            upsert=True,
        )
