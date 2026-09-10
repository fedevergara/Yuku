"""Semantic, resumable audit for strict Scienti project entities.

The audit never changes normalized projects.  It counts every finding and
stores only bounded examples, with special care for the heterogeneous date
formats used by CVLAC and GrupLAC.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import calendar
import html as ihtml
import json
import re
from typing import Any, Iterable
import unicodedata
from zoneinfo import ZoneInfo

from pymongo import ASCENDING

from yuku.scienti_entities import ENTITY_RUNS


AUDIT_VERSION = "scienti-project-semantic-audit-v1"
PROJECT_AUDITS = "scienti_project_semantic_audits"
PROJECT_ANOMALIES = "scienti_project_semantic_audit_anomalies"
RUN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
COLLECTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,119}")
COD_RH_RE = re.compile(r"\d{10}")
BOGOTA = ZoneInfo("America/Bogota")

MONTHS = {
    "enero": 1, "january": 1,
    "febrero": 2, "february": 2,
    "marzo": 3, "march": 3,
    "abril": 4, "april": 4,
    "mayo": 5, "may": 5,
    "junio": 6, "june": 6,
    "julio": 7, "july": 7,
    "agosto": 8, "august": 8,
    "septiembre": 9, "setiembre": 9, "september": 9,
    "octubre": 10, "october": 10,
    "noviembre": 11, "november": 11,
    "diciembre": 12, "december": 12,
}
OPEN_DATE_TOKENS = {
    "actual", "actualidad", "en curso", "en marcha", "presente", "vigente",
}
SEVERITIES = {
    "missing_title": "critical",
    "divergent_titles": "critical",
    "missing_identity_key": "critical",
    "invalid_identity_key": "critical",
    "wrong_target_entity": "critical",
    "missing_occurrences": "critical",
    "duplicate_occurrence_ids": "critical",
    "author_count_mismatch": "critical",
    "duplicate_authors": "critical",
    "source_scoped_identity": "warning",
    "missing_types": "warning",
    "conflicting_project_types": "warning",
    "missing_author": "warning",
    "authors_without_cod_rh": "warning",
    "author_identity_conflicts": "warning",
    "missing_group": "info",
    "missing_abstract": "info",
    "abstract_missing_despite_source_summary": "warning",
    "multiple_source_summaries": "info",
    "invalid_start_date_occurrence": "warning",
    "invalid_end_date_occurrence": "warning",
    "source_end_before_start": "warning",
    "conflicting_start_dates": "warning",
    "conflicting_end_dates": "warning",
    "mixed_open_and_closed_end_dates": "info",
    "missing_top_start_year": "warning",
    "missing_top_end_year": "warning",
    "top_start_year_mismatch": "warning",
    "top_end_year_mismatch": "warning",
    "top_start_loses_precision": "warning",
    "top_end_loses_precision": "warning",
    "top_end_present_for_open_project": "warning",
    "occurrence_missing_source_kind": "critical",
    "occurrence_missing_source_id": "critical",
    "occurrence_missing_project_type": "warning",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def semantic_key(value: Any) -> str:
    text = ihtml.unescape(str(value or "")).replace("\xa0", " ")
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _date_result(
    raw: Any,
    status: str,
    *,
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> dict[str, Any]:
    precision = "none"
    if year is not None:
        precision = "year"
    if month is not None:
        precision = "month"
    if day is not None:
        precision = "day"
    return {
        "raw": str(raw or ""),
        "status": status,
        "year": year,
        "month": month,
        "day": day,
        "precision": precision,
    }


def parse_scienti_date(value: Any) -> dict[str, Any]:
    """Parse only explicit Scienti date formats, preserving precision."""
    if value in (None, ""):
        return _date_result(value, "missing")
    if isinstance(value, datetime):
        parsed = value.astimezone(BOGOTA) if value.tzinfo else value.replace(tzinfo=BOGOTA)
        return _date_result(value, "valid", year=parsed.year, month=parsed.month, day=parsed.day)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 1900 <= int(value) <= 2100:
            return _date_result(value, "valid", year=int(value))
        try:
            parsed = datetime.fromtimestamp(float(value), tz=BOGOTA)
        except (OverflowError, OSError, ValueError):
            return _date_result(value, "invalid")
        return _date_result(value, "valid", year=parsed.year, month=parsed.month, day=parsed.day)

    text = semantic_key(value).strip(" ,.;")
    if not text:
        return _date_result(value, "missing")
    if text in OPEN_DATE_TOKENS:
        return _date_result(value, "open")

    match = re.fullmatch(r"(19\d{2}|20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:\s+.*)?", text)
    if match:
        year, month, day = map(int, match.groups())
        try:
            datetime(year, month, day)
        except ValueError:
            return _date_result(value, "invalid")
        return _date_result(value, "valid", year=year, month=month, day=day)

    match = re.fullmatch(r"(19\d{2}|20\d{2})[-/](\d{1,2})", text)
    if match:
        year, month = map(int, match.groups())
        if 1 <= month <= 12:
            return _date_result(value, "valid", year=year, month=month)
        return _date_result(value, "invalid")

    match = re.fullmatch(r"(\d{1,2})[-/](19\d{2}|20\d{2})", text)
    if match:
        month, year = map(int, match.groups())
        if 1 <= month <= 12:
            return _date_result(value, "valid", year=year, month=month)
        return _date_result(value, "invalid")

    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    match = re.fullmatch(rf"({month_names})\s+(19\d{{2}}|20\d{{2}})", text)
    if match:
        return _date_result(
            value, "valid", year=int(match.group(2)), month=MONTHS[match.group(1)]
        )
    match = re.fullmatch(rf"(19\d{{2}}|20\d{{2}})\s+({month_names})", text)
    if match:
        return _date_result(
            value, "valid", year=int(match.group(1)), month=MONTHS[match.group(2)]
        )
    match = re.fullmatch(r"(19\d{2}|20\d{2})", text)
    if match:
        return _date_result(value, "valid", year=int(match.group(1)))
    return _date_result(value, "invalid")


def dates_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("status") != "valid" or right.get("status") != "valid":
        return True
    for field in ("year", "month", "day"):
        left_value = left.get(field)
        right_value = right.get(field)
        if left_value is not None and right_value is not None and left_value != right_value:
            return False
    return True


def date_evidence_conflicts(values: Iterable[dict[str, Any]]) -> bool:
    valid = [value for value in values if value.get("status") == "valid"]
    return any(
        not dates_compatible(valid[left], valid[right])
        for left in range(len(valid))
        for right in range(left + 1, len(valid))
    )


def definitely_ends_before(start: dict[str, Any], end: dict[str, Any]) -> bool:
    if start.get("status") != "valid" or end.get("status") != "valid":
        return False
    start_min = (
        start["year"], start.get("month") or 1, start.get("day") or 1
    )
    end_month = end.get("month") or 12
    end_max = (
        end["year"],
        end_month,
        end.get("day") or calendar.monthrange(end["year"], end_month)[1],
    )
    return end_max < start_min


def _counter_rows(counter: Counter) -> list[dict[str, Any]]:
    return [
        {"value": key, "count": int(value)}
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _rows_counter(rows: Iterable[dict[str, Any]]) -> Counter:
    return Counter({str(row["value"]): int(row["count"]) for row in rows or []})


def _bucket(value: int, bounds: tuple[int, ...]) -> str:
    lower = 0
    for upper in bounds:
        if value <= upper:
            return f"{lower}-{upper}"
        lower = upper + 1
    return f"{lower}+"


class ScientiProjectSemanticAudit:
    """Audit every normalized project with checkpointed exact counters."""

    def __init__(
        self,
        db,
        *,
        audit_name: str,
        collection: str,
        entity_run_name: str,
        progress_every: int = 10000,
        batch_size: int = 2000,
        example_limit: int = 100,
    ):
        if not RUN_NAME_RE.fullmatch(audit_name or ""):
            raise ValueError("audit_name must contain only letters, numbers and underscores")
        if not RUN_NAME_RE.fullmatch(entity_run_name or ""):
            raise ValueError("entity_run_name must contain only letters, numbers and underscores")
        if not COLLECTION_RE.fullmatch(collection or "") or collection.startswith("system."):
            raise ValueError("collection is invalid")
        if progress_every < 1 or batch_size < 1 or example_limit < 1:
            raise ValueError("progress_every, batch_size and example_limit must be positive")
        self.db = db
        self.audit_name = audit_name
        self.collection = collection
        self.entity_run_name = entity_run_name
        self.progress_every = int(progress_every)
        self.batch_size = int(batch_size)
        self.example_limit = int(example_limit)
        self.audits = db[PROJECT_AUDITS]
        self.anomalies = db[PROJECT_ANOMALIES]
        self.config: dict[str, Any] = {}
        self.config_hash = ""
        self.expected_projects = 0
        self.findings: Counter = Counter()
        self.severities: Counter = Counter()
        self.metrics: Counter = Counter()
        self.project_types: Counter = Counter()
        self.source_combinations: Counter = Counter()
        self.start_precision: Counter = Counter()
        self.end_precision: Counter = Counter()
        self.author_histogram: Counter = Counter()
        self.group_histogram: Counter = Counter()
        self.occurrence_histogram: Counter = Counter()
        self.abstract_histogram: Counter = Counter()
        self.example_counts: Counter = Counter()

    def _load_source(self) -> None:
        if self.collection not in self.db.list_collection_names():
            raise ValueError(f"project collection {self.collection!r} was not found")
        entity_run = self.db[ENTITY_RUNS].find_one({"_id": self.entity_run_name})
        if not entity_run:
            raise ValueError(f"entity run {self.entity_run_name!r} was not found")
        if entity_run.get("status") != "complete":
            raise RuntimeError("entity normalization must be complete before semantic audit")
        entity_config = entity_run.get("config") or {}
        destination = ((entity_config.get("destinations") or {}).get("projects"))
        if destination != self.collection:
            raise RuntimeError(
                f"entity run projects destination is {destination!r}, not {self.collection!r}"
            )
        self.expected_projects = self.db[self.collection].count_documents({})
        if self.expected_projects < 1:
            raise RuntimeError("project collection is empty")
        finished_at = entity_run.get("finished_at")
        self.config = {
            "audit_version": AUDIT_VERSION,
            "collection": self.collection,
            "entity_run_name": self.entity_run_name,
            "entity_router_version": entity_config.get("router_version"),
            "entity_normalizer_version": entity_config.get("normalizer_version"),
            "entity_finished_at": finished_at.isoformat() if isinstance(finished_at, datetime) else str(finished_at or ""),
            "expected_projects": self.expected_projects,
        }
        self.config_hash = sha256(
            json.dumps(self.config, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _restore_counter(self, run: dict[str, Any], name: str) -> Counter:
        state = run.get("state") or {}
        if name in {"findings", "severities", "metrics"}:
            return Counter({key: int(value) for key, value in (state.get(name) or {}).items()})
        return _rows_counter((state.get("distributions") or {}).get(name) or [])

    def _prepare(self) -> dict[str, Any]:
        self._load_source()
        previous = self.audits.find_one({"_id": self.audit_name})
        if previous and previous.get("config_hash") != self.config_hash:
            raise ValueError("audit name already exists with another immutable source")
        if not previous:
            self.anomalies.delete_many({"audit_name": self.audit_name})
            self.audits.insert_one(
                {
                    "_id": self.audit_name,
                    "status": "pending",
                    "config": self.config,
                    "config_hash": self.config_hash,
                    "created_at": utc_now(),
                    "progress": {"processed": 0, "last_id": ""},
                    "state": {},
                }
            )
            previous = self.audits.find_one({"_id": self.audit_name}) or {}
        self.findings = self._restore_counter(previous, "findings")
        self.severities = self._restore_counter(previous, "severities")
        self.metrics = self._restore_counter(previous, "metrics")
        self.project_types = self._restore_counter(previous, "project_types")
        self.source_combinations = self._restore_counter(previous, "source_combinations")
        self.start_precision = self._restore_counter(previous, "start_precision")
        self.end_precision = self._restore_counter(previous, "end_precision")
        self.author_histogram = self._restore_counter(previous, "author_histogram")
        self.group_histogram = self._restore_counter(previous, "group_histogram")
        self.occurrence_histogram = self._restore_counter(previous, "occurrence_histogram")
        self.abstract_histogram = self._restore_counter(previous, "abstract_histogram")
        self.example_counts = Counter(
            {
                str(item["_id"]): int(item["count"])
                for item in self.anomalies.aggregate(
                    [
                        {"$match": {"audit_name": self.audit_name}},
                        {"$group": {"_id": "$kind", "count": {"$sum": 1}}},
                    ]
                )
            }
        )
        if previous.get("status") in {"passed", "passed_with_findings", "failed"}:
            return previous
        self.anomalies.create_index([("audit_name", ASCENDING), ("kind", ASCENDING)])
        self.anomalies.create_index([("audit_name", ASCENDING), ("project_id", ASCENDING)])
        self.audits.update_one(
            {"_id": self.audit_name},
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
            "severities": dict(sorted(self.severities.items())),
            "metrics": dict(sorted(self.metrics.items())),
            "distributions": {
                "project_types": _counter_rows(self.project_types),
                "source_combinations": _counter_rows(self.source_combinations),
                "start_precision": _counter_rows(self.start_precision),
                "end_precision": _counter_rows(self.end_precision),
                "author_histogram": _counter_rows(self.author_histogram),
                "group_histogram": _counter_rows(self.group_histogram),
                "occurrence_histogram": _counter_rows(self.occurrence_histogram),
                "abstract_histogram": _counter_rows(self.abstract_histogram),
            },
        }

    def _save_progress(self, processed: int, last_id: str) -> None:
        progress = {
            "processed": processed,
            "total": self.expected_projects,
            "percent": round(processed * 100 / self.expected_projects, 3),
            "last_id": last_id,
            "findings": int(sum(self.findings.values())),
            "critical": int(self.severities.get("critical", 0)),
            "warnings": int(self.severities.get("warning", 0)),
        }
        self.audits.update_one(
            {"_id": self.audit_name},
            {
                "$set": {
                    "status": "running",
                    "progress": progress,
                    "state": self._state(),
                    "last_progress_at": utc_now(),
                }
            },
        )
        print(
            f"{utc_now().isoformat()} INFO: project semantic audit "
            f"processed={processed}/{self.expected_projects} "
            f"critical={progress['critical']} warnings={progress['warnings']} "
            f"last={last_id}",
            flush=True,
        )

    def _finding(self, kind: str, project: dict[str, Any], detail: Any = None) -> None:
        severity = SEVERITIES[kind]
        self.findings[kind] += 1
        self.severities[severity] += 1
        if self.example_counts[kind] >= self.example_limit:
            return
        project_id = str(project.get("_id") or "")
        example_id = sha256(
            f"{self.audit_name}|{kind}|{project_id}".encode("utf-8")
        ).hexdigest()
        result = self.anomalies.replace_one(
            {"_id": example_id},
            {
                "_id": example_id,
                "audit_name": self.audit_name,
                "kind": kind,
                "severity": severity,
                "project_id": project_id,
                "title": str((((project.get("titles") or [{}])[0]).get("title")) or ""),
                "detail": detail,
                "recorded_at": utc_now(),
            },
            upsert=True,
        )
        if result.upserted_id is not None:
            self.example_counts[kind] += 1

    @staticmethod
    def _date_values(values: Iterable[dict[str, Any]]) -> list[str]:
        return sorted({str(value.get("raw") or "") for value in values})

    def _audit_dates(
        self,
        project: dict[str, Any],
        starts: list[dict[str, Any]],
        ends: list[dict[str, Any]],
        open_end_count: int,
    ) -> None:
        valid_starts = [value for value in starts if value["status"] == "valid"]
        valid_ends = [value for value in ends if value["status"] == "valid"]
        if date_evidence_conflicts(valid_starts):
            self._finding("conflicting_start_dates", project, self._date_values(valid_starts))
        if date_evidence_conflicts(valid_ends):
            self._finding("conflicting_end_dates", project, self._date_values(valid_ends))
        if open_end_count and valid_ends:
            self._finding(
                "mixed_open_and_closed_end_dates",
                project,
                {"open": open_end_count, "closed": self._date_values(valid_ends)},
            )

        top_start_year = project.get("year_init")
        source_start_years = {value["year"] for value in valid_starts}
        if source_start_years and top_start_year is None:
            self._finding("missing_top_start_year", project, sorted(source_start_years))
        elif source_start_years and str(top_start_year).isdigit() and int(top_start_year) not in source_start_years:
            self._finding(
                "top_start_year_mismatch",
                project,
                {"top": top_start_year, "source": sorted(source_start_years)},
            )

        top_end_year = project.get("year_end")
        source_end_years = {value["year"] for value in valid_ends}
        if source_end_years and top_end_year is None:
            self._finding("missing_top_end_year", project, sorted(source_end_years))
        elif source_end_years and str(top_end_year).isdigit() and int(top_end_year) not in source_end_years:
            self._finding(
                "top_end_year_mismatch",
                project,
                {"top": top_end_year, "source": sorted(source_end_years)},
            )

        top_start = parse_scienti_date(project.get("date_init"))
        precise_starts = [value for value in valid_starts if value.get("month") is not None]
        if top_start["status"] == "valid" and precise_starts and not any(
            dates_compatible(top_start, value) for value in precise_starts
        ):
            self._finding(
                "top_start_loses_precision",
                project,
                {"top": top_start, "source": precise_starts[:10]},
            )
        top_end = parse_scienti_date(project.get("date_end"))
        precise_ends = [value for value in valid_ends if value.get("month") is not None]
        if top_end["status"] == "valid" and precise_ends and not any(
            dates_compatible(top_end, value) for value in precise_ends
        ):
            self._finding(
                "top_end_loses_precision",
                project,
                {"top": top_end, "source": precise_ends[:10]},
            )
        if open_end_count and not valid_ends and project.get("date_end") is not None:
            self._finding(
                "top_end_present_for_open_project", project, project.get("date_end")
            )

    def _audit_project(self, project: dict[str, Any]) -> None:
        self.metrics["projects"] += 1
        titles = [
            str(value.get("title") or "").strip()
            for value in project.get("titles") or []
            if str(value.get("title") or "").strip()
        ]
        if not titles:
            self._finding("missing_title", project)
        elif len({semantic_key(value) for value in titles}) > 1:
            self._finding("divergent_titles", project, titles)

        types = project.get("types") or []
        if not types:
            self._finding("missing_types", project)

        metadata = project.get("source_metadata") or {}
        identity_key = str(metadata.get("identity_key") or "")
        if not identity_key:
            self._finding("missing_identity_key", project)
        elif not (
            identity_key.startswith("project|")
            or identity_key.startswith("source|projects|")
        ):
            self._finding("invalid_identity_key", project, identity_key)
        elif identity_key.startswith("source|projects|"):
            self._finding("source_scoped_identity", project, identity_key)
        if metadata.get("target_entity") != "projects":
            self._finding(
                "wrong_target_entity", project, metadata.get("target_entity")
            )
        if metadata.get("author_identity_conflicts"):
            self._finding(
                "author_identity_conflicts",
                project,
                metadata.get("author_identity_conflicts"),
            )

        authors = project.get("authors") or []
        author_count = int(project.get("author_count") or 0)
        self.author_histogram[_bucket(len(authors), (0, 1, 2, 5, 10, 25))] += 1
        if author_count != len(authors):
            self._finding(
                "author_count_mismatch",
                project,
                {"declared": author_count, "actual": len(authors)},
            )
        if not authors:
            self._finding("missing_author", project)
        author_keys = [
            (str(value.get("id") or ""), semantic_key(value.get("full_name")))
            for value in authors
        ]
        if len(author_keys) != len(set(author_keys)):
            self._finding("duplicate_authors", project, author_keys)
        missing_cod_rh = sum(
            1 for value in authors if not COD_RH_RE.fullmatch(str(value.get("id") or ""))
        )
        if missing_cod_rh:
            self._finding(
                "authors_without_cod_rh", project, {"authors": len(authors), "missing": missing_cod_rh}
            )

        groups = project.get("groups") or []
        self.group_histogram[_bucket(len(groups), (0, 1, 2, 5, 10))] += 1
        if not groups:
            self._finding("missing_group", project)

        abstract = str(project.get("abstract") or "").strip()
        self.abstract_histogram[_bucket(len(abstract), (0, 100, 500, 1000, 2000, 4000))] += 1
        if not abstract:
            self._finding("missing_abstract", project)

        occurrences = metadata.get("occurrences") or []
        self.metrics["occurrences"] += len(occurrences)
        self.occurrence_histogram[_bucket(len(occurrences), (0, 1, 2, 5, 10, 25, 50))] += 1
        if not occurrences:
            self._finding("missing_occurrences", project)
            return
        occurrence_ids = [str(value.get("id") or "") for value in occurrences]
        if len(occurrence_ids) != len(set(occurrence_ids)):
            self._finding("duplicate_occurrence_ids", project, occurrence_ids)

        source_kinds: set[str] = set()
        project_types: set[str] = set()
        summaries: set[str] = set()
        starts: list[dict[str, Any]] = []
        ends: list[dict[str, Any]] = []
        open_end_count = 0
        for occurrence in occurrences:
            source_kind = str(occurrence.get("source_kind") or "")
            source_id = str(occurrence.get("source_id") or "")
            if not source_kind:
                self._finding("occurrence_missing_source_kind", project, occurrence.get("id"))
            else:
                source_kinds.add(source_kind)
            if not source_id:
                self._finding("occurrence_missing_source_id", project, occurrence.get("id"))
            occurrence_metadata = occurrence.get("metadata") or {}
            project_type = str(occurrence_metadata.get("project_type") or "").strip()
            if not project_type:
                self._finding("occurrence_missing_project_type", project, occurrence.get("id"))
            else:
                project_types.add(semantic_key(project_type))
                self.project_types[project_type] += 1
            summary = str(occurrence_metadata.get("summary") or "").strip()
            if summary:
                summaries.add(semantic_key(summary))
            start = parse_scienti_date(occurrence_metadata.get("start_date"))
            end = parse_scienti_date(
                occurrence_metadata.get("end_date")
                or occurrence_metadata.get("projected_end_date")
            )
            self.start_precision[start["status"] if start["status"] != "valid" else start["precision"]] += 1
            self.end_precision[end["status"] if end["status"] != "valid" else end["precision"]] += 1
            if start["status"] == "invalid":
                self._finding("invalid_start_date_occurrence", project, start["raw"])
            if end["status"] == "invalid":
                self._finding("invalid_end_date_occurrence", project, end["raw"])
            if end["status"] == "open":
                open_end_count += 1
            if start["status"] == "valid":
                starts.append(start)
            if end["status"] == "valid":
                ends.append(end)
            if definitely_ends_before(start, end):
                self._finding(
                    "source_end_before_start",
                    project,
                    {"start": start["raw"], "end": end["raw"]},
                )

        self.source_combinations["+".join(sorted(source_kinds)) or "missing"] += 1
        if len(project_types) > 1:
            self._finding("conflicting_project_types", project, sorted(project_types))
        if not abstract and summaries:
            self._finding(
                "abstract_missing_despite_source_summary", project, {"summaries": len(summaries)}
            )
        if len(summaries) > 1:
            self._finding("multiple_source_summaries", project, {"summaries": len(summaries)})
        self._audit_dates(project, starts, ends, open_end_count)

    def _summary(self, processed: int) -> dict[str, Any]:
        critical = int(self.severities.get("critical", 0))
        warnings = int(self.severities.get("warning", 0))
        status = "failed" if critical else "passed_with_findings" if warnings else "passed"
        return {
            "audit_name": self.audit_name,
            "status": status,
            "audit_version": AUDIT_VERSION,
            "entity_router_version": self.config.get("entity_router_version"),
            "entity_normalizer_version": self.config.get("entity_normalizer_version"),
            "collection": self.collection,
            "projects": processed,
            "occurrences": int(self.metrics.get("occurrences", 0)),
            "finding_counts": dict(sorted(self.findings.items())),
            "severity_counts": dict(sorted(self.severities.items())),
            "critical_findings": critical,
            "warning_findings": warnings,
            "distributions": self._state()["distributions"],
            "examples_collection": PROJECT_ANOMALIES,
            "example_limit_per_finding": self.example_limit,
        }

    def run(self) -> dict[str, Any]:
        previous = self._prepare()
        if previous.get("status") in {"passed", "passed_with_findings", "failed"}:
            return previous.get("summary") or {}
        progress = previous.get("progress") or {}
        processed = int(progress.get("processed") or 0)
        last_id = str(progress.get("last_id") or "")
        query = {"_id": {"$gt": last_id}} if last_id else {}
        projection = {
            "titles": 1, "types": 1, "abstract": 1,
            "date_init": 1, "date_end": 1, "year_init": 1, "year_end": 1,
            "author_count": 1, "authors": 1, "groups": 1,
            "source_metadata": 1,
        }
        try:
            cursor = (
                self.db[self.collection]
                .find(query, projection)
                .sort("_id", ASCENDING)
                .batch_size(self.batch_size)
            )
            for project in cursor:
                self._audit_project(project)
                processed += 1
                last_id = str(project["_id"])
                if processed % self.progress_every == 0:
                    self._save_progress(processed, last_id)
            self._save_progress(processed, last_id)
            if processed != self.expected_projects:
                raise RuntimeError(
                    f"semantic audit coverage is {processed}/{self.expected_projects}"
                )
            summary = self._summary(processed)
            self.audits.update_one(
                {"_id": self.audit_name},
                {
                    "$set": {
                        "status": summary["status"],
                        "summary": summary,
                        "state": self._state(),
                        "finished_at": utc_now(),
                        "last_progress_at": utc_now(),
                    }
                },
            )
            print(
                f"{utc_now().isoformat()} INFO: project semantic audit finished "
                f"status={summary['status']} projects={processed} "
                f"critical={summary['critical_findings']} warnings={summary['warning_findings']}",
                flush=True,
            )
            return summary
        except Exception as error:
            self.audits.update_one(
                {"_id": self.audit_name},
                {
                    "$set": {
                        "status": "failed_to_run",
                        "error": f"{type(error).__name__}: {error}",
                        "failed_at": utc_now(),
                    }
                },
            )
            raise
