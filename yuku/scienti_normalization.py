from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from hashlib import sha256
from multiprocessing import get_context
import re
from typing import Any

from pymongo import ASCENDING, ReplaceOne
from pymongo.errors import AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError

from yuku.cvlac_priority_snapshot import CvlacPrioritySnapshot
from yuku.cvlac_related_works import norm_text, normalize_related_works_document
from yuku.gruplac_related_works import normalize_gruplac_document


CVLAC_PARSER_NAME = "yuku.cvlac_related_works"
CVLAC_PARSER_VERSION = "3.2.0"
CVLAC_NORMALIZATION_RUNS = "scienti_cvlac_normalization_runs"
CVLAC_AUDIT_RUNS = "scienti_cvlac_normalization_audits"
GRUPLAC_AUDIT_RUNS = "scienti_gruplac_normalization_audits"
GRUPLAC_NORMALIZATION_RUNS = "scienti_gruplac_normalization_runs"
GRUPLAC_PARSER_NAME = "yuku.gruplac_related_works"
GRUPLAC_PARSER_VERSION = "3.2.0"

RUN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
DOI_RE = re.compile(r"^https://doi\.org/10\.\d{4,9}/\S+$", re.IGNORECASE)
SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
GRUPLAC_PERIOD_TITLE_SECTIONS = {
    "Estrategias Pedagógicas para el fomento a la CTI",
    "Estrategias de Comunicación del Conocimiento",
    "Participación Ciudadana en Proyectos de CTI",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_name(value: str, label: str) -> None:
    if not RUN_NAME_RE.fullmatch(value or ""):
        raise ValueError(f"{label} must contain only letters, numbers and underscores")


def _content_hash(raw: dict[str, Any]) -> str:
    value = str(raw.get("content_sha256") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return sha256(str(raw.get("html") or "").encode("utf-8")).hexdigest()


def _raw_hash_index(collection) -> dict[str, str]:
    """Load hashes without transferring HTML except for legacy rows lacking one."""
    hashes: dict[str, str] = {}
    missing_hash_ids: list[Any] = []
    for item in collection.find({}, {"content_sha256": 1}):
        value = str(item.get("content_sha256") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", value):
            hashes[str(item["_id"])] = value
        else:
            missing_hash_ids.append(item["_id"])
    for source_id in missing_hash_ids:
        raw = collection.find_one({"_id": source_id}, {"html": 1}) or {
            "html": ""
        }
        hashes[str(source_id)] = _content_hash(raw)
    return hashes


def _bulk_replace(collection, documents: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Exception]]:
    if not documents:
        return []
    operations = [ReplaceOne({"_id": item["_id"]}, item, upsert=True) for item in documents]
    try:
        collection.bulk_write(operations, ordered=False)
        return []
    except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError):
        raise
    except Exception:
        # mongomock releases predating the current PyMongo ReplaceOne API do
        # not accept every operation option. In production, retrying an
        # unsuccessful bulk one document at a time also isolates malformed or
        # over-sized profiles instead of losing the rest of the batch.
        failures = []
        for item in documents:
            try:
                collection.replace_one({"_id": item["_id"]}, item, upsert=True)
            except Exception as error:
                failures.append((item, error))
        return failures


def _normalize_cvlac_task(profile_id: str, html: str) -> dict[str, Any]:
    """Pure, picklable parser task; workers never access MongoDB."""
    try:
        return {
            "profile_id": profile_id,
            "document": normalize_related_works_document(profile_id, html),
            "error": "",
        }
    except Exception as error:
        return {
            "profile_id": profile_id,
            "document": None,
            "error": f"{type(error).__name__}: {error}",
        }


def _normalize_gruplac_task(
    group_code: str,
    html: str,
    nro: str,
    url: str,
) -> dict[str, Any]:
    """Pure GrupLAC parser task; MongoDB remains in the parent process."""
    try:
        return {
            "group_code": group_code,
            "document": normalize_gruplac_document(
                group_code, html, nro=nro, url=url
            ),
            "error": "",
        }
    except Exception as error:
        return {
            "group_code": group_code,
            "document": None,
            "error": f"{type(error).__name__}: {error}",
        }


def _sanitize_unicode_for_bson(value: Any) -> tuple[Any, int]:
    """Repair UTF-16 surrogate code units while preserving valid characters."""
    if isinstance(value, str):
        count = len(SURROGATE_RE.findall(value))
        if not count:
            return value, 0
        repaired = value.encode("utf-16", "surrogatepass").decode(
            "utf-16", "replace"
        )
        return repaired, count
    if isinstance(value, list):
        output = []
        total = 0
        for item in value:
            repaired, count = _sanitize_unicode_for_bson(item)
            output.append(repaired)
            total += count
        return output, total
    if isinstance(value, tuple):
        output = []
        total = 0
        for item in value:
            repaired, count = _sanitize_unicode_for_bson(item)
            output.append(repaired)
            total += count
        return tuple(output), total
    if isinstance(value, dict):
        output = {}
        total = 0
        for key, item in value.items():
            repaired_key, key_count = _sanitize_unicode_for_bson(key)
            repaired_item, item_count = _sanitize_unicode_for_bson(item)
            output[repaired_key] = repaired_item
            total += key_count + item_count
        return output, total
    return value, 0


class CvlacNormalizationRun:
    """Version and normalize a frozen complete CVLAC HTML snapshot.

    The destination is restartable at profile granularity. A profile is only
    skipped when both its source content hash and parser version match the
    current run, so re-execution cannot silently retain stale parsed data.
    """

    RUNS_COLLECTION = CVLAC_NORMALIZATION_RUNS

    def __init__(
        self,
        db,
        *,
        run_name: str,
        source_snapshot_run_name: str,
        destination_collection: str,
        source_state_collection: str | None = None,
        batch_size: int = 25,
        progress_every: int = 100,
        workers: int = 1,
    ):
        _validate_name(run_name, "run_name")
        _validate_name(source_snapshot_run_name, "source_snapshot_run_name")
        if not destination_collection or destination_collection.startswith("system."):
            raise ValueError("destination_collection is invalid")
        if batch_size < 1 or progress_every < 1 or workers < 1:
            raise ValueError("batch_size, progress_every and workers must be positive")
        self.db = db
        self.run_name = run_name
        self.source_snapshot_run_name = source_snapshot_run_name
        self.destination_collection = destination_collection
        self.requested_state_collection = source_state_collection
        self.batch_size = batch_size
        self.progress_every = progress_every
        self.workers = workers
        self.error_collection = f"{destination_collection}_errors"
        self.runs = db[self.RUNS_COLLECTION]
        self.source_run: dict[str, Any] = {}
        self.source_raw_collection = ""
        self.source_state_collection = ""
        self.expected_profiles = 0

    def _load_source(self) -> None:
        source_run = self.db[CvlacPrioritySnapshot.RUNS_COLLECTION].find_one(
            {"_id": self.source_snapshot_run_name}
        )
        if not source_run:
            raise ValueError(
                f"CVLAC snapshot run {self.source_snapshot_run_name!r} was not found"
            )
        if source_run.get("status") != "complete":
            raise RuntimeError(
                f"CVLAC snapshot run {self.source_snapshot_run_name!r} has status "
                f"{source_run.get('status')!r}, complete is required"
            )
        config = source_run.get("config") or {}
        raw_collection = str(config.get("raw_collection") or "")
        state_collection = str(
            self.requested_state_collection or config.get("state_collection") or ""
        )
        if not raw_collection or not state_collection:
            raise RuntimeError("source snapshot does not declare raw and state collections")
        available = set(self.db.list_collection_names())
        missing = sorted({raw_collection, state_collection} - available)
        if missing:
            raise RuntimeError(f"source snapshot collections are missing: {missing}")
        expected = int(source_run.get("target_count") or 0)
        actual = self.db[raw_collection].count_documents({})
        if expected < 1 or actual != expected:
            raise RuntimeError(f"CVLAC raw coverage is {actual}/{expected}")
        self.source_run = source_run
        self.source_raw_collection = raw_collection
        self.source_state_collection = state_collection
        self.expected_profiles = expected

    @property
    def config(self) -> dict[str, Any]:
        return {
            "source_snapshot_run_name": self.source_snapshot_run_name,
            "source_raw_collection": self.source_raw_collection,
            "source_state_collection": self.source_state_collection,
            "destination_collection": self.destination_collection,
            "error_collection": self.error_collection,
            "parser_name": CVLAC_PARSER_NAME,
            "parser_version": CVLAC_PARSER_VERSION,
        }

    def _prepare_run(self) -> None:
        existing = self.runs.find_one({"_id": self.run_name})
        if existing and (existing.get("config") or {}) != self.config:
            raise ValueError(
                f"normalization run {self.run_name!r} already exists with a different config"
            )
        now = utc_now()
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$setOnInsert": {"created_at": now, "config": self.config},
                "$set": {
                    "status": "running",
                    "last_started_at": now,
                    "last_progress_at": now,
                    "runtime": {
                        "batch_size": self.batch_size,
                        "progress_every": self.progress_every,
                        "workers": self.workers,
                    },
                },
                "$inc": {"execution_attempts": 1},
                "$unset": {"error": ""},
            },
            upsert=True,
        )

    def _state_by_profile(self) -> dict[str, str]:
        return {
            str(item.get("source_id") or ""): str(item.get("status") or "unknown")
            for item in self.db[self.source_state_collection].find(
                {"kind": "cvlac"}, {"source_id": 1, "status": 1}
            )
            if item.get("source_id") is not None
        }

    def _store_write_failures(
        self,
        errors_collection,
        failures: list[tuple[dict[str, Any], Exception]],
    ) -> int:
        for document, error in failures:
            profile_id = str(document["_id"])
            errors_collection.replace_one(
                {"_id": profile_id},
                {
                    "_id": profile_id,
                    "run_name": self.run_name,
                    "parser_version": CVLAC_PARSER_VERSION,
                    "source_content_sha256": str(
                        ((document.get("source") or {}).get("content_sha256")) or ""
                    ),
                    "stage": "mongodb_write",
                    "error": f"{type(error).__name__}: {error}",
                    "failed_at": utc_now(),
                },
                upsert=True,
            )
            print(
                f"{utc_now().isoformat()} ERROR: CVLAC normalized write failed "
                f"for {profile_id}: {type(error).__name__}: {error}",
                flush=True,
            )
        return len(failures)

    def _completed_by_profile(self) -> dict[str, tuple[str, str]]:
        return {
            str(item["_id"]): (
                str(((item.get("parser") or {}).get("version")) or ""),
                str(((item.get("source") or {}).get("content_sha256")) or ""),
            )
            for item in self.db[self.destination_collection].find(
                {}, {"parser.version": 1, "source.content_sha256": 1}
            )
        }

    def _source_metadata(self, raw: dict[str, Any], content_hash: str) -> dict[str, Any]:
        return {
            "snapshot_run_name": self.source_snapshot_run_name,
            "raw_collection": self.source_raw_collection,
            "state_collection": self.source_state_collection,
            "url": str(raw.get("url") or ""),
            "fetched_at": raw.get("fetched_at"),
            "http_status": raw.get("http_status"),
            "content_sha256": content_hash,
            "source_content_sha256": str(raw.get("source_content_sha256") or ""),
            "encoding": str(raw.get("encoding") or ""),
            "decode_replacement_chars": int(raw.get("decode_replacement_chars") or 0),
        }

    def _record_parse_failure(
        self,
        errors_collection,
        profile_id: str,
        content_hash: str,
        error: str,
    ) -> None:
        errors_collection.replace_one(
            {"_id": profile_id},
            {
                "_id": profile_id,
                "run_name": self.run_name,
                "parser_version": CVLAC_PARSER_VERSION,
                "source_content_sha256": content_hash,
                "stage": "parser",
                "error": error,
                "failed_at": utc_now(),
            },
            upsert=True,
        )
        print(
            f"{utc_now().isoformat()} ERROR: CVLAC normalization failed "
            f"for {profile_id}: {error}",
            flush=True,
        )

    def _enrich_document(
        self,
        document: dict[str, Any],
        raw: dict[str, Any],
        content_hash: str,
        profile_status: str,
        source_status: str,
    ) -> dict[str, Any]:
        document["profile_status"] = profile_status
        document["parser"] = {
            "name": CVLAC_PARSER_NAME,
            "version": CVLAC_PARSER_VERSION,
            "normalized_at": utc_now(),
        }
        document["source"] = self._source_metadata(raw, content_hash)
        document["source"]["download_status"] = source_status
        document, repaired_surrogates = _sanitize_unicode_for_bson(document)
        document["parser"]["unicode_surrogates_repaired"] = repaired_surrogates
        return document

    def _progress(
        self,
        *,
        position: int,
        processed: int,
        skipped: int,
        errors: int,
        status_counts: Counter,
        content_counts: Counter,
        last_profile_id: str,
        in_flight: int = 0,
    ) -> None:
        progress = {
            "position": position,
            "total": self.expected_profiles,
            "processed_this_attempt": processed,
            "skipped_this_attempt": skipped,
            "errors_this_attempt": errors,
            "profile_status_counts": dict(sorted(status_counts.items())),
            "content_counts": dict(sorted(content_counts.items())),
            "last_profile_id": last_profile_id,
            "in_flight": in_flight,
        }
        now = utc_now()
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"progress": progress, "last_progress_at": now}},
        )
        print(
            f"{now.isoformat()} INFO: CVLAC normalization "
            f"position={position}/{self.expected_profiles} processed={processed} "
            f"skipped={skipped} errors={errors} in_flight={in_flight} "
            f"last={last_profile_id}",
            flush=True,
        )

    def _finish_run(
        self,
        destination,
        errors_collection,
        *,
        processed: int,
        skipped: int,
        failures: int,
        status_counts: Counter,
        content_counts: Counter,
    ) -> dict[str, Any]:
        destination_count = destination.count_documents({})
        error_count = errors_collection.count_documents({})
        coverage_complete = destination_count == self.expected_profiles
        status = (
            "complete"
            if coverage_complete and error_count == 0
            else "complete_with_errors"
        )
        summary = {
            "source_profiles": self.expected_profiles,
            "destination_profiles": destination_count,
            "processed_this_attempt": processed,
            "skipped_this_attempt": skipped,
            "errors_this_attempt": failures,
            "stored_errors": error_count,
            "profile_status_counts": dict(sorted(status_counts.items())),
            "content_counts_this_attempt": dict(sorted(content_counts.items())),
            "parser_version": CVLAC_PARSER_VERSION,
            "workers": self.workers,
            "destination_collection": self.destination_collection,
        }
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$set": {
                    "status": status,
                    "summary": summary,
                    "finished_at": utc_now(),
                    "last_progress_at": utc_now(),
                }
            },
        )
        print(
            f"{utc_now().isoformat()} INFO: CVLAC normalization finished, "
            f"run={self.run_name} status={status} summary={summary}",
            flush=True,
        )
        return {"run_name": self.run_name, "status": status, **summary}

    def _run_parallel(
        self,
        destination,
        errors_collection,
        existing_error_ids: set[str],
        completed: dict[str, tuple[str, str]],
        states: dict[str, str],
    ) -> dict[str, Any]:
        processed = skipped = failures = 0
        status_counts: Counter = Counter()
        content_counts: Counter = Counter()
        pending_documents: list[dict[str, Any]] = []
        in_flight: dict[Any, tuple[dict[str, Any], str, str, str]] = {}
        max_in_flight = self.workers * 4
        last_profile_id = ""
        last_position = 0
        projection = {
            "html": 1,
            "url": 1,
            "fetched_at": 1,
            "http_status": 1,
            "content_sha256": 1,
            "source_content_sha256": 1,
            "encoding": 1,
            "decode_replacement_chars": 1,
        }

        def flush_documents() -> None:
            nonlocal failures, processed, pending_documents
            while len(pending_documents) >= self.batch_size:
                batch = pending_documents[: self.batch_size]
                del pending_documents[: self.batch_size]
                write_failures = _bulk_replace(destination, batch)
                failures += self._store_write_failures(
                    errors_collection, write_failures
                )
                processed -= len(write_failures)

        def consume(futures) -> None:
            nonlocal failures, processed
            for future in futures:
                raw, content_hash, profile_status, source_status = in_flight.pop(
                    future
                )
                result = future.result()
                profile_id = str(raw["_id"])
                if result.get("error"):
                    failures += 1
                    self._record_parse_failure(
                        errors_collection,
                        profile_id,
                        content_hash,
                        str(result["error"]),
                    )
                    continue
                document = self._enrich_document(
                    result["document"],
                    raw,
                    content_hash,
                    profile_status,
                    source_status,
                )
                content_counts["production"] += len(document.get("production") or [])
                content_counts["patents"] += len(document.get("patents") or [])
                content_counts["events"] += len(document.get("events") or [])
                content_counts["projects"] += len(document.get("projects") or [])
                pending_documents.append(document)
                processed += 1
                if profile_id in existing_error_ids:
                    errors_collection.delete_one({"_id": profile_id})
                    existing_error_ids.discard(profile_id)
            flush_documents()

        context = get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=context,
        ) as executor:
            cursor = self.db[self.source_raw_collection].find({}, projection).sort(
                "_id", ASCENDING
            )
            for position, raw in enumerate(cursor, start=1):
                last_position = position
                profile_id = str(raw["_id"])
                last_profile_id = profile_id
                content_hash = _content_hash(raw)
                if profile_id not in states:
                    raise RuntimeError(
                        f"CVLAC profile {profile_id} lacks download state"
                    )
                source_status = states[profile_id]
                profile_status = (
                    "public"
                    if source_status == "downloaded"
                    else "private"
                    if source_status == "private"
                    else source_status
                )
                status_counts[profile_status] += 1
                if completed.get(profile_id) == (
                    CVLAC_PARSER_VERSION,
                    content_hash,
                ):
                    skipped += 1
                else:
                    while len(in_flight) >= max_in_flight:
                        done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                        consume(done)
                    future = executor.submit(
                        _normalize_cvlac_task,
                        profile_id,
                        str(raw.get("html") or ""),
                    )
                    in_flight[future] = (
                        raw,
                        content_hash,
                        profile_status,
                        source_status,
                    )
                if position == 1 or position % self.progress_every == 0:
                    ready = {future for future in in_flight if future.done()}
                    if ready:
                        consume(ready)
                    if pending_documents:
                        write_failures = _bulk_replace(
                            destination, pending_documents
                        )
                        failures += self._store_write_failures(
                            errors_collection, write_failures
                        )
                        processed -= len(write_failures)
                        pending_documents = []
                    self._progress(
                        position=position,
                        processed=processed,
                        skipped=skipped,
                        errors=failures,
                        status_counts=status_counts,
                        content_counts=content_counts,
                        last_profile_id=profile_id,
                        in_flight=len(in_flight),
                    )
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                consume(done)
        if pending_documents:
            write_failures = _bulk_replace(destination, pending_documents)
            failures += self._store_write_failures(
                errors_collection, write_failures
            )
            processed -= len(write_failures)
        self._progress(
            position=last_position,
            processed=processed,
            skipped=skipped,
            errors=failures,
            status_counts=status_counts,
            content_counts=content_counts,
            last_profile_id=last_profile_id,
        )
        return self._finish_run(
            destination,
            errors_collection,
            processed=processed,
            skipped=skipped,
            failures=failures,
            status_counts=status_counts,
            content_counts=content_counts,
        )

    def run(self) -> dict[str, Any]:
        self._load_source()
        self._prepare_run()
        destination = self.db[self.destination_collection]
        errors_collection = self.db[self.error_collection]
        existing_error_ids = {
            str(value) for value in errors_collection.distinct("_id")
        }
        completed = self._completed_by_profile()
        states = self._state_by_profile()
        if len(states) != self.expected_profiles:
            raise RuntimeError(
                f"CVLAC download-state coverage is {len(states)}/{self.expected_profiles}"
            )
        print(
            f"{utc_now().isoformat()} INFO: starting frozen CVLAC normalization, "
            f"run={self.run_name} source={self.source_raw_collection} "
            f"destination={self.destination_collection} profiles={self.expected_profiles} "
            f"already_normalized={len(completed)} parser={CVLAC_PARSER_VERSION} "
            f"workers={self.workers}",
            flush=True,
        )

        if self.workers > 1:
            try:
                return self._run_parallel(
                    destination,
                    errors_collection,
                    existing_error_ids,
                    completed,
                    states,
                )
            except Exception as error:
                self.fail(error)
                raise

        processed = skipped = failures = 0
        status_counts: Counter = Counter()
        content_counts: Counter = Counter()
        pending: list[dict[str, Any]] = []
        projection = {
            "html": 1,
            "url": 1,
            "fetched_at": 1,
            "http_status": 1,
            "content_sha256": 1,
            "source_content_sha256": 1,
            "encoding": 1,
            "decode_replacement_chars": 1,
        }
        try:
            cursor = self.db[self.source_raw_collection].find({}, projection).sort(
                "_id", ASCENDING
            )
            for position, raw in enumerate(cursor, start=1):
                profile_id = str(raw["_id"])
                content_hash = _content_hash(raw)
                if profile_id not in states:
                    raise RuntimeError(f"CVLAC profile {profile_id} lacks download state")
                source_status = states[profile_id]
                profile_status = (
                    "public"
                    if source_status == "downloaded"
                    else "private"
                    if source_status == "private"
                    else source_status
                )
                status_counts[profile_status] += 1
                if completed.get(profile_id) == (CVLAC_PARSER_VERSION, content_hash):
                    skipped += 1
                else:
                    try:
                        document = normalize_related_works_document(
                            profile_id, str(raw.get("html") or "")
                        )
                        normalized_at = utc_now()
                        document["profile_status"] = profile_status
                        document["parser"] = {
                            "name": CVLAC_PARSER_NAME,
                            "version": CVLAC_PARSER_VERSION,
                            "normalized_at": normalized_at,
                        }
                        document["source"] = self._source_metadata(raw, content_hash)
                        document["source"]["download_status"] = source_status
                        content_counts["production"] += len(document.get("production") or [])
                        content_counts["patents"] += len(document.get("patents") or [])
                        content_counts["events"] += len(document.get("events") or [])
                        content_counts["projects"] += len(document.get("projects") or [])
                        pending.append(document)
                        processed += 1
                        if profile_id in existing_error_ids:
                            errors_collection.delete_one({"_id": profile_id})
                            existing_error_ids.discard(profile_id)
                    except Exception as error:
                        failures += 1
                        errors_collection.replace_one(
                            {"_id": profile_id},
                            {
                                "_id": profile_id,
                                "run_name": self.run_name,
                                "parser_version": CVLAC_PARSER_VERSION,
                                "source_content_sha256": content_hash,
                                "error": f"{type(error).__name__}: {error}",
                                "failed_at": utc_now(),
                            },
                            upsert=True,
                        )
                        print(
                            f"{utc_now().isoformat()} ERROR: CVLAC normalization failed "
                            f"for {profile_id}: {type(error).__name__}: {error}",
                            flush=True,
                        )
                if len(pending) >= self.batch_size:
                    write_failures = _bulk_replace(destination, pending)
                    failures += self._store_write_failures(
                        errors_collection, write_failures
                    )
                    processed -= len(write_failures)
                    pending = []
                if position == 1 or position % self.progress_every == 0:
                    write_failures = _bulk_replace(destination, pending)
                    failures += self._store_write_failures(
                        errors_collection, write_failures
                    )
                    processed -= len(write_failures)
                    pending = []
                    self._progress(
                        position=position,
                        processed=processed,
                        skipped=skipped,
                        errors=failures,
                        status_counts=status_counts,
                        content_counts=content_counts,
                        last_profile_id=profile_id,
                    )
            write_failures = _bulk_replace(destination, pending)
            failures += self._store_write_failures(errors_collection, write_failures)
            processed -= len(write_failures)
            return self._finish_run(
                destination,
                errors_collection,
                processed=processed,
                skipped=skipped,
                failures=failures,
                status_counts=status_counts,
                content_counts=content_counts,
            )
        except Exception as error:
            self.fail(error)
            raise

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


