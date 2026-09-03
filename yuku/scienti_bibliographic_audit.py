"""Final bibliographic enrichment gate for published Scienti works."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any

from yuku.cvlac_related_works import (
    SUSPICIOUS_PUBLISHER_RE,
    VALID_PAGES_RE,
    norm_text,
)


BIBLIOGRAPHIC_AUDITS = "scienti_bibliographic_enrichment_audits"
MEASUREMENT_RUNS = "minciencias_measurement_runs"
SUSPICIOUS_VOLUME_RE = re.compile(
    r"(?:^(?:p(?:[aá]gs?|[aá]ginas)\.?|pages?)\s*:?\s*$|"
    r"\b(?:fasc(?:[ií]culo)?\.?|p(?:[aá]gs?|[aá]ginas)\.?|pages?|"
    r"ISBN|ISSN|DOI|Autores?)\s*:)",
    re.IGNORECASE,
)
SUSPICIOUS_EDITION_RE = re.compile(
    r"\b(?:ISBN|ISSN|Serie|Autores?|Autor del documento original|"
    r"Medio de divulgaci[oó]n|Idiomas?)\s*:",
    re.IGNORECASE,
)
SUSPICIOUS_PUBLICATION_PLACE_RE = re.compile(
    r"^(?:(?:19|20)\d{2}|(?:ISBN|ISSN|Autores?|Edici[oó]n|Serie)\b)",
    re.IGNORECASE,
)
# MongoDB's \w/\W semantics reject accented letters, so use an explicit
# Latin character range for values already bounded by the parser.
AUDIT_VALID_LANGUAGE_RE = re.compile(
    r"^[A-Za-zÀ-ÖØ-öø-ÿ]+(?:[ /-][A-Za-zÀ-ÖØ-öø-ÿ]+){0,5}$"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ScientiBibliographicEnrichmentAuditor:
    """Prove that final materialization preserves clean explicit metadata."""

    def __init__(self, db):
        self.db = db

    def audit(
        self,
        *,
        audit_name: str,
        run_name: str,
        base_collection: str,
        final_collection: str,
        materialization_run_name: str,
    ) -> dict[str, Any]:
        final_run = self.db[MEASUREMENT_RUNS].find_one(
            {"_id": materialization_run_name}
        ) or {}
        summary = final_run.get("summary") or {}
        base = self.db[base_collection]
        final = self.db[final_collection]
        publisher_query = {
            "bibliographic_info.scienti.fields.publisher.status": "consistent"
        }
        book_query = {
            "bibliographic_info.scienti.fields.book_title.status": "consistent"
        }
        conflict_query = {
            "bibliographic_info.scienti.fields.publisher.status": "conflict"
        }
        counts = {
            "base_works": base.count_documents({}),
            "final_works": final.count_documents({}),
            "base_consistent_publishers": base.count_documents(publisher_query),
            "final_consistent_publishers": final.count_documents(publisher_query),
            "base_consistent_book_titles": base.count_documents(book_query),
            "final_consistent_book_titles": final.count_documents(book_query),
            "publisher_conflicts_preserved": final.count_documents(conflict_query),
            "base_resolved_multiple_publishers": base.count_documents(
                {
                    "bibliographic_info.scienti.publisher_entities.status": (
                        "resolved_multiple"
                    )
                }
            ),
            "final_resolved_multiple_publishers": final.count_documents(
                {
                    "bibliographic_info.scienti.publisher_entities.status": (
                        "resolved_multiple"
                    )
                }
            ),
            "publisher_multiple_candidates": final.count_documents(
                {
                    "bibliographic_info.scienti.publisher_entities.status": (
                        "candidate_multiple"
                    )
                }
            ),
        }
        fields = "bibliographic_info.scienti.fields"
        checks = {
            "materialization_not_complete": int(final_run.get("status") != "complete"),
            "materialization_critical": int(summary.get("critical_anomalies") or 0),
            "final_count_mismatch": abs(
                counts["final_works"] - int(summary.get("documents") or 0)
            ),
            "publisher_loss": abs(
                counts["base_consistent_publishers"]
                - counts["final_consistent_publishers"]
            ),
            "book_title_loss": abs(
                counts["base_consistent_book_titles"]
                - counts["final_consistent_book_titles"]
            ),
            "consistent_publisher_without_source": final.count_documents(
                {
                    **publisher_query,
                    "$or": [
                        {"source.publisher.name": {"$exists": False}},
                        {"source.publisher.name": ""},
                    ],
                }
            ),
            "publisher_value_mismatch": final.count_documents(
                {
                    **publisher_query,
                    "$expr": {
                        "$ne": [
                            "$bibliographic_info.publisher.name",
                            "$source.publisher.name",
                        ]
                    },
                }
            ),
            "book_title_source_mismatch": final.count_documents(
                {
                    **book_query,
                    "$expr": {"$ne": ["$bibliographic_info.book_title", "$source.name"]},
                }
            ),
            "conflict_consolidated_as_publisher": final.count_documents(
                {**conflict_query, "bibliographic_info.publisher.name": {"$exists": True}}
            ),
            "suspicious_publisher": final.count_documents(
                {"source.publisher.name": SUSPICIOUS_PUBLISHER_RE}
            ),
            "suspicious_volume": final.count_documents(
                {f"{fields}.volume.value": SUSPICIOUS_VOLUME_RE}
            ),
            "invalid_pages": final.count_documents(
                {
                    f"{fields}.pages.value": {
                        "$exists": True,
                        "$ne": "",
                        "$not": VALID_PAGES_RE,
                    }
                }
            ),
            "invalid_language": final.count_documents(
                {
                    f"{fields}.language.value": {
                        "$exists": True,
                        "$ne": "",
                        "$not": AUDIT_VALID_LANGUAGE_RE,
                    }
                }
            ),
            "suspicious_edition": final.count_documents(
                {f"{fields}.edition.value": SUSPICIOUS_EDITION_RE}
            ),
            "suspicious_publication_place": final.count_documents(
                {f"{fields}.publication_place.value": SUSPICIOUS_PUBLICATION_PLACE_RE}
            ),
            "publisher_entity_resolution_loss": abs(
                counts["base_resolved_multiple_publishers"]
                - counts["final_resolved_multiple_publishers"]
            ),
            "invalid_publisher_entities": 0,
            "publisher_entity_raw_mismatch": 0,
            "publisher_entities_without_consistent_source": final.count_documents(
                {
                    "bibliographic_info.scienti.publisher_entities": {
                        "$exists": True
                    },
                    f"{fields}.publisher.status": {"$ne": "consistent"},
                }
            ),
        }
        entity_path = "bibliographic_info.scienti.publisher_entities"
        cursor = final.find(
            {entity_path: {"$exists": True}},
            {
                "source.publisher.name": 1,
                entity_path: 1,
            },
        )
        for document in cursor:
            source = document.get("source") or {}
            source = source if isinstance(source, dict) else {}
            source_publisher = source.get("publisher") or {}
            source_publisher = (
                source_publisher if isinstance(source_publisher, dict) else {}
            )
            source_name = str(source_publisher.get("name") or "").strip()
            bibliographic_info = document.get("bibliographic_info") or {}
            bibliographic_info = (
                bibliographic_info if isinstance(bibliographic_info, dict) else {}
            )
            scienti = bibliographic_info.get("scienti") or {}
            scienti = scienti if isinstance(scienti, dict) else {}
            raw_entity_data = scienti.get("publisher_entities")
            valid_container = isinstance(raw_entity_data, dict)
            entity_data = raw_entity_data if valid_container else {}
            raw_value = str(entity_data.get("raw_value") or "").strip()
            if not raw_value or source_name != raw_value:
                checks["publisher_entity_raw_mismatch"] += 1
            status = entity_data.get("status")
            items_key = (
                "entities" if status == "resolved_multiple" else "candidate_entities"
            )
            items = entity_data.get(items_key)
            item_list = items if isinstance(items, list) else []
            normalized_names = [
                str(item.get("normalized_name") or "").strip()
                for item in item_list
                if isinstance(item, dict)
            ]
            display_names = [
                str(item.get("name") or "").strip()
                for item in item_list
                if isinstance(item, dict)
            ]
            valid_status = status in {"resolved_multiple", "candidate_multiple"}
            resolved_are_authorities = status != "resolved_multiple" or all(
                isinstance(item, dict) and item.get("authority_id")
                for item in item_list
            )
            valid_confidence = entity_data.get("confidence") == (
                "high" if status == "resolved_multiple" else "medium"
            )
            valid_names = (
                len(normalized_names) == len(item_list)
                and len(display_names) == len(item_list)
                and all(
                    normalized_name == norm_text(display_name)
                    for display_name, normalized_name in zip(
                        display_names, normalized_names
                    )
                )
            )
            mutually_exclusive_items = not (
                entity_data.get("entities") and entity_data.get("candidate_entities")
            )
            if (
                not valid_container
                or not valid_status
                or not isinstance(items, list)
                or len(items) < 2
                or not valid_names
                or len(set(normalized_names)) != len(normalized_names)
                or not resolved_are_authorities
                or not valid_confidence
                or not entity_data.get("rule")
                or not entity_data.get("rule_version")
                or not mutually_exclusive_items
            ):
                checks["invalid_publisher_entities"] += 1
        critical = sum(checks.values())
        report = {
            "_id": audit_name,
            "run_name": run_name,
            "status": "passed" if critical == 0 else "failed",
            "audited_at": utc_now(),
            "collections": {
                "base": base_collection,
                "final": final_collection,
            },
            "materialization_run": materialization_run_name,
            "materialization_summary": deepcopy(summary),
            "counts": counts,
            "checks": checks,
            "critical_anomalies": critical,
        }
        self.db[BIBLIOGRAPHIC_AUDITS].replace_one(
            {"_id": audit_name}, report, upsert=True
        )
        return report
