"""Final bibliographic enrichment gate for published Scienti works."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any

from yuku.cvlac_related_works import (
    SUSPICIOUS_PUBLISHER_RE,
    VALID_PAGES_RE,
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
        }
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