class GruplacNormalizationRun:
    """Normalize a frozen GrupLAC HTML collection with parallel safe resume."""

    RUNS_COLLECTION = GRUPLAC_NORMALIZATION_RUNS

    def __init__(
        self,
        db,
        *,
        run_name: str,
        source_raw_collection: str,
        source_state_collection: str,
        destination_collection: str,
        workers: int = 1,
        progress_every: int = 50,
    ):
        _validate_name(run_name, "run_name")
        if workers < 1 or progress_every < 1:
            raise ValueError("workers and progress_every must be positive")
        self.db = db
        self.run_name = run_name
        self.source_raw_collection = source_raw_collection
        self.source_state_collection = source_state_collection
        self.destination_collection = destination_collection
        self.error_collection = f"{destination_collection}_errors"
        self.workers = workers
        self.progress_every = progress_every
        self.runs = db[self.RUNS_COLLECTION]

    @property
    def config(self) -> dict[str, Any]:
        return {
            "source_raw_collection": self.source_raw_collection,
            "source_state_collection": self.source_state_collection,
            "destination_collection": self.destination_collection,
            "error_collection": self.error_collection,
            "parser_name": GRUPLAC_PARSER_NAME,
            "parser_version": GRUPLAC_PARSER_VERSION,
        }

    def _prepare(self) -> tuple[int, dict[str, str]]:
        available = set(self.db.list_collection_names())
        missing = sorted(
            {self.source_raw_collection, self.source_state_collection} - available
        )
        if missing:
            raise ValueError(f"required GrupLAC collections are missing: {missing}")
        source_count = self.db[self.source_raw_collection].count_documents({})
        states = {
            str(item.get("source_id") or "").upper(): str(
                item.get("status") or "unknown"
            )
            for item in self.db[self.source_state_collection].find(
                {"kind": "gruplac"}, {"source_id": 1, "status": 1}
            )
            if item.get("source_id")
        }
        if source_count < 1 or len(states) != source_count:
            raise RuntimeError(
                f"GrupLAC source/state coverage is {source_count}/{len(states)}"
            )
        existing = self.runs.find_one({"_id": self.run_name})
        if existing and (existing.get("config") or {}) != self.config:
            raise ValueError(
                f"GrupLAC normalization {self.run_name!r} exists with a different config"
            )
        now = utc_now()
        self.runs.update_one(
            {"_id": self.run_name},
            {
                "$setOnInsert": {"created_at": now, "config": self.config},
                "$set": {
                    "status": "running",
                    "last_started_at": now,
                    "last_progress_at": now,
                    "runtime": {
                        "workers": self.workers,
                        "progress_every": self.progress_every,
                    },
                },
                "$inc": {"execution_attempts": 1},
                "$unset": {"error": ""},
            },
            upsert=True,
        )
        return source_count, states

    def _progress(
        self,
        *,
        position: int,
        total: int,
        processed: int,
        skipped: int,
        failures: int,
        status_counts: Counter,
        content_counts: Counter,
        last_group_code: str,
        in_flight: int,
    ) -> None:
        progress = {
            "position": position,
            "total": total,
            "processed_this_attempt": processed,
            "skipped_this_attempt": skipped,
            "errors_this_attempt": failures,
            "group_status_counts": dict(sorted(status_counts.items())),
            "content_counts": dict(sorted(content_counts.items())),
            "last_group_code": last_group_code,
            "in_flight": in_flight,
        }
        now = utc_now()
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"progress": progress, "last_progress_at": now}},
        )
        print(
            f"{now.isoformat()} INFO: GrupLAC normalization "
            f"position={position}/{total} processed={processed} skipped={skipped} "
            f"errors={failures} in_flight={in_flight} last={last_group_code}",
            flush=True,
        )

    def run(self) -> dict[str, Any]:
        source_count, states = self._prepare()
        source = self.db[self.source_raw_collection]
        destination = self.db[self.destination_collection]
        errors_collection = self.db[self.error_collection]
        existing_error_ids = {
            str(value) for value in errors_collection.distinct("_id")
        }
        completed = {
            str(item["_id"]): (
                str(((item.get("parser") or {}).get("version")) or ""),
                str(((item.get("source") or {}).get("content_sha256")) or ""),
            )
            for item in destination.find(
                {}, {"parser.version": 1, "source.content_sha256": 1}
            )
        }
        print(
            f"{utc_now().isoformat()} INFO: starting GrupLAC normalization, "
            f"run={self.run_name} source={self.source_raw_collection} "
            f"destination={self.destination_collection} groups={source_count} "
            f"already_normalized={len(completed)} parser={GRUPLAC_PARSER_VERSION} "
            f"workers={self.workers}",
            flush=True,
        )
        processed = skipped = failures = 0
        status_counts: Counter = Counter()
        content_counts: Counter = Counter()
        in_flight: dict[Any, tuple[dict[str, Any], str, str]] = {}
        projection = {
            "html": 1,
            "url": 1,
            "nro": 1,
            "fetched_at": 1,
            "http_status": 1,
            "content_sha256": 1,
            "source_content_sha256": 1,
            "encoding": 1,
            "decode_replacement_chars": 1,
        }

        def record_failure(
            group_code: str,
            content_hash: str,
            stage: str,
            error: str,
        ) -> None:
            errors_collection.replace_one(
                {"_id": group_code},
                {
                    "_id": group_code,
                    "run_name": self.run_name,
                    "parser_version": GRUPLAC_PARSER_VERSION,
                    "source_content_sha256": content_hash,
                    "stage": stage,
                    "error": error,
                    "failed_at": utc_now(),
                },
                upsert=True,
            )
            print(
                f"{utc_now().isoformat()} ERROR: GrupLAC {stage} failed for "
                f"{group_code}: {error}",
                flush=True,
            )

        def consume_result(
            result: dict[str, Any],
            raw: dict[str, Any],
            content_hash: str,
            group_status: str,
        ) -> None:
            nonlocal processed, failures
            group_code = str(raw["_id"]).upper()
            if result.get("error"):
                failures += 1
                record_failure(
                    group_code, content_hash, "parser", str(result["error"])
                )
                return
            document = result["document"]
            document["group_status"] = group_status
            document["parser"] = {
                "name": GRUPLAC_PARSER_NAME,
                "version": GRUPLAC_PARSER_VERSION,
                "normalized_at": utc_now(),
            }
            document["source"] = {
                "raw_collection": self.source_raw_collection,
                "state_collection": self.source_state_collection,
                "url": str(raw.get("url") or ""),
                "fetched_at": raw.get("fetched_at"),
                "http_status": raw.get("http_status"),
                "content_sha256": content_hash,
                "source_content_sha256": str(
                    raw.get("source_content_sha256") or ""
                ),
                "encoding": str(raw.get("encoding") or ""),
                "decode_replacement_chars": int(
                    raw.get("decode_replacement_chars") or 0
                ),
                "download_status": group_status,
            }
            document, repaired = _sanitize_unicode_for_bson(document)
            document["parser"]["unicode_surrogates_repaired"] = repaired
            write_failures = _bulk_replace(destination, [document])
            if write_failures:
                failures += 1
                error = write_failures[0][1]
                record_failure(
                    group_code,
                    content_hash,
                    "mongodb_write",
                    f"{type(error).__name__}: {error}",
                )
                return
            processed += 1
            content_counts["production"] += int(
                document.get("production_count") or 0
            )
            content_counts["members"] += int(document.get("members_count") or 0)
            if group_code in existing_error_ids:
                errors_collection.delete_one({"_id": group_code})
                existing_error_ids.discard(group_code)

        def consume_futures(futures) -> None:
            for future in futures:
                raw, content_hash, group_status = in_flight.pop(future)
                consume_result(
                    future.result(), raw, content_hash, group_status
                )

        last_position = 0
        last_group_code = ""
        try:
            executor = None
            if self.workers > 1:
                executor = ProcessPoolExecutor(
                    max_workers=self.workers,
                    mp_context=get_context("spawn"),
                )
            try:
                cursor = source.find({}, projection).sort("_id", ASCENDING)
                for position, raw in enumerate(cursor, start=1):
                    last_position = position
                    group_code = str(raw["_id"]).upper()
                    last_group_code = group_code
                    if group_code not in states:
                        raise RuntimeError(
                            f"GrupLAC group {group_code} lacks download state"
                        )
                    group_status = states[group_code]
                    status_counts[group_status] += 1
                    content_hash = _content_hash(raw)
                    if completed.get(group_code) == (
                        GRUPLAC_PARSER_VERSION,
                        content_hash,
                    ):
                        skipped += 1
                    elif executor is None:
                        consume_result(
                            _normalize_gruplac_task(
                                group_code,
                                str(raw.get("html") or ""),
                                str(raw.get("nro") or ""),
                                str(raw.get("url") or ""),
                            ),
                            raw,
                            content_hash,
                            group_status,
                        )
                    else:
                        while len(in_flight) >= self.workers * 2:
                            done, _ = wait(
                                in_flight, return_when=FIRST_COMPLETED
                            )
                            consume_futures(done)
                        future = executor.submit(
                            _normalize_gruplac_task,
                            group_code,
                            str(raw.get("html") or ""),
                            str(raw.get("nro") or ""),
                            str(raw.get("url") or ""),
                        )
                        in_flight[future] = (
                            raw,
                            content_hash,
                            group_status,
                        )
                    if position == 1 or position % self.progress_every == 0:
                        ready = {future for future in in_flight if future.done()}
                        if ready:
                            consume_futures(ready)
                        self._progress(
                            position=position,
                            total=source_count,
                            processed=processed,
                            skipped=skipped,
                            failures=failures,
                            status_counts=status_counts,
                            content_counts=content_counts,
                            last_group_code=group_code,
                            in_flight=len(in_flight),
                        )
                while in_flight:
                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    consume_futures(done)
            finally:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=False)
            destination.create_index("group_code", unique=True)
            destination_count = destination.count_documents({})
            error_count = errors_collection.count_documents({})
            aggregate_counts = next(
                destination.aggregate(
                    [
                        {
                            "$group": {
                                "_id": None,
                                "production": {
                                    "$sum": {"$ifNull": ["$production_count", 0]}
                                },
                                "members": {
                                    "$sum": {"$ifNull": ["$members_count", 0]}
                                },
                            }
                        }
                    ]
                ),
                {},
            )
            complete_content_counts = {
                "members": int(aggregate_counts.get("members") or 0),
                "production": int(aggregate_counts.get("production") or 0),
            }
            status = (
                "complete"
                if destination_count == source_count and error_count == 0
                else "complete_with_errors"
            )
            summary = {
                "source_groups": source_count,
                "destination_groups": destination_count,
                "processed_this_attempt": processed,
                "skipped_this_attempt": skipped,
                "errors_this_attempt": failures,
                "stored_errors": error_count,
                "group_status_counts": dict(sorted(status_counts.items())),
                "content_counts": complete_content_counts,
                "content_counts_this_attempt": dict(
                    sorted(content_counts.items())
                ),
                "parser_version": GRUPLAC_PARSER_VERSION,
                "workers": self.workers,
                "destination_collection": self.destination_collection,
            }
            self.runs.update_one(
                {"_id": self.run_name},
                {
                    "$set": {
                        "status": status,
                        "summary": summary,
                        "finished_at": utc_now(),
                        "last_progress_at": utc_now(),
                    }
                },
            )
            self._progress(
                position=last_position,
                total=source_count,
                processed=processed,
                skipped=skipped,
                failures=failures,
                status_counts=status_counts,
                content_counts=content_counts,
                last_group_code=last_group_code,
                in_flight=0,
            )
            print(
                f"{utc_now().isoformat()} INFO: GrupLAC normalization finished, "
                f"run={self.run_name} status={status} summary={summary}",
                flush=True,
            )
            return {"run_name": self.run_name, "status": status, **summary}
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
            )
            raise


