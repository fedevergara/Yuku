"""Atomic publication pointer for audited Scienti entity versions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any

from yuku.scienti_auxiliary_audit import AUDITS as AUXILIARY_AUDITS
from yuku.scienti_entities import ENTITY_AUDITS, ENTITY_KEYS, ENTITY_RUNS
from yuku.scienti_entity_comparison import ENTITY_COMPARISONS
from yuku.scienti_project_audit import PROJECT_AUDITS


PUBLICATIONS = "scienti_entity_publications"
PUBLICATION_VERSION = "scienti-entity-publication-v1"
RUN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ScientiEntityPublisher:
    """Promote one immutable entity run only after every declared gate passes."""

    def __init__(
        self,
        db,
        *,
        run_name: str,
        comparison_name: str,
        project_audit_name: str,
        works_audit_name: str,
        patents_audit_name: str,
        events_audit_name: str,
        allow_snapshot_without_comparison: bool = False,
    ):
        required_names = {
            "run_name": run_name,
            "project_audit_name": project_audit_name,
            "works_audit_name": works_audit_name,
            "patents_audit_name": patents_audit_name,
            "events_audit_name": events_audit_name,
        }
        for label, value in required_names.items():
            if not RUN_NAME_RE.fullmatch(value or ""):
                raise ValueError(f"{label} must contain only letters, numbers and underscores")
        if comparison_name and not RUN_NAME_RE.fullmatch(comparison_name):
            raise ValueError(
                "comparison_name must contain only letters, numbers and underscores"
            )
        if not comparison_name and not allow_snapshot_without_comparison:
            raise ValueError(
                "comparison_name is required unless audited snapshot publication "
                "is explicitly enabled"
            )
        self.db = db
        self.run_name = run_name
        self.comparison_name = comparison_name
        self.allow_snapshot_without_comparison = bool(
            allow_snapshot_without_comparison
        )
        self.audit_names = {
            "projects": project_audit_name,
            "works": works_audit_name,
            "patents": patents_audit_name,
            "events": events_audit_name,
        }

    @staticmethod
    def _passed(document: dict[str, Any], label: str) -> dict[str, Any]:
        summary = document.get("summary") or document
        status = str(document.get("status") or summary.get("status") or "")
        critical = int(summary.get("critical_findings", summary.get("critical_anomalies", 0)) or 0)
        if status not in {"passed", "passed_with_findings"} or critical:
            raise RuntimeError(f"{label} did not pass: status={status} critical={critical}")
        return summary

    def _validate(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        run = self.db[ENTITY_RUNS].find_one({"_id": self.run_name})
        if not run or run.get("status") != "complete":
            raise RuntimeError("target entity normalization is missing or incomplete")
        config = run.get("config") or {}
        destinations = config.get("destinations") or {}
        if set(destinations) != set(ENTITY_KEYS):
            raise RuntimeError("target run does not define all four entity collections")
        internal = self.db[ENTITY_AUDITS].find_one({"_id": self.run_name}) or {}
        self._passed(internal, "entity normalization audit")

        comparison_summary: dict[str, Any] = {}
        if self.comparison_name:
            comparison = self.db[ENTITY_COMPARISONS].find_one(
                {"_id": self.comparison_name}
            ) or {}
            comparison_summary = self._passed(
                comparison, "entity version comparison"
            )
            if comparison_summary.get("new_run_name") != self.run_name:
                raise RuntimeError("comparison does not target the requested entity run")

        audit_summaries: dict[str, Any] = {}
        for entity, audit_name in self.audit_names.items():
            collection = PROJECT_AUDITS if entity == "projects" else AUXILIARY_AUDITS
            audit = self.db[collection].find_one({"_id": audit_name}) or {}
            summary = self._passed(audit, f"{entity} semantic audit")
            audit_config = audit.get("config") or {}
            audit_run_name = summary.get("entity_run_name") or audit_config.get("entity_run_name")
            audit_collection = summary.get("collection") or audit_config.get("collection")
            if audit_run_name != self.run_name:
                raise RuntimeError(f"{entity} audit belongs to another entity run")
            if audit_collection != destinations[entity]:
                raise RuntimeError(f"{entity} audit belongs to another collection")
            audit_summaries[entity] = {
                "audit_name": audit_name,
                "status": summary.get("status"),
                "critical_findings": int(summary.get("critical_findings", 0) or 0),
                "warning_findings": int(summary.get("warning_findings", 0) or 0),
                "documents": int(summary.get("documents", summary.get("projects", 0)) or 0),
            }
        return run, comparison_summary, audit_summaries

    def publish(self) -> dict[str, Any]:
        run, comparison, audits = self._validate()
        publications = self.db[PUBLICATIONS]
        current = publications.find_one({"_id": "current"}) or {}
        if current.get("current_run_name") == self.run_name:
            return deepcopy(current)
        current_run = str(current.get("current_run_name") or "")
        expected_previous = (
            str(comparison.get("old_run_name") or "")
            if self.comparison_name
            else current_run
        )
        if self.comparison_name and current_run and current_run != expected_previous:
            raise RuntimeError(
                f"publication changed concurrently: current={current_run} expected={expected_previous}"
            )
        destinations = deepcopy((run.get("config") or {}).get("destinations") or {})
        published_at = utc_now()
        pointer = {
            "_id": "current",
            "publication_version": PUBLICATION_VERSION,
            "current_run_name": self.run_name,
            "previous_run_name": expected_previous,
            "destinations": destinations,
            "normalizer_version": (run.get("config") or {}).get("normalizer_version"),
            "publication_mode": (
                "compared_release"
                if self.comparison_name
                else "audited_snapshot"
            ),
            "comparison_name": self.comparison_name or None,
            "semantic_audits": audits,
            "published_at": published_at,
        }
        # This single-document replacement is the authoritative atomic switch.
        publications.replace_one({"_id": "current"}, pointer, upsert=True)
        publications.replace_one(
            {"_id": self.run_name},
            {
                **deepcopy(pointer),
                "_id": self.run_name,
                "record_type": "release",
                "status": "current",
            },
            upsert=True,
        )
        if expected_previous:
            publications.update_one(
                {"_id": expected_previous, "record_type": "release"},
                {"$set": {"status": "superseded", "superseded_at": published_at}},
            )
        publications.create_index("current_run_name")
        publications.create_index("status")
        return publications.find_one({"_id": "current"}) or pointer
