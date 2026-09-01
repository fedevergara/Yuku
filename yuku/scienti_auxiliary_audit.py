"""Specialized semantic audits for Scienti works, patents and events."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any
import unicodedata

from pymongo import ASCENDING

from yuku.scienti_entities import (
    ENTITY_NORMALIZER_VERSION,
    ENTITY_RUNS,
    THESIS_FAMILIES,
    exact_key,
    registration_key,
)
from yuku.scienti_project_audit import (
    dates_compatible,
    definitely_ends_before,
    parse_scienti_date,
    semantic_key,
)


AUDIT_VERSION = "scienti-auxiliary-semantic-audit-v2"
AUDITS = "scienti_auxiliary_semantic_audits"
ANOMALIES = "scienti_auxiliary_semantic_audit_anomalies"
SUPPORTED_ENTITIES = ("works", "patents", "events")
RUN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
COLLECTION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,119}")
COD_RH_RE = re.compile(r"\d{10}")

CRITICAL = {
    "missing_title", "divergent_titles", "missing_identity_key",
    "invalid_identity_key", "wrong_target_entity", "wrong_family",
    "missing_occurrences", "duplicate_occurrence_ids", "author_count_mismatch",
    "duplicate_authors", "occurrence_missing_source_kind",
    "occurrence_missing_source_id", "occurrence_missing_route_rule",
    "invalid_normalizer_version", "invalid_thesis_identity",
    "student_also_advisor", "missing_impactu_thesis_type",
    "unexpected_impactu_thesis_type", "invalid_ip_identity",
    "missing_ip_namespace", "mixed_ip_namespaces",
    "registration_identity_without_registration",
    "conflicting_registration_numbers", "identity_namespace_mismatch",
    "invalid_event_identity", "conflicting_event_types",
    "conflicting_event_start_dates", "top_event_date_mismatch",
    "top_event_year_mismatch",
}
WARNINGS = {
    "source_scoped_identity", "author_identity_conflicts", "missing_authors",
    "missing_types",
    "probable_duplicate_author_names", "missing_student", "missing_advisor",
    "invalid_year_published", "date_published_year_mismatch",
    "invalid_presentation_date", "conflicting_countries",
    "missing_registration_number", "missing_ip_rights_holder",
    "missing_patent_presentation_date",
    "invalid_event_start_date", "invalid_event_end_date",
    "event_end_before_start", "missing_event_start_date", "missing_top_event_date",
    "top_event_type_mismatch", "conflicting_event_scopes",
    "source_invalid_event_start_date", "source_invalid_event_end_date",
    "source_event_end_before_start", "source_invalid_event_scope",
    "source_event_scope_normalized", "advisor_removed_exact_student_match",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def person_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _rows(counter: Counter) -> list[dict[str, Any]]:
    return [
        {"value": key, "count": int(value)}
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _counter(rows: list[dict[str, Any]]) -> Counter:
    return Counter({str(row["value"]): int(row["count"]) for row in rows or []})


def _bucket(value: int, bounds: tuple[int, ...]) -> str:
    lower = 0
    for upper in bounds:
        if value <= upper:
            return f"{lower}-{upper}"
        lower = upper + 1
    return f"{lower}+"


class ScientiAuxiliarySemanticAudit:
    """Checkpointed entity-specific semantic audit without source mutation."""

    def __init__(
        self,
        db,
        *,
        audit_name: str,
        entity: str,
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
        if entity not in SUPPORTED_ENTITIES:
            raise ValueError(f"entity must be one of {SUPPORTED_ENTITIES}")
        if not COLLECTION_RE.fullmatch(collection or "") or collection.startswith("system."):
            raise ValueError("collection is invalid")
        if progress_every < 1 or batch_size < 1 or example_limit < 1:
            raise ValueError("progress_every, batch_size and example_limit must be positive")
        self.db = db
        self.audit_name = audit_name
        self.entity = entity
        self.collection = collection
        self.entity_run_name = entity_run_name
        self.progress_every = int(progress_every)
        self.batch_size = int(batch_size)
        self.example_limit = int(example_limit)
        self.audits = db[AUDITS]
        self.anomalies = db[ANOMALIES]
        self.config: dict[str, Any] = {}
        self.config_hash = ""
        self.expected = 0
        self.findings: Counter = Counter()
        self.severities: Counter = Counter()
        self.metrics: Counter = Counter()
        self.distributions: dict[str, Counter] = defaultdict(Counter)
        self.example_counts: Counter = Counter()

    def _load_source(self) -> None:
        if self.collection not in self.db.list_collection_names():
            raise ValueError(f"entity collection {self.collection!r} was not found")
        run = self.db[ENTITY_RUNS].find_one({"_id": self.entity_run_name})
        if not run or run.get("status") != "complete":
            raise RuntimeError("entity normalization must be complete before semantic audit")
        config = run.get("config") or {}
        destination = (config.get("destinations") or {}).get(self.entity)
        if destination != self.collection:
            raise RuntimeError(
                f"entity run destination for {self.entity} is {destination!r}, not {self.collection!r}"
            )
        self.expected = self.db[self.collection].count_documents({})
        if self.expected < 1:
            raise RuntimeError("entity collection is empty")
        finished_at = run.get("finished_at")
        self.config = {
            "audit_version": AUDIT_VERSION,
            "entity": self.entity,
            "collection": self.collection,
            "entity_run_name": self.entity_run_name,
            "entity_router_version": config.get("router_version"),
            "entity_normalizer_version": config.get("normalizer_version"),
            "entity_finished_at": finished_at.isoformat() if isinstance(finished_at, datetime) else str(finished_at or ""),
            "expected_documents": self.expected,
        }
        self.config_hash = sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

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
        state = previous.get("state") or {}
        self.findings = Counter({key: int(value) for key, value in (state.get("findings") or {}).items()})
        self.severities = Counter({key: int(value) for key, value in (state.get("severities") or {}).items()})
        self.metrics = Counter({key: int(value) for key, value in (state.get("metrics") or {}).items()})
        self.distributions = defaultdict(
            Counter,
            {
                key: _counter(value)
                for key, value in (state.get("distributions") or {}).items()
            },
        )
        self.example_counts = Counter(
            {
                str(value["_id"]): int(value["count"])
                for value in self.anomalies.aggregate(
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
                key: _rows(value) for key, value in sorted(self.distributions.items())
            },
        }

    def _severity(self, kind: str) -> str:
        if kind in CRITICAL:
            return "critical"
        if kind in WARNINGS:
            return "warning"
        return "info"

    def _finding(self, kind: str, document: dict[str, Any], detail: Any = None) -> None:
        severity = self._severity(kind)
        self.findings[kind] += 1
        self.severities[severity] += 1
        if self.example_counts[kind] >= self.example_limit:
            return
        identifier = str(document.get("_id") or "")
        example_id = sha256(f"{self.audit_name}|{kind}|{identifier}".encode()).hexdigest()
        result = self.anomalies.replace_one(
            {"_id": example_id},
            {
                "_id": example_id,
                "audit_name": self.audit_name,
                "entity": self.entity,
                "kind": kind,
                "severity": severity,
                "document_id": identifier,
                "title": str(((document.get("titles") or [{}])[0]).get("title") or ""),
                "detail": detail,
                "recorded_at": utc_now(),
            },
            upsert=True,
        )
        if result.upserted_id is not None:
            self.example_counts[kind] += 1

    def _audit_base(self, document: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        self.metrics["documents"] += 1
        titles = [
            str(value.get("title") or "").strip()
            for value in document.get("titles") or []
            if str(value.get("title") or "").strip()
        ]
        if not titles:
            self._finding("missing_title", document)
        elif len({semantic_key(value) for value in titles}) > 1:
            self._finding("divergent_titles", document, titles)
        metadata = document.get("source_metadata") or {}
        if metadata.get("target_entity") != self.entity:
            self._finding("wrong_target_entity", document, metadata.get("target_entity"))
        if metadata.get("normalizer_version") != ENTITY_NORMALIZER_VERSION:
            self._finding("invalid_normalizer_version", document, metadata.get("normalizer_version"))
        identity = str(metadata.get("identity_key") or "")
        if not identity:
            self._finding("missing_identity_key", document)
        if metadata.get("author_identity_conflicts"):
            self._finding("author_identity_conflicts", document, metadata.get("author_identity_conflicts"))
        if not document.get("types"):
            self._finding("missing_types", document)
        identity_rule = str(metadata.get("identity_rule") or "")
        self.distributions["identity_rules"][identity_rule or "missing"] += 1

        authors = document.get("authors") or []
        try:
            declared = int(document.get("author_count") or 0)
        except (TypeError, ValueError):
            declared = -1
        if declared != len(authors):
            self._finding("author_count_mismatch", document, {"declared": declared, "actual": len(authors)})
        author_keys = [
            (str(value.get("id") or ""), semantic_key(value.get("full_name")), str(value.get("type") or ""))
            for value in authors
        ]
        if len(author_keys) != len(set(author_keys)):
            self._finding("duplicate_authors", document, author_keys)
        probable = defaultdict(list)
        for value in authors:
            key = person_key(value.get("full_name"))
            if key:
                probable[key].append(
                    {"id": str(value.get("id") or ""), "name": str(value.get("full_name") or ""), "type": str(value.get("type") or "")}
                )
        duplicates = [values for values in probable.values() if len(values) > 1]
        if duplicates:
            self._finding("probable_duplicate_author_names", document, duplicates[:10])
        if not authors:
            self._finding("missing_authors", document)
        self.distributions["author_counts"][_bucket(len(authors), (0, 1, 2, 5, 10, 25))] += 1
        self.distributions["group_counts"][_bucket(len(document.get("groups") or []), (0, 1, 2, 5, 10))] += 1

        occurrences = metadata.get("occurrences") or []
        self.metrics["occurrences"] += len(occurrences)
        self.distributions["occurrence_counts"][_bucket(len(occurrences), (0, 1, 2, 5, 10, 25, 50))] += 1
        if not occurrences:
            self._finding("missing_occurrences", document)
            return [], metadata
        occurrence_ids = [str(value.get("id") or "") for value in occurrences]
        if len(occurrence_ids) != len(set(occurrence_ids)):
            self._finding("duplicate_occurrence_ids", document, occurrence_ids)
        source_kinds = set()
        for occurrence in occurrences:
            source_kind = str(occurrence.get("source_kind") or "")
            if not source_kind:
                self._finding("occurrence_missing_source_kind", document, occurrence.get("id"))
            else:
                source_kinds.add(source_kind)
            if not str(occurrence.get("source_id") or ""):
                self._finding("occurrence_missing_source_id", document, occurrence.get("id"))
            if not str(occurrence.get("route_rule") or ""):
                self._finding("occurrence_missing_route_rule", document, occurrence.get("id"))
        self.distributions["source_combinations"]["+".join(sorted(source_kinds)) or "missing"] += 1
        return occurrences, metadata

    def _audit_work(self, document: dict[str, Any], occurrences: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
        family = str(metadata.get("family") or "")
        allowed = {"work", *THESIS_FAMILIES}
        if family not in allowed:
            self._finding("wrong_family", document, family)
        self.distributions["families"][family or "missing"] += 1
        identity = str(metadata.get("identity_key") or "")
        if family == "work":
            # General works intentionally remain source-scoped at this layer.
            # Bibliographic de-duplication belongs to the conservative works
            # graph, so treating these identities as thesis anomalies would
            # produce false positives and encourage unsafe early merges.
            if not identity.startswith("source|works|"):
                self._finding("invalid_identity_key", document, identity)
        else:
            if not (identity.startswith("thesis|") or identity.startswith("source|works|")):
                self._finding("invalid_thesis_identity", document, identity)
            if identity.startswith("source|works|"):
                self._finding("source_scoped_identity", document, identity)

        year = document.get("year_published")
        if year is not None and not (isinstance(year, int) and 1900 <= year <= 2100):
            self._finding("invalid_year_published", document, year)
        top_date = parse_scienti_date(document.get("date_published"))
        if top_date["status"] == "valid" and year is not None and top_date["year"] != year:
            self._finding("date_published_year_mismatch", document, {"date": top_date, "year": year})
        for occurrence in occurrences:
            values = occurrence.get("metadata") or {}
            for key in ("institution", "affiliation", "academic_program", "assessment"):
                if values.get(key) not in (None, ""):
                    self.metrics[f"occurrences_with_{key}"] += 1

        if family == "work":
            return

        authors = document.get("authors") or []
        students = [value for value in authors if value.get("type") != "advisor"]
        advisors = [value for value in authors if value.get("type") == "advisor"]
        if not students:
            self._finding("missing_student", document)
        if not advisors:
            self._finding("missing_advisor", document)
        # Role exclusion follows the same accent-sensitive exact presentation
        # key as normalization.  Accent-insensitive similarities remain only
        # the non-destructive probable_duplicate_author_names warning.
        student_keys = {exact_key(value.get("full_name")) for value in students}
        advisor_keys = {exact_key(value.get("full_name")) for value in advisors}
        overlap = sorted((student_keys & advisor_keys) - {""})
        if overlap:
            self._finding("student_also_advisor", document, overlap)
        resolutions = metadata.get("role_resolutions") or []
        self.metrics["advisor_roles_removed_by_exact_student_match"] += len(resolutions)
        if resolutions:
            self._finding(
                "advisor_removed_exact_student_match", document, resolutions[:10]
            )
        self.distributions["student_counts"][_bucket(len(students), (0, 1, 2, 5, 10))] += 1
        self.distributions["advisor_counts"][_bucket(len(advisors), (0, 1, 2, 5, 10))] += 1
        self.metrics["students_without_cod_rh"] += sum(
            not COD_RH_RE.fullmatch(str(value.get("id") or "")) for value in students
        )
        self.metrics["advisors_without_cod_rh"] += sum(
            not COD_RH_RE.fullmatch(str(value.get("id") or "")) for value in advisors
        )
        impactu_types = {
            semantic_key(value.get("type"))
            for value in document.get("types") or []
            if value.get("source") == "impactu"
        }
        expected = {
            "undergraduate_thesis": semantic_key("Tesis de Pregrado"),
            "graduate_thesis": semantic_key("Tesis de Posgrado"),
            "graduate_degree_work": semantic_key("Docencia"),
        }.get(family)
        if expected and expected not in impactu_types:
            self._finding("missing_impactu_thesis_type", document, sorted(impactu_types))
        if not expected and impactu_types:
            self._finding("unexpected_impactu_thesis_type", document, sorted(impactu_types))

    def _audit_patent(self, document: dict[str, Any], occurrences: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
        family = str(metadata.get("family") or "")
        if family not in {"patent", "intellectual_property"}:
            self._finding("wrong_family", document, family)
        self.distributions["families"][family or "missing"] += 1
        identity = str(metadata.get("identity_key") or "")
        if not identity.startswith("ip|"):
            self._finding("invalid_ip_identity", document, identity)
        if "|source|" in identity:
            self._finding("source_scoped_identity", document, identity)
        namespaces = {
            str(value.get("identity_namespace") or "") for value in occurrences
        }
        if "" in namespaces:
            self._finding("missing_ip_namespace", document)
            namespaces.discard("")
        if len(namespaces) > 1:
            self._finding("mixed_ip_namespaces", document, sorted(namespaces))
        for namespace in namespaces:
            self.distributions["ip_namespaces"][namespace] += 1
            if identity.startswith("ip|") and not identity.startswith(f"ip|{namespace}|"):
                self._finding("identity_namespace_mismatch", document, {"identity": identity, "namespace": namespace})
        registrations = {
            registration_key((value.get("metadata") or {}).get("registration_number"))
            for value in occurrences
        }
        registrations.discard("")
        if not registrations:
            self._finding("missing_registration_number", document)
        if len(registrations) > 1:
            self._finding("conflicting_registration_numbers", document, sorted(registrations))
        if "|registration|" in identity and not registrations:
            self._finding("registration_identity_without_registration", document, identity)
        countries = {
            semantic_key((value.get("metadata") or {}).get("country"))
            for value in occurrences
            if (value.get("metadata") or {}).get("country") not in (None, "")
        }
        if len(countries) > 1:
            self._finding("conflicting_countries", document, sorted(countries))
        has_rights_holder = False
        has_presentation_date = False
        for occurrence in occurrences:
            values = occurrence.get("metadata") or {}
            presentation = values.get("presentation_date")
            has_presentation_date = has_presentation_date or presentation not in (None, "")
            has_rights_holder = has_rights_holder or any(
                values.get(key) not in (None, "") for key in ("applicant", "holder")
            )
            if presentation not in (None, "") and parse_scienti_date(presentation)["status"] == "invalid":
                self._finding("invalid_presentation_date", document, presentation)
            for key in ("applicant", "holder", "country", "registration_number", "funding_institution"):
                if values.get(key) not in (None, ""):
                    self.metrics[f"occurrences_with_{key}"] += 1
        if not has_rights_holder:
            self._finding("missing_ip_rights_holder", document)
        if family == "patent" and not has_presentation_date:
            self._finding("missing_patent_presentation_date", document)

    def _audit_event(self, document: dict[str, Any], occurrences: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
        family = str(metadata.get("family") or "")
        if family != "event":
            self._finding("wrong_family", document, family)
        identity = str(metadata.get("identity_key") or "")
        if not (identity.startswith("event|") or identity.startswith("source|events|")):
            self._finding("invalid_event_identity", document, identity)
        if identity.startswith("source|events|"):
            self._finding("source_scoped_identity", document, identity)
        event_types = set()
        starts = []
        scopes = set()
        for occurrence in occurrences:
            values = occurrence.get("metadata") or {}
            for finding in occurrence.get("normalization_findings") or []:
                kind = str((finding or {}).get("kind") or "")
                if kind in {
                    "source_invalid_event_start_date",
                    "source_invalid_event_end_date",
                    "source_event_end_before_start",
                    "source_invalid_event_scope",
                    "source_event_scope_normalized",
                }:
                    self._finding(kind, document, finding)
            event_type = semantic_key(values.get("event_type") or occurrence.get("product_type"))
            if event_type:
                event_types.add(event_type)
                self.distributions["event_types"][event_type] += 1
            scope = semantic_key(values.get("scope"))
            if scope:
                scopes.add(scope)
                self.distributions["event_scopes"][scope] += 1
            start = parse_scienti_date(values.get("start_date"))
            end = parse_scienti_date(values.get("end_date"))
            if start["status"] == "invalid":
                self._finding("invalid_event_start_date", document, start["raw"])
            elif start["status"] == "missing":
                self._finding("missing_event_start_date", document, occurrence.get("id"))
            elif start["status"] == "valid":
                starts.append(start)
            if end["status"] == "invalid":
                self._finding("invalid_event_end_date", document, end["raw"])
            if definitely_ends_before(start, end):
                self._finding("event_end_before_start", document, {"start": start["raw"], "end": end["raw"]})
            for key in ("location", "city", "venue"):
                if values.get(key) not in (None, ""):
                    self.metrics[f"occurrences_with_{key}"] += 1
            self.metrics["associated_products"] += len(values.get("associated_products") or [])
            self.metrics["associated_institutions"] += len(values.get("associated_institutions") or [])
            self.metrics["participants"] += len(values.get("participants") or [])
        if len(event_types) > 1:
            self._finding("conflicting_event_types", document, sorted(event_types))
        if len(scopes) > 1:
            self._finding("conflicting_event_scopes", document, sorted(scopes))
        for left in range(len(starts)):
            if any(not dates_compatible(starts[left], starts[right]) for right in range(left + 1, len(starts))):
                self._finding("conflicting_event_start_dates", document, sorted({value["raw"] for value in starts}))
                break
        top = parse_scienti_date(document.get("date_held"))
        if starts and top["status"] != "valid":
            self._finding("missing_top_event_date", document)
        elif starts and not any(dates_compatible(top, value) for value in starts):
            self._finding("top_event_date_mismatch", document, {"top": top, "sources": starts[:10]})
        year = document.get("year_held")
        source_years = {value["year"] for value in starts}
        if source_years and year not in source_years:
            self._finding("top_event_year_mismatch", document, {"top": year, "sources": sorted(source_years)})
        top_types = {semantic_key(value.get("type")) for value in document.get("types") or []}
        if event_types and not event_types.issubset(top_types):
            self._finding("top_event_type_mismatch", document, {"top": sorted(top_types), "sources": sorted(event_types)})

    def _audit_document(self, document: dict[str, Any]) -> None:
        occurrences, metadata = self._audit_base(document)
        if self.entity == "works":
            self._audit_work(document, occurrences, metadata)
        elif self.entity == "patents":
            self._audit_patent(document, occurrences, metadata)
        else:
            self._audit_event(document, occurrences, metadata)

    def _save(self, processed: int, last_id: str) -> None:
        progress = {
            "processed": processed,
            "total": self.expected,
            "percent": round(processed * 100 / self.expected, 3),
            "last_id": last_id,
            "critical": int(self.severities.get("critical", 0)),
            "warnings": int(self.severities.get("warning", 0)),
            "findings": int(sum(self.findings.values())),
        }
        self.audits.update_one(
            {"_id": self.audit_name},
            {"$set": {"status": "running", "progress": progress, "state": self._state(), "last_progress_at": utc_now()}},
        )
        print(
            f"{utc_now().isoformat()} INFO: {self.entity} semantic audit "
            f"processed={processed}/{self.expected} critical={progress['critical']} "
            f"warnings={progress['warnings']} last={last_id}",
            flush=True,
        )

    def _summary(self, processed: int) -> dict[str, Any]:
        critical = int(self.severities.get("critical", 0))
        warnings = int(self.severities.get("warning", 0))
        status = "failed" if critical else "passed_with_findings" if warnings else "passed"
        state = self._state()
        return {
            "audit_name": self.audit_name,
            "status": status,
            "audit_version": AUDIT_VERSION,
            "entity": self.entity,
            "collection": self.collection,
            "entity_run_name": self.entity_run_name,
            "entity_normalizer_version": self.config.get("entity_normalizer_version"),
            "documents": processed,
            "occurrences": int(self.metrics.get("occurrences", 0)),
            "finding_counts": state["findings"],
            "severity_counts": state["severities"],
            "critical_findings": critical,
            "warning_findings": warnings,
            "metrics": state["metrics"],
            "distributions": state["distributions"],
            "examples_collection": ANOMALIES,
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
        try:
            cursor = self.db[self.collection].find(query).sort("_id", ASCENDING).batch_size(self.batch_size)
            for document in cursor:
                self._audit_document(document)
                processed += 1
                last_id = str(document["_id"])
                if processed % self.progress_every == 0:
                    self._save(processed, last_id)
            self._save(processed, last_id)
            if processed != self.expected:
                raise RuntimeError(f"semantic audit coverage is {processed}/{self.expected}")
            summary = self._summary(processed)
            self.audits.update_one(
                {"_id": self.audit_name},
                {"$set": {"status": summary["status"], "summary": summary, "state": self._state(), "finished_at": utc_now()}},
            )
            return summary
        except Exception as error:
            self.audits.update_one(
                {"_id": self.audit_name},
                {"$set": {"status": "failed_to_run", "error": f"{type(error).__name__}: {error}", "failed_at": utc_now()}},
            )
            raise