class _AnomalyRecorder:
    def __init__(self, collection, audit_name: str, example_limit: int):
        self.collection = collection
        self.audit_name = audit_name
        self.example_limit = example_limit
        self.counts: Counter = Counter()
        self.examples: Counter = Counter()

    def add(self, kind: str, source_id: str, detail: Any = None) -> None:
        self.counts[kind] += 1
        if self.examples[kind] >= self.example_limit:
            return
        sequence = self.examples[kind]
        self.examples[kind] += 1
        document = {
            "_id": "{}:{}:{}".format(kind, source_id, sequence),
            "audit_name": self.audit_name,
            "kind": kind,
            "source_id": source_id,
            "detail": detail,
        }
        self.collection.replace_one({"_id": document["_id"]}, document, upsert=True)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _valid_year(value: Any) -> bool:
    if value in (None, ""):
        return True
    try:
        year = int(value)
    except (TypeError, ValueError):
        return False
    return 1800 <= year <= utc_now().year + 1


def _audit_records(
    recorder: _AnomalyRecorder,
    source_id: str,
    section: str,
    records: Any,
    distributions: Counter,
) -> int:
    if not isinstance(records, list):
        recorder.add(f"{section}_not_list", source_id, type(records).__name__)
        return 0
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            recorder.add(f"{section}_record_not_object", source_id, index)
            continue
        title = str(record.get("title") or "").strip()
        if not title:
            recorder.add(f"{section}_missing_title", source_id, index)
        if (
            section == "production"
            and str(record.get("source_section") or "")
            in GRUPLAC_PERIOD_TITLE_SECTIONS
        ):
            source_section = str(record.get("source_section") or "")
            if re.match(r"^desde\s+", title, flags=re.IGNORECASE):
                recorder.add(
                    "production_title_period_misalignment",
                    source_id,
                    {
                        "index": index,
                        "source_section": source_section,
                        "product_type": record.get("product_type"),
                        "title": title,
                    },
                )
            if str(record.get("product_type") or "") != source_section:
                recorder.add(
                    "production_timeline_type_mismatch",
                    source_id,
                    {
                        "index": index,
                        "source_section": source_section,
                        "product_type": record.get("product_type"),
                    },
                )
            if not str(record.get("start_date") or "").strip():
                recorder.add(
                    "production_timeline_missing_start_date",
                    source_id,
                    {"index": index, "title": title},
                )
        if not _valid_year(record.get("year")):
            recorder.add(
                f"{section}_invalid_year",
                source_id,
                {"index": index, "year": record.get("year")},
            )
        authors = record.get("authors")
        if authors is not None and not isinstance(authors, list):
            recorder.add(f"{section}_authors_not_list", source_id, index)
        elif isinstance(authors, list) and len(authors) > 50:
            recorder.add(
                f"{section}_suspicious_author_count", source_id, {"index": index, "count": len(authors)}
            )
        for doi in _as_list(record.get("doi")):
            if not DOI_RE.fullmatch(str(doi).strip()):
                recorder.add(f"{section}_invalid_doi", source_id, {"index": index, "value": doi})
        for isbn in _as_list(record.get("isbn")):
            compact = re.sub(r"[^0-9Xx]", "", str(isbn))
            if len(compact) not in {10, 13}:
                recorder.add(f"{section}_invalid_isbn", source_id, {"index": index, "value": isbn})
        distributions[str(record.get("type_impactu") or "(vacío)")] += 1
    return len(records)


