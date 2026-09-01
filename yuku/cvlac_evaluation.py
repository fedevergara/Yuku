from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import re

from pymongo import ASCENDING

from yuku.scienti_profiles import ScientiProfileDownloader


PRIORITY_SOURCES = (
    ("recognized_researchers", "cod_rh"),
    ("gruplac_production_data", "id_persona_pd"),
    ("cvlac_data", "id_persona_pr"),
    ("recognized_groups", "cod_rh_lider"),
    ("gruplac_related_works", "members.cod_rh"),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_cod_rh(value) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    value = value.zfill(10)
    return value if len(value) == 10 and value.isdigit() else ""


def html_sha256(html: str) -> str:
    return sha256((html or "").encode("utf-8")).hexdigest()


def stable_sample(values, size: int, seed: str) -> list[str]:
    values = set(values)
    ordered = sorted(
        values,
        key=lambda value: sha256("{}:{}".format(seed, value).encode("utf-8")).digest(),
    )
    return ordered[: max(0, size)]


class CvlacProfileEvaluation:
    """Create and run an isolated, deterministic CVLAC evaluation cohort."""

    RUNS_COLLECTION = "scienti_cvlac_evaluation_runs"

    def __init__(
        self,
        db,
        *,
        run_name: str,
        reference_collection: str = "cvlac_stage_raw",
        new_count: int = 500,
        refresh_count: int = 500,
        seed: str = "yuku-cvlac-evaluation-v1",
        workers: int = 4,
        requests_per_second: float = 2.0,
    ):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", run_name or ""):
            raise ValueError("run_name must contain only letters, numbers and underscores")
        if new_count < 0 or refresh_count < 0 or new_count + refresh_count < 1:
            raise ValueError("the evaluation cohort must contain at least one profile")
        self.db = db
        self.run_name = run_name
        self.reference_collection = reference_collection
        self.new_count = new_count
        self.refresh_count = refresh_count
        self.seed = seed
        self.workers = workers
        self.requests_per_second = requests_per_second
        self.cohort_collection = f"{run_name}_cohort"
        self.raw_collection = f"{run_name}_raw"
        self.state_collection = f"{run_name}_downloads"
        self.works_collection = f"{run_name}_works"
        self.runs = db[self.RUNS_COLLECTION]

    @property
    def config(self) -> dict:
        return {
            "reference_collection": self.reference_collection,
            "new_count": self.new_count,
            "refresh_count": self.refresh_count,
            "seed": self.seed,
            "cohort_collection": self.cohort_collection,
            "raw_collection": self.raw_collection,
            "state_collection": self.state_collection,
            "works_collection": self.works_collection,
        }

    def _check_or_create_run(self) -> None:
        existing = self.runs.find_one({"_id": self.run_name})
        if existing and existing.get("config") != self.config:
            raise ValueError(
                f"evaluation {self.run_name!r} already exists with a different config"
            )
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$setOnInsert": {
                    "created_at": utc_now(),
                    "config": self.config,
                },
                "$set": {
                    "status": "preparing",
                    "last_started_at": utc_now(),
                },
                "$inc": {"execution_attempts": 1},
            },
            upsert=True,
        )

    def _priority_index(self) -> dict[str, set[str]]:
        available = set(self.db.list_collection_names())
        sources: dict[str, set[str]] = {}
        for collection, field in PRIORITY_SOURCES:
            if collection not in available:
                continue
            label = f"{collection}.{field}"
            for value in self.db[collection].distinct(field):
                code = normalize_cod_rh(value)
                if code:
                    sources.setdefault(code, set()).add(label)
        return sources

    def prepare_cohort(self) -> list[dict]:
        self._check_or_create_run()
        if self.reference_collection not in self.db.list_collection_names():
            raise ValueError(
                f"reference collection {self.reference_collection!r} was not found"
            )

        expected = self.new_count + self.refresh_count
        cohort = self.db[self.cohort_collection]
        existing_cohort = list(
            cohort.find({"run_name": self.run_name}).sort("rank", ASCENDING)
        )
        if len(existing_cohort) == expected:
            segment_counts = Counter(item.get("segment") for item in existing_cohort)
            if segment_counts == Counter(
                {"new": self.new_count, "refresh": self.refresh_count}
            ):
                self.runs.update_one(
                    {"_id": self.run_name},
                    {
                        "$set": {
                            "status": "cohort_ready",
                            "cohort_size": expected,
                        }
                    },
                )
                return existing_cohort

        sources = self._priority_index()
        reference_ids = {
            normalize_cod_rh(value)
            for value in self.db[self.reference_collection].distinct("_id")
        }
        reference_ids.discard("")
        comparable_ids = {
            normalize_cod_rh(value)
            for value in self.db[self.reference_collection].distinct(
                "_id", {"html": {"$type": "string", "$ne": ""}}
            )
        }
        comparable_ids.discard("")
        priority_ids = set(sources)
        missing = priority_ids - reference_ids
        refreshable = priority_ids & comparable_ids
        if len(missing) < self.new_count:
            raise ValueError(
                f"requested {self.new_count} new profiles but only {len(missing)} are available"
            )
        if len(refreshable) < self.refresh_count:
            raise ValueError(
                f"requested {self.refresh_count} refresh profiles but only "
                f"{len(refreshable)} are available"
            )

        selected = [
            ("new", value)
            for value in stable_sample(missing, self.new_count, "{}:new".format(self.seed))
        ] + [
            ("refresh", value)
            for value in stable_sample(
                refreshable, self.refresh_count, "{}:refresh".format(self.seed)
            )
        ]
        for rank, (segment, code) in enumerate(selected, start=1):
            cohort.update_one(
                {"_id": code},
                {
                    "$setOnInsert": {"selected_at": utc_now()},
                    "$set": {
                        "run_name": self.run_name,
                        "segment": segment,
                        "rank": rank,
                        "sources": sorted(sources[code]),
                    },
                },
                upsert=True,
            )
        cohort.create_index([("segment", ASCENDING), ("rank", ASCENDING)])
        actual = cohort.count_documents({"run_name": self.run_name})
        if actual != expected:
            raise RuntimeError(f"cohort has {actual} records, expected {expected}")

        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$set": {
                    "status": "cohort_ready",
                    "priority_universe": len(priority_ids),
                    "available_new": len(missing),
                    "available_refresh": len(refreshable),
                    "cohort_size": expected,
                }
            },
        )
        return list(cohort.find({"run_name": self.run_name}).sort("rank", ASCENDING))

    def download(self, cohort: list[dict]) -> dict:
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"status": "downloading", "download_started_at": utc_now()}},
        )
        downloader = ScientiProfileDownloader(
            self.db,
            workers=self.workers,
            requests_per_second=self.requests_per_second,
            state_collection=self.state_collection,
        )
        counts = downloader.download(
            "cvlac",
            downloader.cvlac_targets(item["_id"] for item in cohort),
            raw_collection=self.raw_collection,
            progress_every=25,
        )
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"download_counts": counts, "download_finished_at": utc_now()}},
        )
        return counts

    def summarize_download(self, cohort: list[dict]) -> dict:
        counters = Counter()
        cohort_collection = self.db[self.cohort_collection]
        raw_collection = self.db[self.raw_collection]
        reference_collection = self.db[self.reference_collection]
        state_collection = self.db[self.state_collection]

        for item in cohort:
            code = item["_id"]
            segment = item["segment"]
            raw = raw_collection.find_one({"_id": code})
            state = state_collection.find_one({"_id": "cvlac:{}".format(code)}) or {}
            status = state.get("status", "not_attempted")
            counters[f"{segment}_{status}"] += 1
            result = {
                "status": status,
                "http_status": state.get("http_status"),
                "attempts": state.get("attempts", 0),
                "evaluated_at": utc_now(),
            }
            if raw:
                html = raw.get("html", "")
                result.update(
                    {
                        "fetched_at": raw.get("fetched_at"),
                        "content_sha256": raw.get("content_sha256") or html_sha256(html),
                        "html_bytes": len(html.encode("utf-8")),
                    }
                )
                counters[f"{segment}_raw_saved"] += 1
            if segment == "refresh" and raw:
                reference = reference_collection.find_one({"_id": code}, {"html": 1})
                reference_html = (reference or {}).get("html", "")
                reference_hash = html_sha256(reference_html)
                changed = reference_hash != result["content_sha256"]
                result.update(
                    {
                        "reference_content_sha256": reference_hash,
                        "content_changed": changed,
                    }
                )
                counters["refresh_changed" if changed else "refresh_unchanged"] += 1
            cohort_collection.update_one({"_id": code}, {"$set": {"result": result}})

        summary = dict(sorted(counters.items()))
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"status": "downloaded", "download_summary": summary}},
        )
        return summary

    def complete(self, graph_summary: dict) -> None:
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$set": {
                    "status": "complete",
                    "graph_summary": graph_summary,
                    "finished_at": utc_now(),
                },
                "$unset": {"error": ""},
            },
        )

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
