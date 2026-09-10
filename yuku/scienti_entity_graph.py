"""Conservative exact graphs for normalized Scienti projects and patents."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from time import time
from typing import Any, Iterable

from pymongo import ASCENDING, ReplaceOne

from yuku.cvlac_work_graph import is_generic_title
from yuku.scienti_entities import (
    ENTITY_AUDITS,
    ENTITY_RUNS,
    merge_entity_documents,
    registration_key,
)
from yuku.scienti_routing import exact_key


ENTITY_GRAPH_RUNS = "scienti_entity_graph_runs"
ENTITY_GRAPH_REVIEWS = "scienti_entity_graph_reviews"
ENTITY_GRAPH_VERSIONS = {
    "projects": "scienti-project-graph-v1",
    "patents": "scienti-patent-graph-v1",
}
RUN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
COLLECTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,119}")
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")


def _unique_strings(values: Iterable[Any]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value or "").strip()})


def _first_title(document: dict[str, Any]) -> str:
    for value in document.get("titles") or []:
        if isinstance(value, dict) and str(value.get("title") or "").strip():
            return str(value["title"]).strip()
    return ""


def _year(value: Any) -> int | None:
    if isinstance(value, int) and 1900 <= value <= 2100:
        return value
    match = YEAR_RE.search(str(value or ""))
    return int(match.group(1)) if match else None


def _occurrences(document: dict[str, Any]) -> list[dict[str, Any]]:
    values = (document.get("source_metadata") or {}).get("occurrences") or []
    return [value for value in values if isinstance(value, dict)]


def _stable_anchors(document: dict[str, Any]) -> set[str]:
    anchors = {
        "author:{}".format(str(value.get("id")).zfill(10))
        for value in document.get("authors") or []
        if isinstance(value, dict)
        and str(value.get("id") or "").isdigit()
        and len(str(value.get("id") or "").zfill(10)) == 10
    }
    anchors.update(
        "group:{}".format(str(value.get("id") or "").strip().upper())
        for value in document.get("groups") or []
        if isinstance(value, dict) and str(value.get("id") or "").strip()
    )
    return anchors


def _project_type(document: dict[str, Any]) -> str:
    values = {
        exact_key((value.get("metadata") or {}).get("project_type"))
        for value in _occurrences(document)
        if (value.get("metadata") or {}).get("project_type")
    }
    if len(values) == 1:
        return next(iter(values))
    fallback = {
        exact_key(value.get("type"))
        for value in document.get("types") or []
        if isinstance(value, dict)
        and value.get("source") == "scienti"
        and value.get("level") == 1
        and value.get("type")
    }
    return next(iter(fallback)) if len(fallback) == 1 else ""


def _patent_namespace(document: dict[str, Any]) -> str:
    values = {
        exact_key(value.get("identity_namespace"))
        for value in _occurrences(document)
        if value.get("identity_namespace")
    }
    return next(iter(values)) if len(values) == 1 else ""


def _constraint_values(
    entity: str, document: dict[str, Any]
) -> dict[str, set[Any]]:
    occurrences = _occurrences(document)
    if entity == "projects":
        years = {
            value
            for value in (
                _year(document.get("year_init")),
                *(
                    _year((item.get("metadata") or {}).get("start_date"))
                    for item in occurrences
                ),
            )
            if value
        }
        return {"years": years, "registrations": set(), "countries": set()}
    years = {
        value
        for item in occurrences
        for value in (
            _year((item.get("metadata") or {}).get("year")),
            _year((item.get("metadata") or {}).get("presentation_date")),
        )
        if value
    }
    registrations = {
        value
        for item in occurrences
        if (
            value := registration_key(
                (item.get("metadata") or {}).get("registration_number")
            )
        )
    }
    countries = {
        exact_key((item.get("metadata") or {}).get("country"))
        for item in occurrences
        if (item.get("metadata") or {}).get("country")
    }
    return {
        "years": years,
        "registrations": registrations,
        "countries": countries,
    }


def graph_key_document(entity: str, document: dict[str, Any]) -> dict[str, Any]:
    """Create one bounded exact-candidate record for a normalized entity."""
    source_id = str(document.get("_id") or "")
    title = _first_title(document)
    title_key = exact_key(title)
    subtype = _project_type(document) if entity == "projects" else _patent_namespace(document)
    anchors = _stable_anchors(document)
    constraints = _constraint_values(entity, document)
    eligible = bool(
        title_key
        and subtype
        and anchors
        and not is_generic_title(title)
    )
    fingerprint = f"{entity}|{subtype}|{title_key}"
    group_key = (
        sha256(fingerprint.encode("utf-8")).hexdigest()
        if eligible
        else "source:{}".format(source_id)
    )
    return {
        "_id": source_id,
        "source_id": source_id,
        "group_key": group_key,
        "candidate_fingerprint": fingerprint if eligible else "",
        "title_key": title_key,
        "subtype": subtype,
        "anchors": sorted(anchors),
        "years": sorted(constraints["years"]),
        "registrations": sorted(constraints["registrations"]),
        "countries": sorted(constraints["countries"]),
        "identity_rule": str(
            (document.get("source_metadata") or {}).get("identity_rule") or ""
        ),
        "merge_eligible": eligible,
    }


class _UnionFind:
    def __init__(self, rows: list[dict[str, Any]]):
        self.parent = list(range(len(rows)))
        self.constraints = [
            {
                field: set(row.get(field) or [])
                for field in ("years", "registrations", "countries")
            }
            for row in rows
        ]
        self.rules: dict[int, set[str]] = defaultdict(set)

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def compatible(self, left: int, right: int) -> tuple[bool, str]:
        left_root, right_root = self.find(left), self.find(right)
        for field in ("years", "registrations", "countries"):
            values = (
                self.constraints[left_root][field]
                | self.constraints[right_root][field]
            )
            if len(values) > 1:
                return False, f"conflicting_{field}"
        return True, ""

    def union(self, left: int, right: int, rule: str) -> bool:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return False
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        for field in self.constraints[left_root]:
            self.constraints[left_root][field].update(
                self.constraints[right_root][field]
            )
        self.rules[left_root].update(self.rules.pop(right_root, set()))
        self.rules[left_root].add(rule)
        return True


class ScientiExactEntityGraphBuilder:
    """Build an exact, checkpointed project or patent graph in MongoDB."""

    def __init__(
        self,
        db,
        *,
        entity: str,
        run_name: str,
        source_collection: str,
        target_collection: str,
        entity_run_name: str = "",
        batch_size: int = 1000,
        progress_every: int = 10000,
        max_candidate_group: int = 100,
        review_limit: int = 200,
        replace: bool = False,
    ):
        if entity not in ENTITY_GRAPH_VERSIONS:
            raise ValueError("entity must be projects or patents")
        if not RUN_NAME_RE.fullmatch(run_name or ""):
            raise ValueError("invalid entity graph run name")
        for value in (source_collection, target_collection):
            if not COLLECTION_RE.fullmatch(value or "") or value.startswith("system."):
                raise ValueError("invalid entity graph collection name")
        if source_collection == target_collection:
            raise ValueError("entity graph source and target must differ")
        self.db = db
        self.entity = entity
        self.version = ENTITY_GRAPH_VERSIONS[entity]
        self.run_name = run_name
        self.source_collection = source_collection
        self.target_collection = target_collection
        self.entity_run_name = entity_run_name
        self.batch_size = max(1, int(batch_size))
        self.progress_every = max(1, int(progress_every))
        self.max_candidate_group = max(2, int(max_candidate_group))
        self.review_limit = max(0, int(review_limit))
        self.replace = bool(replace)
        safe_run = re.sub(r"[^A-Za-z0-9_]", "_", run_name)
        self.keys_name = f"__yuku_{safe_run}_{entity}_graph_keys"
        self.output_name = f"__yuku_{safe_run}_{entity}_graph_output"
        self.runs = db[ENTITY_GRAPH_RUNS]
        self.reviews = db[ENTITY_GRAPH_REVIEWS]
        self.metrics: Counter = Counter()
        self.review_stored = 0
        self.config = {
            "graph_version": self.version,
            "entity": entity,
            "source_collection": source_collection,
            "target_collection": target_collection,
            "entity_run_name": entity_run_name,
            "batch_size": self.batch_size,
            "max_candidate_group": self.max_candidate_group,
        }
        self.config_hash = sha256(
            json.dumps(self.config, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _validate_source(self) -> int:
        if self.source_collection not in self.db.list_collection_names():
            raise ValueError(f"source collection {self.source_collection!r} was not found")
        count = self.db[self.source_collection].count_documents({})
        if count < 1:
            raise ValueError("entity graph source is empty")
        if self.entity_run_name:
            run = self.db[ENTITY_RUNS].find_one({"_id": self.entity_run_name}) or {}
            destinations = (run.get("config") or {}).get("destinations") or {}
            if run.get("status") != "complete" or destinations.get(self.entity) != self.source_collection:
                raise RuntimeError("entity graph source is not a completed normalization destination")
            audit = self.db[ENTITY_AUDITS].find_one({"_id": self.entity_run_name}) or {}
            if audit.get("status") != "passed" or int(audit.get("critical_anomalies", 0) or 0):
                raise RuntimeError("entity graph source did not pass its normalization audit")
            publication = self.db["scienti_entity_publications"].find_one(
                {"_id": self.entity_run_name}
            ) or {}
            if publication.get("status") not in {"current", "superseded"}:
                raise RuntimeError("entity graph source has not been atomically published")
        return count

    def _prepare(self) -> dict[str, Any]:
        source_count = self._validate_source()
        self.review_stored = self.reviews.count_documents(
            {"run_name": self.run_name}
        )
        previous = self.runs.find_one({"_id": self.run_name})
        if self.replace:
            self.db[self.keys_name].drop()
            self.db[self.output_name].drop()
            self.db[self.target_collection].drop()
            self.reviews.delete_many({"run_name": self.run_name})
            self.runs.delete_one({"_id": self.run_name})
            previous = None
        if previous and previous.get("config_hash") != self.config_hash:
            raise ValueError("entity graph run exists with another configuration")
        if previous:
            if int(previous.get("source_count") or 0) != source_count:
                raise RuntimeError("immutable entity graph source count changed")
            return previous
        if self.target_collection in self.db.list_collection_names():
            raise ValueError("entity graph target already exists; use replace explicitly")
        document = {
            "_id": self.run_name,
            "status": "pending",
            "config": deepcopy(self.config),
            "config_hash": self.config_hash,
            "source_count": source_count,
            "created_at": datetime.now(timezone.utc),
            "stages": {},
        }
        self.runs.insert_one(document)
        return document

    @staticmethod
    def _bulk_replace(collection, documents: list[dict[str, Any]]) -> None:
        if not documents:
            return
        operations = [
            ReplaceOne({"_id": value["_id"]}, value, upsert=True)
            for value in documents
        ]
        try:
            collection.bulk_write(operations, ordered=False)
        except TypeError:
            for value in documents:
                collection.replace_one({"_id": value["_id"]}, value, upsert=True)

    def _extract_keys(self) -> dict[str, Any]:
        run = self.runs.find_one({"_id": self.run_name}) or {}
        stage = (run.get("stages") or {}).get("extract_keys") or {}
        if stage.get("status") == "complete":
            return stage.get("summary") or {}
        last_id = str(stage.get("last_id") or "")
        query = {"_id": {"$gt": last_id}} if last_id else {}
        pending: list[dict[str, Any]] = []
        processed = int(stage.get("processed") or 0)
        keys = self.db[self.keys_name]
        projection = {
            "titles": 1,
            "types": 1,
            "authors.id": 1,
            "groups.id": 1,
            "year_init": 1,
            "source_metadata.identity_rule": 1,
            "source_metadata.occurrences.identity_namespace": 1,
            "source_metadata.occurrences.metadata": 1,
        }
        for document in self.db[self.source_collection].find(
            query, projection
        ).sort("_id", ASCENDING):
            key = graph_key_document(self.entity, document)
            pending.append(key)
            processed += 1
            last_id = key["_id"]
            self.metrics["merge_eligible"] += int(key["merge_eligible"])
            self.metrics["source_scoped"] += int(not key["merge_eligible"])
            if len(pending) >= self.batch_size:
                self._bulk_replace(keys, pending)
                pending.clear()
                self.runs.update_one(
                    {"_id": self.run_name},
                    {"$set": {
                        "status": "running",
                        "stages.extract_keys.status": "running",
                        "stages.extract_keys.last_id": last_id,
                        "stages.extract_keys.processed": processed,
                        "last_progress_at": datetime.now(timezone.utc),
                    }},
                )
                if processed % self.progress_every < self.batch_size:
                    print(
                        f"{datetime.now(timezone.utc).isoformat()} INFO: "
                        f"{self.entity} graph keys={processed} last={last_id}",
                        flush=True,
                    )
        self._bulk_replace(keys, pending)
        keys.create_index([("group_key", ASCENDING), ("source_id", ASCENDING)])
        count = keys.count_documents({})
        source_count = int((self.runs.find_one({"_id": self.run_name}) or {}).get("source_count") or 0)
        if count != source_count:
            raise RuntimeError(f"entity graph key coverage mismatch: {count} != {source_count}")
        summary = {
            "keys": count,
            "merge_eligible": keys.count_documents({"merge_eligible": True}),
            "source_scoped": keys.count_documents({"merge_eligible": False}),
        }
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {
                "status": "running",
                "stages.extract_keys.status": "complete",
                "stages.extract_keys.last_id": last_id,
                "stages.extract_keys.processed": processed,
                "stages.extract_keys.summary": summary,
            }},
        )
        return summary

    def _review(self, reason: str, rows: list[dict[str, Any]]) -> None:
        self.metrics[f"review_{reason}"] += 1
        if self.review_limit <= 0:
            return
        if self.review_stored >= self.review_limit:
            return
        source_ids = sorted(str(value["source_id"]) for value in rows)[:20]
        review_id = sha256(
            f"{self.run_name}|{reason}|{'|'.join(source_ids)}".encode("utf-8")
        ).hexdigest()
        self.reviews.replace_one(
            {"_id": review_id},
            {
                "_id": review_id,
                "run_name": self.run_name,
                "entity": self.entity,
                "reason": reason,
                "source_ids": source_ids,
                "candidate_fingerprint": rows[0].get("candidate_fingerprint", ""),
            },
            upsert=True,
        )
        self.review_stored += 1

    def _components(
        self, rows: list[dict[str, Any]]
    ) -> list[tuple[list[int], list[str]]]:
        if len(rows) == 1 or not rows[0].get("merge_eligible"):
            return [([index], []) for index in range(len(rows))]
        if len(rows) > self.max_candidate_group:
            self._review("oversized_exact_candidate_group", rows)
            return [([index], []) for index in range(len(rows))]
        graph = _UnionFind(rows)
        for left in range(len(rows)):
            for right in range(left + 1, len(rows)):
                shared = set(rows[left].get("anchors") or []) & set(
                    rows[right].get("anchors") or []
                )
                if not shared:
                    continue
                compatible, reason = graph.compatible(left, right)
                if not compatible:
                    self._review(reason, [rows[left], rows[right]])
                    continue
                informative = bool(
                    set(rows[left].get("years") or [])
                    | set(rows[right].get("years") or [])
                    | set(rows[left].get("registrations") or [])
                    | set(rows[right].get("registrations") or [])
                )
                if not informative:
                    continue
                anchor_kind = "author" if any(value.startswith("author:") for value in shared) else "group"
                rule = f"exact_title_subtype_shared_{anchor_kind}_compatible_constraints"
                if graph.union(left, right, rule):
                    self.metrics["automatic_edges"] += 1
        grouped: dict[int, list[int]] = defaultdict(list)
        for index in range(len(rows)):
            grouped[graph.find(index)].append(index)
        return [
            (indices, sorted(graph.rules.get(graph.find(root), set())))
            for root, indices in sorted(grouped.items())
        ]

    def _canonical_id(
        self, documents: list[dict[str, Any]], member_ids: list[str]
    ) -> str:
        priorities = {
            "projects": {"exact_type_title_year": 2},
            "patents": {
                "exact_namespace_registration_country_title": 3,
                "exact_namespace_title_year_country": 2,
            },
        }[self.entity]
        ranked = sorted(
            (
                priorities.get(
                    str((value.get("source_metadata") or {}).get("identity_rule") or ""),
                    0,
                ),
                str(value["_id"]),
            )
            for value in documents
        )
        best = ranked[-1][0] if ranked else 0
        if best:
            return min(value_id for priority, value_id in ranked if priority == best)
        if len(member_ids) == 1:
            return member_ids[0]
        identity = f"{self.version}|{self.entity}|{'|'.join(member_ids)}"
        return sha256(identity.encode("utf-8")).hexdigest()

    def _materialize_component(
        self,
        documents: list[dict[str, Any]],
        rules: list[str],
        evidence: dict[str, Any],
        built_at: int,
    ) -> dict[str, Any]:
        member_ids = sorted(str(value["_id"]) for value in documents)
        output_id = self._canonical_id(documents, member_ids)
        ordered = sorted(
            documents,
            key=lambda value: (
                str(value["_id"]) != output_id,
                str(value["_id"]),
            ),
        )
        prepared = []
        for value in ordered:
            copied = deepcopy(value)
            copied["_id"] = output_id
            prepared.append(copied)
        output = prepared[0]
        for value in prepared[1:]:
            output = merge_entity_documents(output, value)
        output.setdefault("updated", []).append(
            {"source": "scienti_entity_graph", "time": built_at}
        )
        metadata = output.setdefault("source_metadata", {})
        metadata["target_entity"] = self.entity
        metadata["entity_graph"] = {
            "version": self.version,
            "member_ids": member_ids,
            "member_count": len(member_ids),
            "link_rules": sorted(set(rules)),
            "evidence": evidence,
            "conservative_exact_matching": True,
        }
        return output

    def _materialize_group(
        self,
        rows: list[dict[str, Any]],
        built_at: int,
        documents_by_id: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        source_ids = [str(value["source_id"]) for value in rows]
        if documents_by_id is None:
            documents_by_id = {
                str(value["_id"]): value
                for value in self.db[self.source_collection].find(
                    {"_id": {"$in": source_ids}}
                )
            }
        if any(source_id not in documents_by_id for source_id in source_ids):
            raise RuntimeError("entity graph source changed during materialization")
        output = []
        for indices, rules in self._components(rows):
            documents = [documents_by_id[source_ids[index]] for index in indices]
            component_rows = [rows[index] for index in indices]
            anchor_counts = Counter(
                anchor
                for value in component_rows
                for anchor in value.get("anchors") or []
            )
            evidence = {
                "title_key": component_rows[0].get("title_key", ""),
                "subtype": component_rows[0].get("subtype", ""),
                "years": sorted(
                    {year for value in component_rows for year in value.get("years") or []}
                ),
                "registrations": sorted(
                    {
                        item
                        for value in component_rows
                        for item in value.get("registrations") or []
                    }
                ),
                "countries": sorted(
                    {
                        item
                        for value in component_rows
                        for item in value.get("countries") or []
                    }
                ),
                "shared_stable_anchors": sorted(
                    value for value, count in anchor_counts.items() if count > 1
                ),
            }
            output.append(
                self._materialize_component(documents, rules, evidence, built_at)
            )
            self.metrics["components"] += 1
            if len(documents) > 1:
                self.metrics["merged_components"] += 1
                self.metrics["merged_source_documents"] += len(documents)
            else:
                self.metrics["singleton_components"] += 1
        return output

    def _materialize(self) -> dict[str, Any]:
        run = self.runs.find_one({"_id": self.run_name}) or {}
        stage = (run.get("stages") or {}).get("materialize") or {}
        if stage.get("status") == "complete":
            return stage.get("summary") or {}
        last_group = str(stage.get("last_group_key") or "")
        query = {"group_key": {"$gt": last_group}} if last_group else {}
        cursor = self.db[self.keys_name].find(query).sort(
            [("group_key", ASCENDING), ("source_id", ASCENDING)]
        )
        output = self.db[self.output_name]
        pending_groups: list[list[dict[str, Any]]] = []
        pending_source_count = 0
        current_key = ""
        current_rows: list[dict[str, Any]] = []
        groups = int(stage.get("groups") or 0)
        built_at = int(run.get("built_at") or time())

        def flush_batch() -> None:
            nonlocal groups, last_group, pending_source_count
            if not pending_groups:
                return
            source_ids = [
                str(row["source_id"])
                for group_rows in pending_groups
                for row in group_rows
            ]
            documents_by_id = {
                str(value["_id"]): value
                for value in self.db[self.source_collection].find(
                    {"_id": {"$in": source_ids}}
                )
            }
            if len(documents_by_id) != len(source_ids):
                raise RuntimeError("entity graph source changed during materialization")
            pending_documents: list[dict[str, Any]] = []
            for group_rows in pending_groups:
                pending_documents.extend(
                    self._materialize_group(
                        group_rows, built_at, documents_by_id=documents_by_id
                    )
                )
            self._bulk_replace(output, pending_documents)
            groups += len(pending_groups)
            last_group = pending_groups[-1][0]["group_key"]
            pending_groups.clear()
            pending_source_count = 0
            self.runs.update_one(
                {"_id": self.run_name},
                {"$set": {
                    "status": "running",
                    "stages.materialize.status": "running",
                    "stages.materialize.last_group_key": last_group,
                    "stages.materialize.groups": groups,
                    "last_progress_at": datetime.now(timezone.utc),
                }},
            )
            if groups % self.progress_every < self.batch_size:
                print(
                    f"{datetime.now(timezone.utc).isoformat()} INFO: "
                    f"{self.entity} graph groups={groups}",
                    flush=True,
                )

        def queue_group(rows: list[dict[str, Any]]) -> None:
            nonlocal pending_source_count
            if not rows:
                return
            pending_groups.append(rows)
            pending_source_count += len(rows)
            if pending_source_count >= self.batch_size:
                flush_batch()

        for row in cursor:
            row_key = str(row["group_key"])
            if current_rows and row_key != current_key:
                queue_group(current_rows)
                current_rows = []
            current_key = row_key
            current_rows.append(row)
        queue_group(current_rows)
        flush_batch()
        output.create_index("titles.title")
        output.create_index("authors.id")
        output.create_index("groups.id")
        output.create_index("source_metadata.entity_graph.member_ids")
        output.create_index("source_metadata.identity_key")
        summary = {
            "components": output.count_documents({}),
            "merged_components": output.count_documents(
                {"source_metadata.entity_graph.member_count": {"$gt": 1}}
            ),
            "source_documents": int(run.get("source_count") or 0),
        }
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {
                "stages.materialize.status": "complete",
                "stages.materialize.last_group_key": last_group,
                "stages.materialize.groups": groups,
                "stages.materialize.summary": summary,
                "stages.materialize.metrics": dict(self.metrics),
            }},
        )
        return summary

    def _audit(self) -> dict[str, Any]:
        run = self.runs.find_one({"_id": self.run_name}) or {}
        source_count = int(run.get("source_count") or 0)
        output = self.db[self.output_name]
        represented_result = next(
            output.aggregate(
                [{"$group": {"_id": None, "count": {"$sum": "$source_metadata.entity_graph.member_count"}}}]
            ),
            {},
        )
        represented = int(represented_result.get("count") or 0)
        duplicated_result = list(
            output.aggregate(
                [
                    {"$unwind": "$source_metadata.entity_graph.member_ids"},
                    {"$group": {"_id": "$source_metadata.entity_graph.member_ids", "count": {"$sum": 1}}},
                    {"$match": {"count": {"$ne": 1}}},
                    {"$limit": 1},
                ],
                allowDiskUse=True,
            )
        )
        critical = {
            "source_coverage": abs(source_count - represented),
            "duplicate_source_members": len(duplicated_result),
            "missing_title": output.count_documents({"titles.0.title": {"$exists": False}}),
            "wrong_target_entity": output.count_documents(
                {"source_metadata.target_entity": {"$ne": self.entity}}
            ),
            "missing_graph_metadata": output.count_documents(
                {"source_metadata.entity_graph.version": {"$ne": self.version}}
            ),
        }
        critical_count = sum(critical.values())
        summary = {
            "status": "passed" if critical_count == 0 else "failed",
            "graph_version": self.version,
            "entity": self.entity,
            "source_documents": source_count,
            "components": output.count_documents({}),
            "merged_components": output.count_documents(
                {"source_metadata.entity_graph.member_count": {"$gt": 1}}
            ),
            "represented_source_documents": represented,
            "critical_anomalies": critical_count,
            "critical_counts": critical,
            "review_findings": self.reviews.count_documents({"run_name": self.run_name}),
        }
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"stages.audit.status": summary["status"], "stages.audit.summary": summary}},
        )
        if critical_count:
            raise RuntimeError(f"{self.entity} graph audit failed: {critical}")
        return summary

    def _publish(self, audit: dict[str, Any]) -> dict[str, Any]:
        if self.target_collection in self.db.list_collection_names():
            raise RuntimeError("entity graph target appeared concurrently")
        self.db[self.output_name].rename(self.target_collection, dropTarget=False)
        summary = {
            **audit,
            "status": "complete",
            "collection": self.target_collection,
            "finished_at": datetime.now(timezone.utc),
        }
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {"status": "complete", "summary": summary, "finished_at": summary["finished_at"]}},
        )
        self.db[self.keys_name].drop()
        return summary

    def run(self) -> dict[str, Any]:
        previous = self._prepare()
        if previous.get("status") == "complete":
            if self.target_collection not in self.db.list_collection_names():
                raise RuntimeError("completed entity graph target is missing")
            return deepcopy(previous.get("summary") or {})
        self.runs.update_one(
            {"_id": self.run_name},
            {"$set": {
                "status": "running",
                "started_at": previous.get("started_at") or datetime.now(timezone.utc),
                "built_at": int(previous.get("built_at") or time()),
            }},
        )
        try:
            self._extract_keys()
            self._materialize()
            audit = self._audit()
            return self._publish(audit)
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
