"""Resumable, exhaustive comparison of two strict Scienti entity versions."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Callable, Iterator

from pymongo import ASCENDING

from yuku.scienti_entities import (
    BOGOTA,
    ENTITY_KEYS,
    ENTITY_NORMALIZER_VERSION,
    ENTITY_RUNS,
    _resolve_thesis_roles,
    _sanitize_event_record,
    _timestamp,
    _year,
    exact_key,
)


COMPARISON_VERSION = "scienti-entity-version-comparison-v2"
ENTITY_COMPARISONS = "scienti_entity_version_comparisons"
ENTITY_COMPARISON_ANOMALIES = "scienti_entity_version_comparison_anomalies"
ENTITY_COMPARISON_TRANSITIONS = "scienti_entity_version_comparison_transitions"
RUN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
DATE_FIELDS = {
    "projects": {"date_init": "start_date", "date_end": "end_date"},
    "events": {"date_held": "start_date"},
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _old_timestamp(value: Any) -> int | None:
    """Reproduce the v1 timestamp behavior exactly for comparison."""
    text = str(value or "").strip()
    if not text:
        return None
    parsed: datetime | None = None
    for pattern in ("%Y-%m-%d", "%Y/%m", "%Y/%m/%d", "%Y-%m"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=BOGOTA)
            break
        except ValueError:
            pass
    if parsed is None:
        year = _year(text)
        if year:
            parsed = datetime(year, 1, 1, tzinfo=BOGOTA)
    return int(parsed.timestamp()) if parsed else None


def _expected_date(
    document: dict[str, Any], raw_field: str, parser: Callable[[Any], int | None]
) -> int | None:
    occurrences = ((document.get("source_metadata") or {}).get("occurrences") or [])
    for occurrence in occurrences:
        raw = (occurrence.get("metadata") or {}).get(raw_field)
        parsed = parser(raw)
        if parsed is not None:
            return parsed
    return None


def _normalize_updated(document: dict[str, Any]) -> None:
    document["updated"] = [
        {key: value for key, value in item.items() if key != "time"}
        for item in document.get("updated") or []
    ]


def _difference_paths(left: Any, right: Any, path: str = "", limit: int = 25) -> list[str]:
    if left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        output: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in left or key not in right:
                output.append(child)
            else:
                output.extend(_difference_paths(left[key], right[key], child, limit - len(output)))
            if len(output) >= limit:
                break
        return output
    return [path or "$"]


def _next(cursor: Iterator[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        return next(cursor)
    except StopIteration:
        return None


class ScientiEntityVersionComparator:
    """Compare every `_id` and field while allowing only declared v2 changes."""

    def __init__(
        self,
        db,
        *,
        comparison_name: str,
        old_run_name: str,
        new_run_name: str,
        progress_every: int = 10000,
        batch_size: int = 1000,
        example_limit: int = 100,
    ):
        for label, value in {
            "comparison_name": comparison_name,
            "old_run_name": old_run_name,
            "new_run_name": new_run_name,
        }.items():
            if not RUN_NAME_RE.fullmatch(value or ""):
                raise ValueError(f"{label} must contain only letters, numbers and underscores")
        if progress_every < 1 or batch_size < 1 or example_limit < 1:
            raise ValueError("progress_every, batch_size and example_limit must be positive")
        self.db = db
        self.comparison_name = comparison_name
        self.old_run_name = old_run_name
        self.new_run_name = new_run_name
        self.progress_every = int(progress_every)
        self.batch_size = int(batch_size)
        self.example_limit = int(example_limit)
        self.runs = db[ENTITY_COMPARISONS]
        self.anomalies = db[ENTITY_COMPARISON_ANOMALIES]
        self.transitions = db[ENTITY_COMPARISON_TRANSITIONS]
        self.old_destinations: dict[str, str] = {}
        self.new_destinations: dict[str, str] = {}
        self.config: dict[str, Any] = {}
        self.config_hash = ""
        self.metrics: dict[str, Counter] = defaultdict(Counter)
        self.findings: Counter = Counter()
        self.example_counts: Counter = Counter()
        self.transition_profile = "legacy_month_dates"

    def _load_sources(self) -> None:
        old_run = self.db[ENTITY_RUNS].find_one({"_id": self.old_run_name})
        new_run = self.db[ENTITY_RUNS].find_one({"_id": self.new_run_name})
        for label, run in (("old", old_run), ("new", new_run)):
            if not run or run.get("status") != "complete":
                raise RuntimeError(f"{label} entity run is missing or incomplete")
        old_config = old_run.get("config") or {}
        new_config = new_run.get("config") or {}
        self.old_destinations = dict(old_config.get("destinations") or {})
        self.new_destinations = dict(new_config.get("destinations") or {})
        if set(self.old_destinations) != set(ENTITY_KEYS) or set(self.new_destinations) != set(ENTITY_KEYS):
            raise RuntimeError("both runs must define the four strict entity destinations")
        counts = {
            entity: {
                "old": self.db[self.old_destinations[entity]].count_documents({}),
                "new": self.db[self.new_destinations[entity]].count_documents({}),
            }
            for entity in ENTITY_KEYS
        }
        if any(not values["old"] or not values["new"] for values in counts.values()):
            raise RuntimeError(f"entity comparison sources cannot be empty: {counts}")
        old_version = str(old_config.get("normalizer_version") or "")
        new_version = str(new_config.get("normalizer_version") or "")
        if (
            old_version == "scienti-entity-normalizer-v2"
            and new_version == "scienti-entity-normalizer-v3"
        ):
            self.transition_profile = "semantic_hardening_v3"
        self.config = {
            "comparison_version": COMPARISON_VERSION,
            "old_run_name": self.old_run_name,
            "new_run_name": self.new_run_name,
            "old_destinations": self.old_destinations,
            "new_destinations": self.new_destinations,
            "old_config_hash": old_run.get("config_hash"),
            "new_config_hash": new_run.get("config_hash"),
            "old_normalizer_version": old_version,
            "new_normalizer_version": new_version,
            "transition_profile": self.transition_profile,
            "counts": counts,
        }
        self.config_hash = sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

    def _prepare(self) -> dict[str, Any]:
        self._load_sources()
        previous = self.runs.find_one({"_id": self.comparison_name})
        if previous and previous.get("config_hash") != self.config_hash:
            raise ValueError("comparison name already exists with different immutable sources")
        if not previous:
            self.anomalies.delete_many({"comparison_name": self.comparison_name})
            self.transitions.delete_many({"comparison_name": self.comparison_name})
            self.runs.insert_one(
                {
                    "_id": self.comparison_name,
                    "status": "pending",
                    "config": self.config,
                    "config_hash": self.config_hash,
                    "created_at": utc_now(),
                    "progress": {"entity": ENTITY_KEYS[0], "last_id": ""},
                    "state": {},
                }
            )
            previous = self.runs.find_one({"_id": self.comparison_name}) or {}
        state = previous.get("state") or {}
        self.findings = Counter({key: int(value) for key, value in (state.get("findings") or {}).items()})
        self.metrics = defaultdict(
            Counter,
            {
                entity: Counter({key: int(value) for key, value in values.items()})
                for entity, values in (state.get("metrics") or {}).items()
            },
        )
        self.example_counts = Counter(
            {
                str(value["_id"]): int(value["count"])
                for value in self.anomalies.aggregate(
                    [
                        {"$match": {"comparison_name": self.comparison_name}},
                        {"$group": {"_id": "$kind", "count": {"$sum": 1}}},
                    ]
                )
            }
        )
        if previous.get("status") in {"passed", "failed"}:
            return previous
        self.anomalies.create_index([("comparison_name", ASCENDING), ("kind", ASCENDING)])
        self.transitions.create_index(
            [
                ("comparison_name", ASCENDING),
                ("entity", ASCENDING),
                ("side", ASCENDING),
                ("document_id", ASCENDING),
            ],
            unique=True,
        )
        self.runs.update_one(
            {"_id": self.comparison_name},
            {
                "$set": {"status": "running", "last_started_at": utc_now()},
                "$inc": {"execution_attempts": 1},
                "$unset": {"error": ""},
            },
        )
        return previous

    def _state(self) -> dict[str, Any]:
        return {
            "findings": dict(sorted(self.findings.items())),
            "metrics": {
                entity: dict(sorted(values.items()))
                for entity, values in sorted(self.metrics.items())
            },
        }

    def _finding(self, entity: str, kind: str, identifier: str, detail: Any = None) -> None:
        key = f"{entity}.{kind}"
        self.findings[key] += 1
        if self.example_counts[key] >= self.example_limit:
            return
        example_id = sha256(
            f"{self.comparison_name}|{key}|{identifier}".encode()
        ).hexdigest()
        result = self.anomalies.replace_one(
            {"_id": example_id},
            {
                "_id": example_id,
                "comparison_name": self.comparison_name,
                "entity": entity,
                "kind": key,
                "document_id": identifier,
                "detail": detail,
                "recorded_at": utc_now(),
            },
            upsert=True,
        )
        if result.upserted_id is not None:
            self.example_counts[key] += 1

    def _compare_dates(
        self, entity: str, identifier: str, old: dict[str, Any], new: dict[str, Any]
    ) -> None:
        for field, raw_field in DATE_FIELDS.get(entity, {}).items():
            old_value = old.pop(field, None)
            new_value = new.pop(field, None)
            expected_old = _expected_date(old, raw_field, _old_timestamp)
            expected_new = _expected_date(new, raw_field, _timestamp)
            if old_value != expected_old:
                self._finding(
                    entity, "old_date_not_reproducible", identifier,
                    {"field": field, "actual": old_value, "expected": expected_old},
                )
            if new_value != expected_new:
                self._finding(
                    entity, "new_date_not_reproducible", identifier,
                    {"field": field, "actual": new_value, "expected": expected_new},
                )
            metric = f"{field}.changed_expected" if old_value != new_value else f"{field}.unchanged"
            self.metrics[entity][metric] += 1

    @staticmethod
    def _finding_keys(values: list[dict[str, Any]]) -> list[str]:
        return sorted(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            for value in values or []
        )

    def _compare_v3_work(
        self, identifier: str, old: dict[str, Any], new: dict[str, Any]
    ) -> None:
        expected_authors, expected_resolutions = _resolve_thesis_roles(
            old.get("authors") or []
        )
        if new.get("authors") != expected_authors:
            self._finding(
                "works", "invalid_thesis_role_transition", identifier,
                {
                    "expected_authors": expected_authors,
                    "actual_authors": new.get("authors") or [],
                },
            )
        if int(new.get("author_count") or 0) != len(expected_authors):
            self._finding(
                "works", "invalid_thesis_author_count_transition", identifier,
                {"expected": len(expected_authors), "actual": new.get("author_count")},
            )
        actual_resolutions = (new.get("source_metadata") or {}).get("role_resolutions") or []
        expected_names = {exact_key(value.get("name")) for value in expected_resolutions}
        actual_names = {exact_key(value.get("name")) for value in actual_resolutions}
        if actual_names != expected_names or any(
            value.get("rule") != "student_precedence_exact_name"
            for value in actual_resolutions
        ):
            self._finding(
                "works", "invalid_thesis_role_resolution_evidence", identifier,
                {"expected_names": sorted(expected_names), "actual": actual_resolutions},
            )
        else:
            self.metrics["works"]["exact_role_resolutions_verified"] += len(actual_names)
        old["authors"] = deepcopy(expected_authors)
        old["author_count"] = len(expected_authors)
        (old.get("source_metadata") or {}).pop("role_resolutions", None)
        (new.get("source_metadata") or {}).pop("role_resolutions", None)
        for occurrence in (new.get("source_metadata") or {}).get("occurrences") or []:
            findings = occurrence.pop("normalization_findings", [])
            if any(
                value != {
                    "kind": "advisor_removed_exact_student_match",
                    "field": "authors",
                }
                for value in findings
            ):
                self._finding(
                    "works", "unexpected_thesis_normalization_finding",
                    identifier, findings,
                )

    def _compare_v3_event(
        self, identifier: str, old: dict[str, Any], new: dict[str, Any]
    ) -> None:
        old_occurrences = {
            str(value.get("id") or ""): value
            for value in (old.get("source_metadata") or {}).get("occurrences") or []
        }
        new_occurrences = {
            str(value.get("id") or ""): value
            for value in (new.get("source_metadata") or {}).get("occurrences") or []
        }
        if set(old_occurrences) != set(new_occurrences):
            self._finding(
                "events", "unexpected_occurrence_redistribution_same_identity",
                identifier,
                {
                    "only_old": sorted(set(old_occurrences) - set(new_occurrences)),
                    "only_new": sorted(set(new_occurrences) - set(old_occurrences)),
                },
            )
            return
        for occurrence_id, old_occurrence in old_occurrences.items():
            new_occurrence = new_occurrences[occurrence_id]
            expected_metadata, expected_findings = _sanitize_event_record(
                old_occurrence.get("metadata") or {}
            )
            if new_occurrence.get("metadata") != expected_metadata:
                self._finding(
                    "events", "invalid_event_metadata_transition", identifier,
                    {
                        "occurrence_id": occurrence_id,
                        "expected": expected_metadata,
                        "actual": new_occurrence.get("metadata") or {},
                    },
                )
            actual_findings = new_occurrence.get("normalization_findings") or []
            if self._finding_keys(actual_findings) != self._finding_keys(expected_findings):
                self._finding(
                    "events", "invalid_event_finding_transition", identifier,
                    {
                        "occurrence_id": occurrence_id,
                        "expected": expected_findings,
                        "actual": actual_findings,
                    },
                )
            old_occurrence["metadata"] = expected_metadata
            old_occurrence.pop("normalization_findings", None)
            new_occurrence.pop("normalization_findings", None)
        (old.get("source_metadata") or {}).pop("role_resolutions", None)
        (new.get("source_metadata") or {}).pop("role_resolutions", None)
        # The specialized semantic audit verifies that these consolidated
        # values match one of the sanitized source occurrences.
        for field in ("date_held", "year_held"):
            old.pop(field, None)
            new.pop(field, None)

    def _compare_v3_transition(
        self, entity: str, identifier: str, old: dict[str, Any], new: dict[str, Any]
    ) -> None:
        if entity == "works":
            self._compare_v3_work(identifier, old, new)
        elif entity == "events":
            self._compare_v3_event(identifier, old, new)
        else:
            (old.get("source_metadata") or {}).pop("role_resolutions", None)
            (new.get("source_metadata") or {}).pop("role_resolutions", None)

    def _record_reidentified(
        self, entity: str, side: str, document: dict[str, Any]
    ) -> None:
        identifier = str(document.get("_id") or "")
        self.transitions.replace_one(
            {
                "comparison_name": self.comparison_name,
                "entity": entity,
                "side": side,
                "document_id": identifier,
            },
            {
                "comparison_name": self.comparison_name,
                "entity": entity,
                "side": side,
                "document_id": identifier,
                "document": document,
            },
            upsert=True,
        )
        self.metrics[entity][f"reidentified_{side}_documents"] += 1

    def _audit_reidentified_events(self) -> None:
        records = list(
            self.transitions.find(
                {"comparison_name": self.comparison_name, "entity": "events"}
            )
        )
        if not records:
            return
        by_side: dict[str, dict[str, tuple[dict[str, Any], dict[str, Any]]]] = {
            "old": {}, "new": {}
        }
        for record in records:
            document = record.get("document") or {}
            side = str(record.get("side") or "")
            for occurrence in (document.get("source_metadata") or {}).get("occurrences") or []:
                occurrence_id = str(occurrence.get("id") or "")
                if occurrence_id in by_side[side]:
                    self._finding(
                        "events", "duplicate_reidentified_occurrence", occurrence_id, side
                    )
                by_side[side][occurrence_id] = (document, occurrence)
        only_old = set(by_side["old"]) - set(by_side["new"])
        only_new = set(by_side["new"]) - set(by_side["old"])
        for occurrence_id in sorted(only_old):
            self._finding("events", "lost_reidentified_occurrence", occurrence_id)
        for occurrence_id in sorted(only_new):
            self._finding("events", "added_reidentified_occurrence", occurrence_id)
        stable_fields = (
            "source_kind", "source_collection", "source_id", "record_index",
            "source_section", "product_type", "route_rule", "url", "validated",
        )
        for occurrence_id in sorted(set(by_side["old"]) & set(by_side["new"])):
            old_document, old_occurrence = by_side["old"][occurrence_id]
            new_document, new_occurrence = by_side["new"][occurrence_id]
            if any(old_occurrence.get(key) != new_occurrence.get(key) for key in stable_fields):
                self._finding(
                    "events", "reidentified_source_evidence_changed", occurrence_id
                )
            expected_metadata, expected_findings = _sanitize_event_record(
                old_occurrence.get("metadata") or {}
            )
            if new_occurrence.get("metadata") != expected_metadata:
                self._finding(
                    "events", "invalid_reidentified_event_metadata", occurrence_id,
                    {"expected": expected_metadata, "actual": new_occurrence.get("metadata")},
                )
            if self._finding_keys(new_occurrence.get("normalization_findings") or []) != self._finding_keys(expected_findings):
                self._finding(
                    "events", "invalid_reidentified_event_findings", occurrence_id,
                    {"expected": expected_findings, "actual": new_occurrence.get("normalization_findings")},
                )
            new_meta = new_document.get("source_metadata") or {}
            if (
                new_meta.get("identity_rule") != "source_scoped_insufficient_anchors"
                or not str(new_meta.get("identity_key") or "").startswith("source|events|")
            ):
                self._finding(
                    "events", "invalid_reidentified_event_identity", occurrence_id,
                    {
                        "identity_rule": new_meta.get("identity_rule"),
                        "identity_key": new_meta.get("identity_key"),
                    },
                )
            if (old_document.get("titles") or []) != (new_document.get("titles") or []):
                self._finding("events", "reidentified_event_title_changed", occurrence_id)
        self.metrics["events"]["reidentified_occurrences_verified"] += len(
            set(by_side["old"]) & set(by_side["new"])
        )

    def _compare_document(
        self, entity: str, old: dict[str, Any], new: dict[str, Any]
    ) -> None:
        identifier = str(old.get("_id") or new.get("_id") or "")
        self.metrics[entity]["compared"] += 1
        old_meta = old.get("source_metadata") or {}
        new_meta = new.get("source_metadata") or {}
        old_version = old_meta.pop("normalizer_version", None)
        new_version = new_meta.pop("normalizer_version", None)
        if self.transition_profile == "semantic_hardening_v3":
            if old_version != "scienti-entity-normalizer-v2":
                self._finding(
                    entity, "invalid_old_normalizer_version", identifier, old_version
                )
            else:
                self.metrics[entity]["old_normalizer_version_verified"] += 1
        elif old_version is not None:
            self._finding(entity, "unexpected_old_normalizer_version", identifier, old_version)
        if new_version != ENTITY_NORMALIZER_VERSION:
            self._finding(entity, "invalid_new_normalizer_version", identifier, new_version)
        else:
            self.metrics[entity]["normalizer_version_verified"] += 1
        if self.transition_profile == "semantic_hardening_v3":
            self._compare_v3_transition(entity, identifier, old, new)
        else:
            self._compare_dates(entity, identifier, old, new)
        _normalize_updated(old)
        _normalize_updated(new)
        if old != new:
            self._finding(
                entity,
                "unexpected_metadata_difference",
                identifier,
                {"paths": _difference_paths(old, new)},
            )

    def _save(self, entity: str, last_id: str, *, complete: bool = False) -> None:
        position = ENTITY_KEYS.index(entity)
        next_entity = ENTITY_KEYS[position + 1] if complete and position + 1 < len(ENTITY_KEYS) else entity
        progress = {
            "entity": next_entity,
            "last_id": "" if complete else last_id,
            "completed_entities": list(ENTITY_KEYS[: position + 1]) if complete else list(ENTITY_KEYS[:position]),
            "documents_compared": sum(values.get("compared", 0) for values in self.metrics.values()),
            "findings": int(sum(self.findings.values())),
        }
        self.runs.update_one(
            {"_id": self.comparison_name},
            {"$set": {"status": "running", "progress": progress, "state": self._state(), "last_progress_at": utc_now()}},
        )
        print(
            f"{utc_now().isoformat()} INFO: entity comparison entity={entity} "
            f"compared={self.metrics[entity].get('compared', 0)} "
            f"findings={sum(self.findings.values())} last={last_id}",
            flush=True,
        )

    def _compare_entity(self, entity: str, last_id: str) -> None:
        query = {"_id": {"$gt": last_id}} if last_id else {}
        old_cursor = iter(
            self.db[self.old_destinations[entity]].find(query).sort("_id", ASCENDING).batch_size(self.batch_size)
        )
        new_cursor = iter(
            self.db[self.new_destinations[entity]].find(query).sort("_id", ASCENDING).batch_size(self.batch_size)
        )
        old = _next(old_cursor)
        new = _next(new_cursor)
        processed_since_save = 0
        current_id = last_id
        while old is not None or new is not None:
            old_id = str(old.get("_id")) if old is not None else None
            new_id = str(new.get("_id")) if new is not None else None
            if new is None or (old is not None and old_id < new_id):
                current_id = old_id or current_id
                if self.transition_profile == "semantic_hardening_v3" and entity == "events":
                    self._record_reidentified(entity, "old", old)
                else:
                    self.metrics[entity]["missing_in_new"] += 1
                    self._finding(entity, "missing_in_new", current_id)
                old = _next(old_cursor)
            elif old is None or new_id < old_id:
                current_id = new_id or current_id
                if self.transition_profile == "semantic_hardening_v3" and entity == "events":
                    self._record_reidentified(entity, "new", new)
                else:
                    self.metrics[entity]["missing_in_old"] += 1
                    self._finding(entity, "missing_in_old", current_id)
                new = _next(new_cursor)
            else:
                current_id = old_id or current_id
                self._compare_document(entity, old, new)
                old = _next(old_cursor)
                new = _next(new_cursor)
            processed_since_save += 1
            if processed_since_save >= self.progress_every:
                self._save(entity, current_id)
                processed_since_save = 0
        self._save(entity, current_id, complete=True)
        if self.transition_profile == "semantic_hardening_v3" and entity == "events":
            self._audit_reidentified_events()

    def _summary(self) -> dict[str, Any]:
        findings = int(sum(self.findings.values()))
        return {
            "comparison_name": self.comparison_name,
            "status": "passed" if findings == 0 else "failed",
            "comparison_version": COMPARISON_VERSION,
            "transition_profile": self.transition_profile,
            "old_run_name": self.old_run_name,
            "new_run_name": self.new_run_name,
            "collection_counts": self.config["counts"],
            "metrics": {
                entity: dict(sorted(self.metrics[entity].items())) for entity in ENTITY_KEYS
            },
            "finding_counts": dict(sorted(self.findings.items())),
            "critical_findings": findings,
            "examples_collection": ENTITY_COMPARISON_ANOMALIES,
        }

    def run(self) -> dict[str, Any]:
        previous = self._prepare()
        if previous.get("status") in {"passed", "failed"}:
            return previous.get("summary") or {}
        progress = previous.get("progress") or {}
        start_entity = str(progress.get("entity") or ENTITY_KEYS[0])
        start_position = ENTITY_KEYS.index(start_entity)
        completed_entities = set(progress.get("completed_entities") or [])
        try:
            for position, entity in enumerate(ENTITY_KEYS[start_position:], start=start_position):
                if entity in completed_entities:
                    continue
                last_id = str(progress.get("last_id") or "") if position == start_position else ""
                self._compare_entity(entity, last_id)
            summary = self._summary()
            self.runs.update_one(
                {"_id": self.comparison_name},
                {"$set": {"status": summary["status"], "summary": summary, "state": self._state(), "finished_at": utc_now()}},
            )
            if summary["status"] == "passed":
                self.transitions.delete_many({"comparison_name": self.comparison_name})
            return summary
        except Exception as error:
            self.runs.update_one(
                {"_id": self.comparison_name},
                {"$set": {"status": "failed_to_run", "error": f"{type(error).__name__}: {error}", "failed_at": utc_now()}},
            )
            raise
