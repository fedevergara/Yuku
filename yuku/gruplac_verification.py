from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import re

from pymongo import ASCENDING

from yuku.gruplac_related_works import normalize_gruplac_document
from yuku.scienti_profiles import ScientiProfileDownloader


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GruplacIncompleteVerifier:
    """Recheck frozen incomplete GrupLAC pages without replacing valid evidence."""

    RUNS_COLLECTION = "scienti_gruplac_verification_runs"

    def __init__(
        self,
        db,
        *,
        run_name: str,
        source_raw_collection: str,
        source_state_collection: str,
        normalized_collection: str,
        workers: int = 1,
        requests_per_second: float = 0.25,
        max_passes: int = 2,
    ):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", run_name or ""):
            raise ValueError("run_name must contain only letters, numbers and underscores")
        self.db = db
        self.run_name = run_name
        self.source_raw_collection = source_raw_collection
        self.source_state_collection = source_state_collection
        self.normalized_collection = normalized_collection
        self.workers = workers
        self.requests_per_second = requests_per_second
        self.max_passes = max_passes
        self.target_collection = f"{run_name}_targets"
        self.raw_collection = f"{run_name}_raw"
        self.state_collection = f"{run_name}_downloads"
        self.result_collection = f"{run_name}_results"
        self.runs = db[self.RUNS_COLLECTION]

    @property
    def config(self) -> dict:
        return {
            "source_raw_collection": self.source_raw_collection,
            "source_state_collection": self.source_state_collection,
            "normalized_collection": self.normalized_collection,
            "target_collection": self.target_collection,
            "raw_collection": self.raw_collection,
            "state_collection": self.state_collection,
            "result_collection": self.result_collection,
            "workers": self.workers,
            "requests_per_second": self.requests_per_second,
            "max_passes": self.max_passes,
        }

    def prepare_targets(self) -> list[dict]:
        existing_run = self.runs.find_one({"_id": self.run_name})
        if existing_run and existing_run.get("config") != self.config:
            raise ValueError(
                f"verification {self.run_name!r} already exists with a different config"
            )
        if existing_run and existing_run.get("target_status") == "ready":
            expected = int(existing_run.get("target_count") or 0)
            targets = list(self.db[self.target_collection].find({}).sort("rank", ASCENDING))
            if len(targets) != expected:
                raise RuntimeError(
                    f"frozen verification target has {len(targets)} records, expected {expected}"
                )
            return targets

        available = set(self.db.list_collection_names())
        required = {self.source_raw_collection, self.source_state_collection}
        missing = sorted(required - available)
        if missing:
            raise ValueError(f"required GrupLAC collections are missing: {missing}")
        states = list(
            self.db[self.source_state_collection].find(
                {"kind": "gruplac", "status": "incomplete"},
                {"source_id": 1, "url": 1},
            )
        )
        targets = []
        digest = sha256()
        for state in sorted(states, key=lambda item: str(item.get("source_id") or "")):
            code = str(state.get("source_id") or "").strip().upper()
            raw = self.db[self.source_raw_collection].find_one(
                {"_id": code}, {"url": 1, "nro": 1, "content_sha256": 1, "html": 1}
            )
            if not code.startswith("COL") or not raw or not raw.get("url"):
                raise RuntimeError(f"incomplete GrupLAC target {code!r} lacks raw evidence")
            reference_hash = raw.get("content_sha256") or sha256(
                str(raw.get("html") or "").encode("utf-8")
            ).hexdigest()
            rank = len(targets) + 1
            target = {
                "_id": code,
                "rank": rank,
                "url": str(raw["url"]).rstrip(","),
                "nro": str(raw.get("nro") or ""),
                "reference_content_sha256": reference_hash,
                "selected_at": utc_now(),
            }
            targets.append(target)
            digest.update(f"{code}|{target['url']}|{reference_hash}\n".encode("utf-8"))

        if not targets:
            raise RuntimeError("no incomplete GrupLAC pages were found")
        destination = self.db[self.target_collection]
        destination.delete_many({})
        destination.insert_many(targets, ordered=True)
        destination.create_index("rank", unique=True)
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$setOnInsert": {"created_at": utc_now(), "config": self.config},
                "$set": {
                    "target_status": "ready",
                    "target_count": len(targets),
                    "target_sha256": digest.hexdigest(),
                    "status": "ready",
                },
            },
            upsert=True,
        )
        return targets

    def download(self, targets: list[dict]) -> list[dict]:
        downloader = ScientiProfileDownloader(
            self.db,
            workers=self.workers,
            requests_per_second=self.requests_per_second,
            state_collection=self.state_collection,
        )
        passes = []
        for pass_number in range(1, self.max_passes + 1):
            missing = len(targets) - self.db[self.raw_collection].count_documents({})
            if missing == 0:
                break
            print(
                f"{utc_now().isoformat()} INFO: GrupLAC verification pass "
                f"{pass_number}/{self.max_passes}, missing={missing}",
                flush=True,
            )
            counts = downloader.download(
                "gruplac",
                ({"id": item["_id"], "url": item["url"], "nro": item["nro"]} for item in targets),
                raw_collection=self.raw_collection,
                progress_every=10,
            )
            passes.append({"pass": pass_number, "counts": counts})
        return passes

    def compare_and_promote(self, targets: list[dict]) -> dict:
        outcomes = Counter()
        promoted = []
        results = self.db[self.result_collection]
        for target in targets:
            code = target["_id"]
            state = self.db[self.state_collection].find_one(
                {"_id": "gruplac:{}".format(code)}
            ) or {}
            verified = self.db[self.raw_collection].find_one({"_id": code})
            verified_status = state.get("status", "not_attempted")
            verified_hash = (verified or {}).get("content_sha256")
            same_hash = bool(
                verified_hash
                and verified_hash == target.get("reference_content_sha256")
            )
            if verified_status == "downloaded" and verified:
                outcome = "promoted_downloaded"
                self.db[self.source_raw_collection].replace_one(
                    {"_id": code}, verified, upsert=True
                )
                self.db[self.source_state_collection].update_one(
                    {"_id": "gruplac:{}".format(code)},
                    {
                        "$set": {
                            "status": "downloaded",
                            "verified_by": self.run_name,
                            "verified_at": utc_now(),
                            "updated_at": utc_now(),
                        }
                    },
                    upsert=True,
                )
                document = normalize_gruplac_document(
                    code,
                    verified.get("html", ""),
                    nro=str(verified.get("nro") or ""),
                    url=str(verified.get("url") or ""),
                )
                self.db[self.normalized_collection].replace_one(
                    {"_id": code}, document, upsert=True
                )
                promoted.append(code)
            elif verified_status == "incomplete" and same_hash:
                outcome = "confirmed_incomplete"
            elif verified_status == "incomplete":
                outcome = "changed_incomplete"
            elif verified_status == "empty":
                outcome = "verification_empty"
            else:
                outcome = "verification_error"
            outcomes[outcome] += 1
            results.replace_one(
                {"_id": code},
                {
                    "_id": code,
                    "outcome": outcome,
                    "reference_content_sha256": target.get("reference_content_sha256"),
                    "verified_content_sha256": verified_hash,
                    "content_unchanged": same_hash,
                    "verified_status": verified_status,
                    "verified_at": utc_now(),
                },
                upsert=True,
            )
        results.create_index("outcome")
        summary = {
            "targets": len(targets),
            "outcomes": dict(sorted(outcomes.items())),
            "promoted": len(promoted),
            "promoted_group_codes": promoted,
        }
        return summary

    def run(self) -> dict:
        try:
            targets = self.prepare_targets()
            self.runs.update_one(
                {"_id": self.run_name},
                {
                    "$set": {"status": "running", "last_started_at": utc_now()},
                    "$inc": {"execution_attempts": 1},
                    "$unset": {"error": ""},
                },
            )
            print(
                f"{utc_now().isoformat()} INFO: frozen GrupLAC incomplete verification, "
                f"run={self.run_name} targets={len(targets)} rate={self.requests_per_second}/s",
                flush=True,
            )
            passes = self.download(targets)
            summary = self.compare_and_promote(targets)
            status = (
                "complete_with_errors"
                if summary["outcomes"].get("verification_error")
                or summary["outcomes"].get("verification_empty")
                else "complete"
            )
            result = {"run_name": self.run_name, "status": status, **summary}
            self.runs.update_one(
                {"_id": self.run_name},
                {
                    "$set": {
                        "status": status,
                        "passes": passes,
                        "summary": summary,
                        "finished_at": utc_now(),
                    }
                },
            )
            print(
                f"{utc_now().isoformat()} INFO: GrupLAC verification finished, {result}",
                flush=True,
            )
            return result
        except Exception as error:
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
            raise