def _count_bibliographic_metadata(records: Any, totals: Counter) -> None:
    """Record extraction coverage without treating missing source data as error."""
    if not isinstance(records, list):
        return
    for record in records:
        if not isinstance(record, dict):
            continue
        value = norm_text(
            f"{record.get('type_impactu', '')} {record.get('source_section', '')}"
        )
        if "libro" not in value and "editorial" not in value:
            continue
        totals["records"] += 1
        if "capitulo" in value:
            totals["chapters"] += 1
        elif "editorial" in value:
            totals["editorial_products"] += 1
        else:
            totals["books"] += 1
        for field in (
            "publisher",
            "book_title",
            "edition",
            "volume",
            "pages",
            "publication_place",
            "language",
            "dissemination_medium",
        ):
            if str(record.get(field) or "").strip():
                totals[f"with_{field}"] += 1


class CvlacNormalizationAuditor:
    """Perform a full coverage, provenance and metadata audit of normalized CVLAC."""

    RUNS_COLLECTION = CVLAC_AUDIT_RUNS

    def __init__(
        self,
        db,
        *,
        audit_name: str,
        normalization_run_name: str,
        progress_every: int = 1000,
        anomaly_example_limit: int = 100,
    ):
        _validate_name(audit_name, "audit_name")
        _validate_name(normalization_run_name, "normalization_run_name")
        self.db = db
        self.audit_name = audit_name
        self.normalization_run_name = normalization_run_name
        self.progress_every = progress_every
        self.anomaly_example_limit = anomaly_example_limit
        self.anomaly_collection = f"{audit_name}_anomalies"
        self.runs = db[self.RUNS_COLLECTION]

    def run(self) -> dict[str, Any]:
        normalization = self.db[CVLAC_NORMALIZATION_RUNS].find_one(
            {"_id": self.normalization_run_name}
        )
        if not normalization:
            raise ValueError(f"normalization run {self.normalization_run_name!r} was not found")
        if normalization.get("status") not in {"complete", "complete_with_errors"}:
            raise RuntimeError("normalization must be finished before it can be audited")
        config = normalization.get("config") or {}
        immutable_config = {
            "normalization_run_name": self.normalization_run_name,
            "source_raw_collection": config.get("source_raw_collection"),
            "source_state_collection": config.get("source_state_collection"),
            "destination_collection": config.get("destination_collection"),
            "parser_version": config.get("parser_version"),
        }
        existing = self.runs.find_one({"_id": self.audit_name})
        if existing and existing.get("config") != immutable_config:
            raise ValueError(f"audit {self.audit_name!r} exists with a different config")
        now = utc_now()
        self.runs.update_one(
            {"_id": self.audit_name},
            {
                "$setOnInsert": {"created_at": now, "config": immutable_config},
                "$set": {"status": "running", "last_started_at": now, "last_progress_at": now},
                "$inc": {"execution_attempts": 1},
                "$unset": {"error": ""},
            },
            upsert=True,
        )
        anomalies_collection = self.db[self.anomaly_collection]
        anomalies_collection.delete_many({"audit_name": self.audit_name})
        recorder = _AnomalyRecorder(
            anomalies_collection, self.audit_name, self.anomaly_example_limit
        )
        raw_collection = str(config["source_raw_collection"])
        state_collection = str(config["source_state_collection"])
        destination_collection = str(config["destination_collection"])
        expected_parser = str(config["parser_version"])
        raw_hashes = _raw_hash_index(self.db[raw_collection])
        expected_status = {
            str(item.get("source_id")): (
                "public" if item.get("status") == "downloaded" else str(item.get("status") or "unknown")
            )
            for item in self.db[state_collection].find(
                {"kind": "cvlac"}, {"source_id": 1, "status": 1}
            )
        }
        seen: set[str] = set()
        totals: Counter = Counter()
        status_counts: Counter = Counter()
        parser_counts: Counter = Counter()
        distributions: Counter = Counter()
        bibliographic_totals: Counter = Counter()
        projection = {
            "profile_status": 1,
            "parser": 1,
            "source": 1,
            "production_counts": 1,
            "production": 1,
            "patents_counts": 1,
            "patents": 1,
            "events_counts": 1,
            "events": 1,
            "projects_counts": 1,
            "projects": 1,
        }
        try:
            cursor = self.db[destination_collection].find({}, projection).sort("_id", ASCENDING)
            for position, document in enumerate(cursor, start=1):
                profile_id = str(document["_id"])
                seen.add(profile_id)
                profile_status = str(document.get("profile_status") or "missing")
                status_counts[profile_status] += 1
                parser_version = str(((document.get("parser") or {}).get("version")) or "missing")
                parser_counts[parser_version] += 1
                if parser_version != expected_parser:
                    recorder.add("parser_version_mismatch", profile_id, parser_version)
                source = document.get("source") or {}
                source_hash = str(source.get("content_sha256") or "")
                expected_hash = raw_hashes.get(profile_id)
                if expected_hash is None:
                    recorder.add("unknown_profile", profile_id)
                elif source_hash != expected_hash:
                    recorder.add(
                        "source_hash_mismatch",
                        profile_id,
                        {"expected": expected_hash, "actual": source_hash},
                    )
                if str(source.get("snapshot_run_name") or "") != str(
                    config.get("source_snapshot_run_name") or ""
                ):
                    recorder.add("source_run_mismatch", profile_id)
                if profile_status != expected_status.get(profile_id, "unknown"):
                    recorder.add(
                        "profile_status_mismatch",
                        profile_id,
                        {"expected": expected_status.get(profile_id), "actual": profile_status},
                    )
                for section, count_field in (
                    ("production", "production_counts"),
                    ("patents", "patents_counts"),
                    ("events", "events_counts"),
                    ("projects", "projects_counts"),
                ):
                    count = _audit_records(
                        recorder,
                        profile_id,
                        section,
                        document.get(section),
                        distributions,
                    )
                    totals[section] += count
                    if section == "production":
                        _count_bibliographic_metadata(
                            document.get(section), bibliographic_totals
                        )
                    if document.get(count_field) != count:
                        recorder.add(
                            f"{section}_count_mismatch",
                            profile_id,
                            {"declared": document.get(count_field), "actual": count},
                        )
                if profile_status == "private" and sum(
                    len(_as_list(document.get(section)))
                    for section in ("production", "patents", "events", "projects")
                ):
                    recorder.add("private_profile_with_content", profile_id)
                if position % self.progress_every == 0:
                    progress = {
                        "position": position,
                        "anomaly_counts": dict(sorted(recorder.counts.items())),
                        "content_counts": dict(sorted(totals.items())),
                        "last_profile_id": profile_id,
                    }
                    self.runs.update_one(
                        {"_id": self.audit_name},
                        {"$set": {"progress": progress, "last_progress_at": utc_now()}},
                    )
                    print(
                        f"{utc_now().isoformat()} INFO: CVLAC audit position={position} "
                        f"anomalies={sum(recorder.counts.values())}",
                        flush=True,
                    )
            missing_ids = set(raw_hashes) - seen
            for profile_id in sorted(missing_ids)[: self.anomaly_example_limit]:
                recorder.add("missing_normalized_profile", profile_id)
            # Preserve the exact total even though only bounded examples are stored.
            recorder.counts["missing_normalized_profile"] = len(missing_ids)
            source_count = len(raw_hashes)
            destination_count = len(seen)
            error_count = self.db[str(config["error_collection"])].count_documents({})
            critical_kinds = {
                "missing_normalized_profile",
                "unknown_profile",
                "source_hash_mismatch",
                "source_run_mismatch",
                "parser_version_mismatch",
                "profile_status_mismatch",
                "production_count_mismatch",
                "patents_count_mismatch",
                "events_count_mismatch",
                "projects_count_mismatch",
            }
            critical_count = error_count + sum(
                recorder.counts.get(kind, 0) for kind in critical_kinds
            )
            status = "passed" if critical_count == 0 and source_count == destination_count else "failed"
            summary = {
                "source_profiles": source_count,
                "destination_profiles": destination_count,
                "stored_parser_errors": error_count,
                "profile_status_counts": dict(sorted(status_counts.items())),
                "parser_version_counts": dict(sorted(parser_counts.items())),
                "content_counts": dict(sorted(totals.items())),
                "bibliographic_metadata_counts": dict(
                    sorted(bibliographic_totals.items())
                ),
                "type_impactu_distribution": [
                    {"value": key, "count": value}
                    for key, value in sorted(distributions.items())
                ],
                "anomaly_counts": dict(sorted(recorder.counts.items())),
                "critical_anomalies": critical_count,
            }
            self.runs.update_one(
                {"_id": self.audit_name},
                {"$set": {"status": status, "summary": summary, "finished_at": utc_now()}},
            )
            return {"audit_name": self.audit_name, "status": status, **summary}
        except Exception as error:
            self.runs.update_one(
                {"_id": self.audit_name},
                {"$set": {"status": "failed_to_run", "error": f"{type(error).__name__}: {error}", "failed_at": utc_now()}},
            )
            raise


