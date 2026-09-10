from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from hashlib import sha256
import json
import re
from typing import Any

from bs4 import BeautifulSoup
from pymongo import ASCENDING

from yuku.cvlac_related_works import (
    extract_profile_author_name,
    norm_text,
    normalize_related_works_document,
)
from yuku.scienti_profiles import ScientiProfileDownloader


COMPARISON_VERSION = "1.0.2"
CATEGORIES = ("production", "patents", "events", "projects")
PRESENTATION_ONLY_FIELDS = {"profile_id", "order"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_value(value: Any) -> Any:
    """Remove presentation-only differences while preserving parsed metadata."""
    if isinstance(value, dict):
        result = {}
        for key, item in sorted(value.items()):
            if key in PRESENTATION_ONLY_FIELDS:
                continue
            if key == "oriented_people":
                combined = " ".join(str(person) for person in (item or []))
                result[key] = sorted(re.findall(r"[a-z0-9]+", norm_text(combined)))
            else:
                result[key] = canonical_value(item)
        return result
    if isinstance(value, list):
        normalized = [canonical_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


def record_fingerprint(record: dict) -> str:
    payload = json.dumps(
        canonical_value(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def record_identity(category: str, record: dict) -> str:
    title = norm_text(str(record.get("title") or ""))
    year = record.get("year") or record.get("start_year") or record.get("start_date")
    dois = sorted(norm_text(value) for value in record.get("doi", []) if value)
    if category == "production" and dois:
        return "doi:" + "|".join(dois) + f"|{title}|{year or ''}"
    isbns = sorted(
        re.sub(r"[^0-9x]", "", str(value).lower())
        for value in record.get("isbn", [])
        if value
    )
    if category in {"production", "patents"} and isbns:
        return "isbn:" + "|".join(isbns) + f"|{title}|{year or ''}"
    kind = norm_text(
        str(
            record.get("type_impactu")
            or record.get("product_type")
            or record.get("event_type")
            or record.get("project_type")
            or category
        )
    )
    return f"{kind}|{title}|{year or ''}"


def record_brief(record: dict, fingerprint: str) -> dict:
    return {
        key: value
        for key, value in {
            "fingerprint": fingerprint,
            "type": record.get("type_impactu")
            or record.get("product_type")
            or record.get("event_type")
            or record.get("project_type"),
            "title": record.get("title"),
            "year": record.get("year")
            or record.get("start_year")
            or record.get("start_date"),
            "doi": record.get("doi"),
            "isbn": record.get("isbn"),
        }.items()
        if value not in (None, "", [])
    }


def record_match_score(category: str, old: dict, new: dict) -> float:
    old_title = norm_text(str(old.get("title") or ""))
    new_title = norm_text(str(new.get("title") or ""))
    if min(len(old_title), len(new_title)) < 5:
        return 0.0
    old_year = old.get("year") or old.get("start_year") or old.get("start_date")
    new_year = new.get("year") or new.get("start_year") or new.get("start_date")
    if old_title == new_title and old_year == new_year:
        return 1.0
    old_type = norm_text(
        str(old.get("type_impactu") or old.get("product_type") or category)
    )
    new_type = norm_text(
        str(new.get("type_impactu") or new.get("product_type") or category)
    )
    if old_type != new_type:
        return 0.0
    similarity = SequenceMatcher(None, old_title, new_title).ratio()
    old_dois = {norm_text(value) for value in old.get("doi", []) if value}
    new_dois = {norm_text(value) for value in new.get("doi", []) if value}
    if old_dois & new_dois and similarity >= 0.65:
        return 1.0
    old_isbns = {
        re.sub(r"[^0-9x]", "", str(value).lower())
        for value in old.get("isbn", [])
        if value
    }
    new_isbns = {
        re.sub(r"[^0-9x]", "", str(value).lower())
        for value in new.get("isbn", [])
        if value
    }
    if old_isbns & new_isbns and similarity >= 0.8:
        return 0.995
    if old_title == new_title:
        return 0.99
    years_compatible = old_year == new_year
    if isinstance(old_year, int) and isinstance(new_year, int):
        years_compatible = abs(old_year - new_year) <= 1
    return similarity if years_compatible and similarity >= 0.94 else 0.0


def changed_fields(old: dict, new: dict) -> list[str]:
    fields = (set(old) | set(new)) - PRESENTATION_ONLY_FIELDS
    return sorted(
        field
        for field in fields
        if canonical_value(old.get(field)) != canonical_value(new.get(field))
    )


def compare_records(
    old_records: list[dict], new_records: list[dict], category: str = "production"
) -> dict:
    old_fingerprints = [record_fingerprint(record) for record in old_records]
    new_fingerprints = [record_fingerprint(record) for record in new_records]
    old_counter = Counter(old_fingerprints)
    new_counter = Counter(new_fingerprints)
    unchanged = old_counter & new_counter
    fingerprint_added = new_counter - old_counter
    fingerprint_removed = old_counter - new_counter
    old_by_fingerprint = {
        fingerprint: record for fingerprint, record in zip(old_fingerprints, old_records)
    }
    new_by_fingerprint = {
        fingerprint: record for fingerprint, record in zip(new_fingerprints, new_records)
    }

    old_remaining = list(fingerprint_removed.elements())
    new_remaining = list(fingerprint_added.elements())
    candidates = []
    for old_index, old_fingerprint in enumerate(old_remaining):
        for new_index, new_fingerprint in enumerate(new_remaining):
            score = record_match_score(
                category,
                old_by_fingerprint[old_fingerprint],
                new_by_fingerprint[new_fingerprint],
            )
            if score:
                candidates.append((score, old_index, new_index))
    matched_old: set[int] = set()
    matched_new: set[int] = set()
    modified_examples = []
    field_counts = Counter()
    for score, old_index, new_index in sorted(candidates, reverse=True):
        if old_index in matched_old or new_index in matched_new:
            continue
        matched_old.add(old_index)
        matched_new.add(new_index)
        old_record = old_by_fingerprint[old_remaining[old_index]]
        new_record = new_by_fingerprint[new_remaining[new_index]]
        fields = changed_fields(old_record, new_record)
        field_counts.update(fields)
        if len(modified_examples) < 10:
            modified_examples.append(
                {
                    "score": round(score, 4),
                    "changed_fields": fields,
                    "old": record_brief(old_record, old_remaining[old_index]),
                    "new": record_brief(new_record, new_remaining[new_index]),
                }
            )
    removed_only = [
        fingerprint
        for index, fingerprint in enumerate(old_remaining)
        if index not in matched_old
    ]
    added_only = [
        fingerprint
        for index, fingerprint in enumerate(new_remaining)
        if index not in matched_new
    ]
    modified_records = len(matched_old)
    return {
        "old_count": len(old_records),
        "new_count": len(new_records),
        "delta": len(new_records) - len(old_records),
        "unchanged_count": sum(unchanged.values()),
        "added_count": len(added_only),
        "removed_count": len(removed_only),
        "modified_record_count": modified_records,
        "fingerprint_added_count": sum(fingerprint_added.values()),
        "fingerprint_removed_count": sum(fingerprint_removed.values()),
        "modified_identity_count": modified_records,
        "modified_field_counts": dict(sorted(field_counts.items())),
        "added_examples": [
            record_brief(new_by_fingerprint[value], value)
            for value in added_only[:10]
        ],
        "removed_examples": [
            record_brief(old_by_fingerprint[value], value)
            for value in removed_only[:10]
        ],
        "modified_examples": modified_examples,
    }


class CvlacEvaluationComparator:
    """Compare legacy and refreshed raw profiles without changing either source."""

    def __init__(
        self,
        db,
        *,
        run_name: str,
        reference_collection: str,
        refreshed_collection: str,
        cohort_collection: str,
        output_collection: str,
    ):
        self.db = db
        self.run_name = run_name
        self.reference_collection = reference_collection
        self.refreshed_collection = refreshed_collection
        self.cohort_collection = cohort_collection
        self.output_collection = output_collection

    @staticmethod
    def _status(html: str) -> str:
        return ScientiProfileDownloader.classify_html("cvlac", html)

    @staticmethod
    def _name(html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        return extract_profile_author_name(soup)

    def compare(self) -> dict:
        required = {
            self.reference_collection,
            self.refreshed_collection,
            self.cohort_collection,
        }
        missing = required - set(self.db.list_collection_names())
        if missing:
            raise ValueError(f"comparison source collections not found: {sorted(missing)}")

        cohort = list(
            self.db[self.cohort_collection]
            .find({"run_name": self.run_name, "segment": "refresh"})
            .sort("rank", ASCENDING)
        )
        destination = self.db[self.output_collection]
        summary = Counter()
        category_totals = {
            category: Counter() for category in CATEGORIES
        }
        category_field_totals = {
            category: Counter() for category in CATEGORIES
        }

        for position, item in enumerate(cohort, start=1):
            profile_id = str(item["_id"])
            try:
                old_raw = self.db[self.reference_collection].find_one(
                    {"_id": profile_id}, {"html": 1}
                )
                new_raw = self.db[self.refreshed_collection].find_one(
                    {"_id": profile_id}, {"html": 1}
                )
                if not old_raw or not new_raw:
                    raise ValueError("old or refreshed raw HTML is missing")
                old_html = old_raw.get("html", "")
                new_html = new_raw.get("html", "")
                old_status = self._status(old_html)
                new_status = self._status(new_html)
                transition = f"{old_status}_to_{new_status}"
                summary[f"status_{transition}"] += 1
                old_name = self._name(old_html)
                new_name = self._name(new_html)
                name_changed = norm_text(old_name) != norm_text(new_name)
                if name_changed:
                    summary["name_changed"] += 1

                document = {
                    "_id": profile_id,
                    "run_name": self.run_name,
                    "comparison_version": COMPARISON_VERSION,
                    "compared_at": utc_now(),
                    "status": {
                        "old": old_status,
                        "new": new_status,
                        "transition": transition,
                    },
                    "name": {
                        "old": old_name,
                        "new": new_name,
                        "changed": name_changed,
                    },
                    "categories": {},
                    "comparable": old_status == "downloaded" and new_status == "downloaded",
                    "risk_flags": [],
                }
                if document["comparable"]:
                    summary["comparable"] += 1
                    old_normalized = normalize_related_works_document(profile_id, old_html)
                    new_normalized = normalize_related_works_document(profile_id, new_html)
                    profile_changed = name_changed
                    for category in CATEGORIES:
                        comparison = compare_records(
                            old_normalized[category],
                            new_normalized[category],
                            category,
                        )
                        document["categories"][category] = comparison
                        totals = category_totals[category]
                        for field in (
                            "old_count",
                            "new_count",
                            "added_count",
                            "removed_count",
                            "unchanged_count",
                            "modified_record_count",
                            "fingerprint_added_count",
                            "fingerprint_removed_count",
                            "modified_identity_count",
                        ):
                            totals[field] += comparison[field]
                        category_field_totals[category].update(
                            comparison["modified_field_counts"]
                        )
                        if (
                            comparison["added_count"]
                            or comparison["removed_count"]
                            or comparison["modified_record_count"]
                        ):
                            totals["profiles_changed"] += 1
                            profile_changed = True
                        old_count = comparison["old_count"]
                        if (
                            comparison["removed_count"] >= 20
                            and comparison["removed_count"] >= old_count * 0.5
                        ):
                            document["risk_flags"].append(f"large_{category}_removal")
                        if comparison["added_count"] >= 50:
                            document["risk_flags"].append(f"large_{category}_addition")
                    document["normalized_changed"] = profile_changed
                    summary["normalized_changed" if profile_changed else "normalized_unchanged"] += 1
                else:
                    summary["not_comparable"] += 1
                    if old_status == "downloaded" and new_status != "downloaded":
                        document["risk_flags"].append("public_became_inaccessible")
                if name_changed:
                    document["risk_flags"].append("name_changed")
                document["review_required"] = bool(document["risk_flags"])
                if document["review_required"]:
                    summary["review_required"] += 1
                destination.replace_one({"_id": profile_id}, document, upsert=True)
            except Exception as error:
                summary["parse_errors"] += 1
                destination.replace_one(
                    {"_id": profile_id},
                    {
                        "_id": profile_id,
                        "run_name": self.run_name,
                        "comparison_version": COMPARISON_VERSION,
                        "compared_at": utc_now(),
                        "error": f"{type(error).__name__}: {error}",
                        "review_required": True,
                    },
                    upsert=True,
                )
            if position % 50 == 0 or position == len(cohort):
                print(
                    f"{utc_now().isoformat()} INFO: compared refreshed CVLAC "
                    f"profiles {position}/{len(cohort)}, summary={dict(summary)}",
                    flush=True,
                )

        destination.create_index("status.transition")
        destination.create_index("normalized_changed")
        destination.create_index("review_required")
        destination.create_index("risk_flags")
        result = {
            "run_name": self.run_name,
            "comparison_version": COMPARISON_VERSION,
            "profiles": len(cohort),
            **dict(sorted(summary.items())),
            "categories": {
                category: {
                    **dict(sorted(values.items())),
                    "modified_fields": dict(
                        sorted(category_field_totals[category].items())
                    ),
                }
                for category, values in category_totals.items()
            },
            "output_collection": self.output_collection,
        }
        return result