class GruplacNormalizationAuditor:
    """Audit complete GrupLAC coverage and the confirmed incomplete profiles."""

    RUNS_COLLECTION = GRUPLAC_AUDIT_RUNS

    def __init__(
        self,
        db,
        *,
        audit_name: str,
        recognized_groups_collection: str,
        raw_collection: str,
        state_collection: str,
        normalized_collection: str,
        verification_run_name: str | None = None,
        anomaly_example_limit: int = 100,
    ):
        _validate_name(audit_name, "audit_name")
        if verification_run_name:
            _validate_name(verification_run_name, "verification_run_name")
        self.db = db
        self.audit_name = audit_name
        self.recognized_groups_collection = recognized_groups_collection
        self.raw_collection = raw_collection
        self.state_collection = state_collection
        self.normalized_collection = normalized_collection
        self.verification_run_name = verification_run_name
        self.anomaly_example_limit = anomaly_example_limit
        self.anomaly_collection = f"{audit_name}_anomalies"
        self.runs = db[self.RUNS_COLLECTION]

    @property
    def config(self) -> dict[str, Any]:
        return {
            "recognized_groups_collection": self.recognized_groups_collection,
            "raw_collection": self.raw_collection,
            "state_collection": self.state_collection,
            "normalized_collection": self.normalized_collection,
            "verification_run_name": self.verification_run_name,
            "expected_parser_version": GRUPLAC_PARSER_VERSION,
        }

    def run(self) -> dict[str, Any]:
        available = set(self.db.list_collection_names())
        required = {
            self.recognized_groups_collection,
            self.raw_collection,
            self.state_collection,
            self.normalized_collection,
        }
        missing = sorted(required - available)
        if missing:
            raise ValueError(f"required GrupLAC collections are missing: {missing}")
        existing = self.runs.find_one({"_id": self.audit_name})
        if existing and existing.get("config") != self.config:
            raise ValueError(f"audit {self.audit_name!r} exists with a different config")
        now = utc_now()
        self.runs.update_one(
            {"_id": self.audit_name},
            {
                "$setOnInsert": {"created_at": now, "config": self.config},
                "$set": {"status": "running", "last_started_at": now},
                "$inc": {"execution_attempts": 1},
                "$unset": {"error": ""},
            },
            upsert=True,
        )
        anomaly_collection = self.db[self.anomaly_collection]
        anomaly_collection.delete_many({"audit_name": self.audit_name})
        recorder = _AnomalyRecorder(
            anomaly_collection, self.audit_name, self.anomaly_example_limit
        )
        expected_ids = {
            str(item.get("codigo_grupo") or item.get("group_code") or item.get("_id") or "").upper()
            for item in self.db[self.recognized_groups_collection].find(
                {"url_gruplac": {"$type": "string", "$ne": ""}},
                {"codigo_grupo": 1, "group_code": 1},
            )
        }
        expected_ids.discard("")
        raw_ids = {str(value).upper() for value in self.db[self.raw_collection].distinct("_id")}
        raw_hashes = _raw_hash_index(self.db[self.raw_collection])
        normalized_ids = {
            str(value).upper() for value in self.db[self.normalized_collection].distinct("_id")
        }
        for kind, values in (
            ("missing_raw_group", expected_ids - raw_ids),
            ("unexpected_raw_group", raw_ids - expected_ids),
            ("missing_normalized_group", raw_ids - normalized_ids),
            ("unexpected_normalized_group", normalized_ids - raw_ids),
        ):
            recorder.counts[kind] = len(values)
            for value in sorted(values)[: self.anomaly_example_limit]:
                recorder.add(kind, value)
            recorder.counts[kind] = len(values)
        state_counts = {
            str(item["_id"]): int(item["count"])
            for item in self.db[self.state_collection].aggregate(
                [
                    {"$match": {"kind": "gruplac"}},
                    {"$group": {"_id": "$status", "count": {"$sum": 1}}},
                ]
            )
        }
        expected_status = {
            str(item.get("source_id") or "").upper(): str(
                item.get("status") or "unknown"
            )
            for item in self.db[self.state_collection].find(
                {"kind": "gruplac"}, {"source_id": 1, "status": 1}
            )
        }
        totals: Counter = Counter()
        distributions: Counter = Counter()
        bibliographic_totals: Counter = Counter()
        parser_counts: Counter = Counter()
        for document in self.db[self.normalized_collection].find(
            {}, {"group_code": 1, "group_name": 1, "group_status": 1, "parser": 1, "source": 1, "members_count": 1, "members": 1, "production_count": 1, "production": 1}
        ):
            code = str(document["_id"])
            parser_version = str(
                ((document.get("parser") or {}).get("version")) or "missing"
            )
            parser_counts[parser_version] += 1
            if parser_version != GRUPLAC_PARSER_VERSION:
                recorder.add("parser_version_mismatch", code, parser_version)
            source_hash = str(
                ((document.get("source") or {}).get("content_sha256")) or ""
            )
            if source_hash != raw_hashes.get(code):
                recorder.add(
                    "source_hash_mismatch",
                    code,
                    {"expected": raw_hashes.get(code), "actual": source_hash},
                )
            group_status = str(document.get("group_status") or "missing")
            if group_status != expected_status.get(code, "unknown"):
                recorder.add(
                    "group_status_mismatch",
                    code,
                    {"expected": expected_status.get(code), "actual": group_status},
                )
            if str(document.get("group_code") or "").upper() != code.upper():
                recorder.add("group_code_mismatch", code, document.get("group_code"))
            if not str(document.get("group_name") or "").strip():
                recorder.add("missing_group_name", code)
            members = document.get("members")
            if not isinstance(members, list):
                recorder.add("members_not_list", code)
                members = []
            if document.get("members_count") != len(members):
                recorder.add(
                    "members_count_mismatch",
                    code,
                    {"declared": document.get("members_count"), "actual": len(members)},
                )
            for member in members:
                if not re.fullmatch(r"\d{10}", str((member or {}).get("cod_rh") or "")):
                    recorder.add("invalid_member_cod_rh", code, (member or {}).get("cod_rh"))
            production_count = _audit_records(
                recorder, code, "production", document.get("production"), distributions
            )
            totals["production"] += production_count
            _count_bibliographic_metadata(
                document.get("production"), bibliographic_totals
            )
            totals["members"] += len(members)
            if document.get("production_count") != production_count:
                recorder.add(
                    "production_count_mismatch",
                    code,
                    {"declared": document.get("production_count"), "actual": production_count},
                )
        verification = {}
        if self.verification_run_name:
            verification_run = self.db["scienti_gruplac_verification_runs"].find_one(
                {"_id": self.verification_run_name}
            )
            if not verification_run:
                recorder.add("missing_verification_run", self.verification_run_name)
            else:
                verification = {
                    "run_name": self.verification_run_name,
                    "status": verification_run.get("status"),
                    "summary": verification_run.get("summary"),
                }
                if verification_run.get("status") != "complete":
                    recorder.add(
                        "verification_run_not_complete",
                        self.verification_run_name,
                        verification_run.get("status"),
                    )
                verification_config = verification_run.get("config") or {}
                expected_verification_config = {
                    "source_raw_collection": self.raw_collection,
                    "source_state_collection": self.state_collection,
                    "normalized_collection": self.normalized_collection,
                }
                for key, expected_value in expected_verification_config.items():
                    if verification_config.get(key) != expected_value:
                        recorder.add(
                            "verification_source_mismatch",
                            self.verification_run_name,
                            {
                                "field": key,
                                "expected": expected_value,
                                "actual": verification_config.get(key),
                            },
                        )
                incomplete_ids = {
                    str(item.get("source_id") or "").upper()
                    for item in self.db[self.state_collection].find(
                        {"kind": "gruplac", "status": "incomplete"}, {"source_id": 1}
                    )
                }
                target_collection = str(verification_config.get("target_collection") or "")
                result_collection = str(verification_config.get("result_collection") or "")
                target_ids = (
                    {
                        str(value).upper()
                        for value in self.db[target_collection].distinct("_id")
                    }
                    if target_collection in available
                    else set()
                )
                confirmed_ids = (
                    {
                        str(value).upper()
                        for value in self.db[result_collection].distinct(
                            "_id", {"outcome": "confirmed_incomplete", "content_unchanged": True}
                        )
                    }
                    if result_collection in available
                    else set()
                )
                for kind, values in (
                    ("incomplete_not_frozen_for_verification", incomplete_ids - target_ids),
                    ("verification_target_no_longer_incomplete", target_ids - incomplete_ids),
                    ("incomplete_not_confirmed", incomplete_ids - confirmed_ids),
                ):
                    recorder.counts[kind] = len(values)
                    for value in sorted(values)[: self.anomaly_example_limit]:
                        recorder.add(kind, value)
                    recorder.counts[kind] = len(values)
                verification.update(
                    {
                        "current_incomplete": len(incomplete_ids),
                        "frozen_targets": len(target_ids),
                        "confirmed_unchanged": len(confirmed_ids),
                    }
                )
        critical_kinds = {
            "missing_raw_group",
            "unexpected_raw_group",
            "missing_normalized_group",
            "unexpected_normalized_group",
            "group_code_mismatch",
            "members_count_mismatch",
            "production_count_mismatch",
            "production_title_period_misalignment",
            "production_timeline_missing_start_date",
            "production_timeline_type_mismatch",
            "parser_version_mismatch",
            "source_hash_mismatch",
            "group_status_mismatch",
            "missing_verification_run",
            "verification_run_not_complete",
            "verification_source_mismatch",
            "incomplete_not_frozen_for_verification",
            "verification_target_no_longer_incomplete",
            "incomplete_not_confirmed",
        }
        error_collection = f"{self.normalized_collection}_errors"
        error_count = self.db[error_collection].count_documents({})
        critical_count = error_count + sum(
            recorder.counts.get(kind, 0) for kind in critical_kinds
        )
        status = "passed" if critical_count == 0 else "failed"
        summary = {
            "expected_groups": len(expected_ids),
            "raw_groups": len(raw_ids),
            "normalized_groups": len(normalized_ids),
            "stored_parser_errors": error_count,
            "download_status_counts": dict(sorted(state_counts.items())),
            "parser_version_counts": dict(sorted(parser_counts.items())),
            "content_counts": dict(sorted(totals.items())),
            "bibliographic_metadata_counts": dict(
                sorted(bibliographic_totals.items())
            ),
            "type_impactu_cardinality": len(distributions),
            "type_impactu_distribution_top_100": [
                {"value": key, "count": value}
                for key, value in sorted(
                    distributions.items(), key=lambda item: (-item[1], item[0])
                )[:100]
            ],
            "verification": verification,
            "anomaly_counts": dict(sorted(recorder.counts.items())),
            "critical_anomalies": critical_count,
        }
        self.runs.update_one(
            {"_id": self.audit_name},
            {"$set": {"status": status, "summary": summary, "finished_at": utc_now()}},
        )
        return {"audit_name": self.audit_name, "status": status, **summary}
