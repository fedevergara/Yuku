from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from hashlib import sha256
import re
from time import time
from typing import Any, Iterable
import uuid

import numpy as np
from pymongo import ASCENDING, ReplaceOne, UpdateOne

from yuku.cvlac_related_works import (
    extract_profile_works_from_html,
    norm_text,
    normalize_doi,
)
from yuku.scienti_routing import route_cvlac, route_gruplac


GRAPH_VERSION = "minciencias-work-graph-v5"
AFFILIATION_RULE_VERSION = "gruplac-product-group-membership-v1"
PUBLISHER_ENTITY_RULE_VERSION = "scienti-publisher-entities-v1"
WORK_GRAPH_PUBLICATIONS = "scienti_work_graph_publications"
CVLAC_SNAPSHOT_RUNS = "scienti_cvlac_priority_runs"
CVLAC_NORMALIZATION_AUDITS = "scienti_cvlac_normalization_audits"
GRUPLAC_NORMALIZATION_AUDITS = "scienti_gruplac_normalization_audits"
CHECKPOINT_STAGES = (
    "extract_nodes",
    "connect_doi",
    "connect_title_year",
    "materialize",
    "audit",
    "publish",
)
TITLE_CLUSTER_SIMILARITY = 0.85
GENERIC_TITLE_MIN_TOKENS = 4
GENERIC_TITLE_MIN_LENGTH = 20
DOI_IDENTITY_RE = re.compile(
    r"^https://doi\.org/10\.\d{4,9}/(?P<suffix>[^\s<>\"{}|\\^`\[\]]+)$",
    re.IGNORECASE,
)
DOI_PLACEHOLDER_RE = re.compile(
    r"(?:^|[./_-])(?:no\s*aplica|noaplica|n/?a|sin\s*doi|sindoi|pendiente)(?:$|[./_-])",
    re.IGNORECASE,
)
DOI_ISSN_SUFFIX_RE = re.compile(r"^\d{4}-\d{3}[\dXx]$")
DOI_DOCUMENT_LINK_RE = re.compile(r"(?:\.full)?\.(?:pdf|html?)$", re.IGNORECASE)


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", norm_text(value)).strip()


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", norm_text(value)).strip()


def format_person_name(value: str) -> str:
    """Use a consistent display case without changing identity normalization."""
    return re.sub(r"\s+", " ", value or "").strip().title()


def name_token_key(value: str) -> tuple[str, ...]:
    return tuple(sorted(token for token in normalize_name(value).split() if len(token) > 1))


def normalize_isbn_identity(value: str) -> str:
    """Return a usable ISBN-10/ISBN-13 identity or an empty string.

    CVLAC and GrupLAC contain placeholders such as ``0`` and malformed values.
    Those values remain available as metadata, but must not connect graph nodes.
    """
    compact = re.sub(r"[^0-9Xx]", "", str(value or "")).upper()
    if len(compact) == 10:
        if not re.fullmatch(r"\d{9}[\dX]", compact):
            return ""
        checksum = sum(
            (10 - index) * (10 if char == "X" else int(char))
            for index, char in enumerate(compact)
        )
        return compact if checksum % 11 == 0 else ""
    if len(compact) == 13 and compact.isdigit():
        checksum = sum(
            int(char) * (1 if index % 2 == 0 else 3)
            for index, char in enumerate(compact[:12])
        )
        check_digit = (10 - checksum % 10) % 10
        return compact if check_digit == int(compact[-1]) else ""
    return ""


def is_repeated_work_title(value: str, title: str) -> bool:
    """Reject title text leaked by CVLAC into the oriented-person field."""
    value_key = normalize_title(value)
    title_key = normalize_title(title)
    if len(value_key.split()) < 6 or not title_key:
        return False
    return SequenceMatcher(None, value_key, title_key).ratio() >= TITLE_CLUSTER_SIMILARITY


def title_fingerprint(title_key: str, year: int | None) -> str:
    identity = f"{GRAPH_VERSION}|title|{title_key}|{year or ''}"
    return sha256(identity.encode("utf-8")).hexdigest()


def stable_partition(value: str, partitions: int) -> int:
    """Assign a value to a reproducible partition without Python's salted hash."""
    partitions = max(1, int(partitions))
    digest = sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % partitions


def bibliographic_authority_keys(
    *,
    title: str,
    year: int | None,
    family: str,
    dois: Iterable[str] = (),
    isbns: Iterable[str] = (),
) -> list[str]:
    keys = []
    for doi in sorted({normalize_doi(value) for value in dois if value}):
        if is_identity_doi(doi):
            keys.append(f"doi|{doi}")
    title_key = normalize_title(title)
    for isbn in sorted(
        {normalized for value in isbns if (normalized := normalize_isbn_identity(value))}
    ):
        keys.append(f"isbn|{isbn}|{title_key}|{year or ''}|{family}")
    if title_key and year and family:
        keys.append(f"title|{title_key}|{year}|{family}")
    return keys


def is_identity_doi(value: str) -> bool:
    """Whether a DOI is specific enough to propose graph edges.

    Rejected values are still retained as work metadata.
    """
    value = normalize_doi(value)
    match = DOI_IDENTITY_RE.fullmatch(value)
    if not match:
        return False
    suffix = match.group("suffix").strip("./_- ")
    suffix_n = norm_text(suffix)
    if len(suffix) < 6:
        return False
    if "issn" in suffix_n or DOI_PLACEHOLDER_RE.search(suffix_n):
        return False
    if DOI_ISSN_SUFFIX_RE.fullmatch(suffix) or DOI_DOCUMENT_LINK_RE.search(suffix):
        return False
    if "?" in suffix or "#" in suffix:
        return False
    if value.startswith("https://doi.org/10.0000/"):
        return False
    # CVLAC contains journal-level DOI prefixes ending only in an acronym.
    if not any(char.isdigit() for char in suffix):
        return False
    return True


def type_family(value: str) -> str:
    value_n = norm_text(value)
    if value_n == "articulo de revista":
        return "article"
    if value_n == "publicaciones editoriales no especializadas":
        return "editorial"
    if value_n == "libro":
        return "book"
    if value_n == "capitulo de libro":
        return "book_chapter"
    if value_n == "tesis de pregrado":
        return "undergraduate_thesis"
    if value_n == "tesis de posgrado":
        return "graduate_thesis"
    if value_n.startswith("trabajos dirigidos/tutorias"):
        return "directed_work"
    return value_n or "unknown"


def is_generic_title(title_key: str) -> bool:
    tokens = [token for token in title_key.split() if len(token) > 2]
    return len(tokens) < GENERIC_TITLE_MIN_TOKENS or len(title_key) < GENERIC_TITLE_MIN_LENGTH


def mongo_safe(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, dict):
        return {mongo_safe(key): mongo_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mongo_safe(item) for item in value]
    if isinstance(value, tuple):
        return [mongo_safe(item) for item in value]
    return value


def validate_scienti_graph_gate(
    db,
    *,
    snapshot_run_name: str,
    cvlac_normalized_collection: str,
    cvlac_audit_name: str,
    recognized_groups_collection: str,
    gruplac_raw_collection: str,
    gruplac_normalized_collection: str,
    gruplac_audit_name: str,
) -> dict[str, Any]:
    """Require exact, audited normalized universes before graph construction."""
    required = {
        cvlac_normalized_collection,
        recognized_groups_collection,
        gruplac_raw_collection,
        gruplac_normalized_collection,
        CVLAC_SNAPSHOT_RUNS,
        CVLAC_NORMALIZATION_AUDITS,
        GRUPLAC_NORMALIZATION_AUDITS,
    }
    missing = sorted(required - set(db.list_collection_names()))
    if missing:
        raise RuntimeError(f"required graph gate collections are missing: {missing}")

    snapshot = db[CVLAC_SNAPSHOT_RUNS].find_one({"_id": snapshot_run_name})
    if not snapshot or snapshot.get("status") != "complete":
        raise RuntimeError(
            f"CVLAC snapshot {snapshot_run_name!r} is not complete"
        )
    snapshot_config = snapshot.get("config") or {}
    raw_collection = str(snapshot_config.get("raw_collection") or "")
    expected_profiles = int(snapshot.get("target_count") or 0)
    if not raw_collection or raw_collection not in db.list_collection_names():
        raise RuntimeError("complete CVLAC snapshot does not expose its raw source")
    raw_profiles = db[raw_collection].count_documents({})
    normalized_profiles = db[cvlac_normalized_collection].count_documents({})
    if not expected_profiles or raw_profiles != expected_profiles:
        raise RuntimeError(
            f"CVLAC raw coverage is {raw_profiles}/{expected_profiles}"
        )
    if normalized_profiles != expected_profiles:
        raise RuntimeError(
            f"CVLAC normalized coverage is {normalized_profiles}/{expected_profiles}"
        )

    cvlac_audit = db[CVLAC_NORMALIZATION_AUDITS].find_one(
        {"_id": cvlac_audit_name}
    )
    if not cvlac_audit or cvlac_audit.get("status") != "passed":
        raise RuntimeError(f"CVLAC audit {cvlac_audit_name!r} is not passed")
    cvlac_audit_config = cvlac_audit.get("config") or {}
    cvlac_summary = cvlac_audit.get("summary") or {}
    if cvlac_audit_config.get("destination_collection") != cvlac_normalized_collection:
        raise RuntimeError("CVLAC audit points to a different normalized collection")
    if (
        int(cvlac_summary.get("critical_anomalies") or 0) != 0
        or int(cvlac_summary.get("stored_parser_errors") or 0) != 0
        or int(cvlac_summary.get("source_profiles") or 0) != expected_profiles
        or int(cvlac_summary.get("destination_profiles") or 0) != expected_profiles
    ):
        raise RuntimeError("CVLAC audit summary does not prove exact clean coverage")

    expected_groups = db[recognized_groups_collection].count_documents(
        {"url_gruplac": {"$type": "string", "$ne": ""}}
    )
    raw_groups = db[gruplac_raw_collection].count_documents({})
    normalized_groups = db[gruplac_normalized_collection].count_documents({})
    if not expected_groups or not (
        expected_groups == raw_groups == normalized_groups
    ):
        raise RuntimeError(
            "GrupLAC coverage is incomplete: "
            f"expected={expected_groups} raw={raw_groups} "
            f"normalized={normalized_groups}"
        )
    gruplac_audit = db[GRUPLAC_NORMALIZATION_AUDITS].find_one(
        {"_id": gruplac_audit_name}
    )
    if not gruplac_audit or gruplac_audit.get("status") != "passed":
        raise RuntimeError(f"GrupLAC audit {gruplac_audit_name!r} is not passed")
    gruplac_audit_config = gruplac_audit.get("config") or {}
    gruplac_summary = gruplac_audit.get("summary") or {}
    if (
        gruplac_audit_config.get("normalized_collection")
        != gruplac_normalized_collection
        or gruplac_audit_config.get("raw_collection") != gruplac_raw_collection
    ):
        raise RuntimeError("GrupLAC audit points to different source collections")
    if (
        int(gruplac_summary.get("critical_anomalies") or 0) != 0
        or int(gruplac_summary.get("stored_parser_errors") or 0) != 0
        or int(gruplac_summary.get("expected_groups") or 0) != expected_groups
        or int(gruplac_summary.get("raw_groups") or 0) != expected_groups
        or int(gruplac_summary.get("normalized_groups") or 0) != expected_groups
    ):
        raise RuntimeError("GrupLAC audit summary does not prove exact clean coverage")

    affiliation_evidence = group_affiliation_evidence_fingerprint(
        db,
        recognized_groups_collection,
        gruplac_normalized_collection,
    )

    return {
        "snapshot_run_name": snapshot_run_name,
        "cvlac_raw_collection": raw_collection,
        "cvlac_normalized_collection": cvlac_normalized_collection,
        "cvlac_audit_name": cvlac_audit_name,
        "cvlac_audit_finished_at": cvlac_audit.get("finished_at"),
        "cvlac_parser_versions": cvlac_summary.get("parser_version_counts") or {},
        "cvlac_profiles": expected_profiles,
        "recognized_groups_collection": recognized_groups_collection,
        "gruplac_raw_collection": gruplac_raw_collection,
        "gruplac_normalized_collection": gruplac_normalized_collection,
        "gruplac_audit_name": gruplac_audit_name,
        "gruplac_audit_finished_at": gruplac_audit.get("finished_at"),
        "gruplac_parser_versions": gruplac_summary.get("parser_version_counts") or {},
        "gruplac_groups": expected_groups,
        "affiliation_evidence_sha256": affiliation_evidence["sha256"],
        "affiliation_historical_institutions": affiliation_evidence[
            "historical_institutions"
        ],
        "affiliation_memberships": affiliation_evidence["memberships"],
        "validated_at": int(time()),
    }


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}
        self.size: dict[str, int] = {}
        self.summary: dict[str, dict[str, set]] = {}

    def add(self, node: dict[str, Any]) -> None:
        node_id = node["_id"]
        if node_id in self.parent:
            return
        self.parent[node_id] = node_id
        self.size[node_id] = 1
        self.summary[node_id] = {
            "titles": {node["title_key"]},
            "years": {node["year"]} if node.get("year") is not None else set(),
            "families": {node["type_family"]} if node.get("type_family") != "unknown" else set(),
            "identity_dois": set(node.get("identity_dois") or []),
            "identity_isbns": set(node.get("identity_isbns") or []),
        }

    def find(self, node_id: str) -> str:
        parent = self.parent[node_id]
        if parent != node_id:
            self.parent[node_id] = self.find(parent)
        return self.parent[node_id]

    def can_union_by_title(self, left_id: str, right_id: str) -> tuple[bool, str]:
        left_root = self.find(left_id)
        right_root = self.find(right_id)
        if left_root == right_root:
            return True, "already_connected"
        left = self.summary[left_root]
        right = self.summary[right_root]

        years = left["years"] | right["years"]
        if len(years) > 1:
            return False, "year_conflict"

        families = left["families"] | right["families"]
        if len(families) > 1:
            return False, "type_conflict"

        if (
            left["identity_dois"]
            and right["identity_dois"]
            and left["identity_dois"].isdisjoint(right["identity_dois"])
        ):
            return False, "doi_conflict"

        if families == {"book"}:
            left_isbns = left["identity_isbns"]
            right_isbns = right["identity_isbns"]
            if left_isbns and right_isbns and left_isbns.isdisjoint(right_isbns):
                return False, "isbn_conflict"

        for left_title in left["titles"]:
            for right_title in right["titles"]:
                similarity = SequenceMatcher(None, left_title, right_title).ratio()
                if similarity < TITLE_CLUSTER_SIMILARITY:
                    return False, "transitive_title_drift"
        return True, "compatible"

    def can_union_by_doi(self, left_id: str, right_id: str) -> tuple[bool, str]:
        left_root = self.find(left_id)
        right_root = self.find(right_id)
        if left_root == right_root:
            return True, "already_connected"
        left = self.summary[left_root]
        right = self.summary[right_root]

        years = left["years"] | right["years"]
        if years and max(years) - min(years) > 1:
            return False, "doi_year_conflict"

        families = left["families"] | right["families"]
        if len(families) > 1:
            return False, "doi_type_conflict"

        for left_title in left["titles"]:
            for right_title in right["titles"]:
                similarity = SequenceMatcher(None, left_title, right_title).ratio()
                if similarity < TITLE_CLUSTER_SIMILARITY:
                    return False, "doi_title_conflict"
        return True, "compatible"

    def union(self, left_id: str, right_id: str) -> tuple[str, bool]:
        left_root = self.find(left_id)
        right_root = self.find(right_id)
        if left_root == right_root:
            return left_root, False
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        self.size[left_root] += self.size.pop(right_root)
        for key in (
            "titles",
            "years",
            "families",
            "identity_dois",
            "identity_isbns",
        ):
            self.summary[left_root][key].update(self.summary[right_root][key])
        self.summary.pop(right_root)
        return left_root, True


class CompactUnionFind(UnionFind):
    """Array-backed Union-Find for the multi-million-node definitive graph.

    Only nodes that participate in a candidate group receive bibliographic
    summaries. Parent and component-size state use fixed-width arrays, avoiding
    one Python string and several dictionary entries per graph occurrence.
    """

    def __init__(self, capacity: int):
        capacity = int(capacity)
        if capacity < 0 or capacity >= np.iinfo(np.int32).max:
            raise ValueError("compact graph capacity must fit in a signed int32")
        self.capacity = capacity
        self.parent = np.arange(capacity, dtype=np.int32)
        self.size = np.ones(capacity, dtype=np.uint32)
        self.active = np.zeros(capacity, dtype=np.bool_)
        self.summary: dict[int, dict[str, set]] = {}

    @property
    def storage_bytes(self) -> int:
        return int(self.parent.nbytes + self.size.nbytes + self.active.nbytes)

    def add(self, node: dict[str, Any]) -> None:
        node_seq = int(node["node_seq"])
        if node_seq < 0 or node_seq >= self.capacity:
            raise ValueError(f"node_seq {node_seq} is outside compact graph capacity")
        if self.active[node_seq]:
            return
        self.active[node_seq] = True
        self.summary[node_seq] = {
            "titles": {node["title_key"]},
            "years": {node["year"]} if node.get("year") is not None else set(),
            "families": (
                {node["type_family"]}
                if node.get("type_family") != "unknown"
                else set()
            ),
            "identity_dois": set(node.get("identity_dois") or []),
            "identity_isbns": set(node.get("identity_isbns") or []),
        }

    def find(self, node_seq: int) -> int:
        node_seq = int(node_seq)
        root = node_seq
        while int(self.parent[root]) != root:
            root = int(self.parent[root])
        while int(self.parent[node_seq]) != node_seq:
            parent = int(self.parent[node_seq])
            self.parent[node_seq] = root
            node_seq = parent
        return root

    def union(self, left_seq: int, right_seq: int) -> tuple[int, bool]:
        left_root = self.find(left_seq)
        right_root = self.find(right_seq)
        if left_root == right_root:
            return left_root, False
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        self.size[left_root] += self.size[right_root]
        self.size[right_root] = 0
        for key in (
            "titles",
            "years",
            "families",
            "identity_dois",
            "identity_isbns",
        ):
            self.summary[left_root][key].update(self.summary[right_root][key])
        self.summary.pop(right_root)
        return left_root, True


def author_evidence(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_owner = name_token_key(left.get("profile_author", ""))
    right_owner = name_token_key(right.get("profile_author", ""))
    left_oriented = {
        name_token_key(name) for name in left.get("oriented_people", [])
    }
    right_oriented = {
        name_token_key(name) for name in right.get("oriented_people", [])
    }
    left_oriented.discard(())
    right_oriented.discard(())
    if left.get("profile_id") == right.get("profile_id"):
        return bool(left_oriented.intersection(right_oriented))

    left_names = {
        name_token_key(name)
        for name in left.get("authors", []) + left.get("oriented_people", [])
    }
    right_names = {
        name_token_key(name)
        for name in right.get("authors", []) + right.get("oriented_people", [])
    }
    left_names.discard(())
    right_names.discard(())

    if left_owner and left_owner in right_names:
        return True
    if right_owner and right_owner in left_names:
        return True
    return bool(left_names.intersection(right_names))


def compact_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": node["_id"],
        "profile_id": node.get("profile_id"),
        "profile_author": node.get("profile_author"),
        "source_kind": node.get("source_kind", "cvlac"),
        "source_id": node.get("source_id") or node.get("profile_id"),
        "group_code": node.get("group_code"),
        "title": node.get("title"),
        "year": node.get("year"),
        "type_impactu": node.get("type_impactu"),
        "product_type": node.get("product_type"),
        "dois": node.get("dois", []),
        "identity_dois": node.get("identity_dois", []),
        "identity_isbns": node.get("identity_isbns", []),
    }


def uniq_strings(values: Iterable[Any]) -> list[str]:
    found: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        key = norm_text(value)
        if value and key and key not in found:
            found[key] = value
    return [found[key] for key in sorted(found)]


BIBLIOGRAPHIC_FIELDS = (
    "publisher",
    "book_title",
    "edition",
    "volume",
    "pages",
    "start_page",
    "end_page",
    "publication_place",
    "language",
    "dissemination_medium",
)


CURATED_COMPOSITE_PUBLISHERS = (
    {
        "raw_names": (
            "Instituto Alexander Von Humboldt Instituto De Ciencias "
            "Naturales De La Universidad Nacional",
        ),
        "entities": (
            {
                "authority_id": "publisher:instituto-alexander-von-humboldt",
                "name": "Instituto Alexander von Humboldt",
            },
            {
                "authority_id": (
                    "publisher:instituto-ciencias-naturales-universidad-nacional"
                ),
                "name": (
                    "Instituto de Ciencias Naturales de la Universidad Nacional"
                ),
            },
        ),
    },
)
PUBLISHER_ENTITY_MARKER_RE = re.compile(
    r"\b(?:editorial(?:es)?|ediciones?|editores?|editora|publishing|"
    r"publishers?|press|universidad|university|universit[eà]|instituto)\b",
    re.IGNORECASE,
)
PUBLISHER_SUBUNIT_MARKER_RE = re.compile(
    r"\b(?:departamento|facultad|escuela|programa|divisi[oó]n|direcci[oó]n)\b",
    re.IGNORECASE,
)


def _publisher_entity(name: str, authority_id: str | None = None) -> dict[str, str]:
    entity = {
        "name": name,
        "normalized_name": norm_text(name),
    }
    if authority_id:
        entity["authority_id"] = authority_id
    return entity


def _looks_like_publisher_entity(name: str) -> bool:
    return bool(
        name
        and len(name) <= 200
        and PUBLISHER_ENTITY_MARKER_RE.search(name)
        and not PUBLISHER_SUBUNIT_MARKER_RE.search(name)
    )


def publisher_entity_evidence(value: str) -> dict[str, Any] | None:
    """Resolve curated composites and flag only strong split candidates.

    This enrichment never replaces the source publisher string. Unverified
    separator-based splits remain candidates and are not canonical entities.
    """
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    raw_key = norm_text(raw_value)
    for authority in CURATED_COMPOSITE_PUBLISHERS:
        if raw_key not in {norm_text(name) for name in authority["raw_names"]}:
            continue
        return {
            "status": "resolved_multiple",
            "raw_value": raw_value,
            "rule": "curated_exact_composite",
            "rule_version": PUBLISHER_ENTITY_RULE_VERSION,
            "confidence": "high",
            "entities": [
                _publisher_entity(entity["name"], entity["authority_id"])
                for entity in authority["entities"]
            ],
        }

    split_rule = ""
    if re.search(r"\s+/\s+", raw_value):
        parts = re.split(r"\s+/\s+", raw_value)
        split_rule = "spaced_slash"
    elif ";" in raw_value:
        parts = re.split(r"\s*;\s*", raw_value)
        split_rule = "semicolon"
    else:
        return None
    if len(parts) != 2 or not all(_looks_like_publisher_entity(part) for part in parts):
        return None
    if len({norm_text(part) for part in parts}) != 2:
        return None
    return {
        "status": "candidate_multiple",
        "raw_value": raw_value,
        "rule": f"explicit_{split_rule}_publisher_markers",
        "rule_version": PUBLISHER_ENTITY_RULE_VERSION,
        "confidence": "medium",
        "candidate_entities": [_publisher_entity(part) for part in parts],
    }


def bibliographic_field_evidence(
    nodes: list[dict[str, Any]], field: str
) -> dict[str, Any]:
    """Consolidate exact-normalized values while retaining every occurrence."""
    candidates: dict[str, dict[str, Any]] = {}
    for node in nodes:
        value = str(node.get(field) or "").strip()
        key = norm_text(value)
        if not key:
            continue
        candidate = candidates.setdefault(
            key,
            {"value": value, "occurrences": []},
        )
        # Prefer the richest spelling only inside the same exact-normalized
        # value (for example, the accented form). This is not fuzzy matching.
        current = str(candidate["value"])
        value_rank = (sum(ord(char) > 127 for char in value), len(value), value)
        current_rank = (
            sum(ord(char) > 127 for char in current),
            len(current),
            current,
        )
        if value_rank > current_rank:
            candidate["value"] = value
        occurrence = {
            "source_kind": str(node.get("source_kind") or ""),
            "source_id": str(node.get("source_id") or ""),
            "record_index": node.get("source_record_index"),
        }
        if occurrence not in candidate["occurrences"]:
            candidate["occurrences"].append(occurrence)
    ordered = [candidates[key] for key in sorted(candidates)]
    if not ordered:
        return {"status": "missing", "value": "", "candidates": []}
    status = "consistent" if len(ordered) == 1 else "conflict"
    return {
        "status": status,
        "value": ordered[0]["value"] if status == "consistent" else "",
        "candidates": ordered,
    }


def materialize_bibliographic_context(
    entry: dict[str, Any],
    nodes: list[dict[str, Any]],
    timestamp: int,
    isbns: list[str],
) -> None:
    if not any(
        str(node.get("type_family") or "") in {"book", "book_chapter", "editorial"}
        for node in nodes
    ):
        return
    evidence = {
        field: bibliographic_field_evidence(nodes, field)
        for field in BIBLIOGRAPHIC_FIELDS
    }
    evidence = {
        field: value
        for field, value in evidence.items()
        if value["status"] != "missing"
    }
    if not evidence:
        return

    bibliographic_info: dict[str, Any] = {}
    for field in (
        "book_title",
        "edition",
        "volume",
        "pages",
        "start_page",
        "end_page",
        "publication_place",
        "language",
        "dissemination_medium",
    ):
        value = (evidence.get(field) or {}).get("value")
        if value:
            bibliographic_info[field] = value
    publisher = (evidence.get("publisher") or {}).get("value", "")
    if publisher:
        bibliographic_info["publisher"] = {
            "name": publisher,
            "country_code": "",
        }
    bibliographic_info["scienti"] = {
        "extraction_rule": "explicit_bibliographic_labels_only",
        "fields": evidence,
    }
    publisher_entities = publisher_entity_evidence(publisher)
    if publisher_entities:
        bibliographic_info["scienti"]["publisher_entities"] = publisher_entities
    entry["bibliographic_info"] = bibliographic_info

    book_title = (evidence.get("book_title") or {}).get("value", "")
    source_name = book_title or publisher
    if not source_name:
        return
    source_type = "book" if book_title else "publisher"
    entry["source"] = {
        "name": source_name,
        "updated": [{"source": "minciencias", "time": timestamp}],
        "names": [
            {
                "lang": "",
                "name": source_name,
                "source": "minciencias",
            }
        ],
        "types": [{"source": "minciencias", "type": source_type}],
        "publisher": (
            {"name": publisher, "country_code": ""} if publisher else {}
        ),
        "external_ids": (
            [{"source": "isbn", "id": value} for value in isbns]
            if book_title
            else []
        ),
        "external_urls": [],
        "apc": {},
        "ranking": [],
    }


def choose_year(nodes: list[dict[str, Any]]) -> int | None:
    counts = Counter(node.get("year") for node in nodes if node.get("year") is not None)
    if not counts:
        return None
    return min(counts, key=lambda year: (-counts[year], year))


def choose_titles(nodes: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    grouped: dict[str, list[str]] = {}
    for node in nodes:
        grouped.setdefault(node["title_key"], []).append(node.get("title", ""))
    ordered_keys = sorted(
        grouped,
        key=lambda key: (-len(grouped[key]), -max(map(len, grouped[key])), key),
    )
    titles = []
    for key in ordered_keys:
        title = max(grouped[key], key=lambda value: (len(value), value))
        titles.append({"title": title, "lang": "", "source": "minciencias"})
    return (titles[0]["title"] if titles else ""), titles


def parse_group_membership_period(
    value: str,
) -> tuple[tuple[int, int], tuple[int, int] | None] | None:
    """Parse the month precision periods exposed by GrupLAC.

    ``None`` as the end means ``Actual``. Malformed or year-only periods are
    rejected instead of guessed because they are later used as product-level
    affiliation evidence.
    """
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    parts = re.split(r"\s+-\s+", value, maxsplit=1)
    if len(parts) != 2:
        return None

    def parse_month(text: str) -> tuple[int, int] | None:
        match = re.fullmatch(r"\s*((?:19|20)\d{2})\s*/\s*(\d{1,2})\s*", text)
        if not match:
            return None
        year, month = map(int, match.groups())
        return (year, month) if 1 <= month <= 12 else None

    start = parse_month(parts[0])
    if start is None:
        return None
    if norm_text(parts[1]) == "actual":
        return start, None
    end = parse_month(parts[1])
    if end is None or end < start:
        return None
    return start, end


def membership_covers_publication_year(value: str, year: int | None) -> bool:
    """Require GrupLAC membership to cover the complete publication year.

    Product records normally expose only a year. A membership beginning or
    ending in the middle of that year cannot safely prove that it was active
    on the unknown publication date.
    """
    if not isinstance(year, int) or not 1900 <= year <= 2100:
        return False
    period = parse_group_membership_period(value)
    if period is None:
        return False
    start, end = period
    return start <= (year, 1) and (end is None or end >= (year, 12))


def group_institution_for_year(
    history: Iterable[dict[str, Any]], year: int | None
) -> str:
    """Return only an institution proven at or around the product year.

    An exact convocatoria year is accepted. Between convocatorias, the
    closest records before and after the product must name the same
    institution after accent/case normalization. We deliberately do not
    extrapolate beyond the historical range or bridge institution changes.
    """
    if not isinstance(year, int) or not 1900 <= year <= 2100:
        return ""
    by_year: dict[int, dict[str, str]] = {}
    for record in history or []:
        if not isinstance(record, dict):
            continue
        record_year = record.get("año", record.get("anio"))
        institution = str(record.get("institucion") or "").strip()
        if not isinstance(record_year, int) or not institution:
            continue
        key = norm_text(institution)
        if key:
            by_year.setdefault(record_year, {})[key] = institution

    def unique_at(record_year: int) -> tuple[str, str] | None:
        values = by_year.get(record_year) or {}
        if len(values) != 1:
            return None
        return next(iter(values.items()))

    if year in by_year:
        exact = unique_at(year)
        return exact[1] if exact else ""

    before = [record_year for record_year in by_year if record_year < year]
    after = [record_year for record_year in by_year if record_year > year]
    if not before or not after:
        return ""
    left = unique_at(max(before))
    right = unique_at(min(after))
    if left is None or right is None or left[0] != right[0]:
        return ""
    return right[1]


def group_affiliation_evidence_fingerprint(
    db,
    recognized_groups_collection: str,
    normalized_groups_collection: str,
) -> dict[str, Any]:
    """Fingerprint the exact group evidence consumed by affiliation rules."""
    digest = sha256()
    histories = 0
    memberships = 0
    recognized = db[recognized_groups_collection].find(
        {}, {"codigo_grupo": 1, "instituciones_historicas": 1}
    ).sort("codigo_grupo", ASCENDING)
    for record in recognized:
        group_code = str(record.get("codigo_grupo") or "").strip().upper()
        values = []
        for item in record.get("instituciones_historicas") or []:
            if not isinstance(item, dict):
                continue
            year = item.get("año", item.get("anio"))
            institution = str(item.get("institucion") or "").strip()
            if not isinstance(year, int) or not institution:
                continue
            values.append((year, norm_text(institution)))
            histories += 1
        digest.update(
            (f"G|{group_code}|{repr(sorted(values))}\n").encode("utf-8")
        )

    normalized = db[normalized_groups_collection].find(
        {}, {"group_code": 1, "members": 1}
    ).sort("group_code", ASCENDING)
    for record in normalized:
        group_code = str(
            record.get("group_code") or record.get("_id") or ""
        ).strip().upper()
        values = []
        for member in record.get("members") or []:
            if not isinstance(member, dict):
                continue
            profile_id = str(member.get("cod_rh") or "").strip()
            if profile_id.isdigit():
                profile_id = profile_id.zfill(10)
            period = str(member.get("period") or "").strip()
            if (
                not profile_id.isdigit()
                or len(profile_id) != 10
                or parse_group_membership_period(period) is None
            ):
                continue
            values.append((profile_id, period))
            memberships += 1
        digest.update(
            (f"M|{group_code}|{repr(sorted(values))}\n").encode("utf-8")
        )
    return {
        "sha256": digest.hexdigest(),
        "historical_institutions": histories,
        "memberships": memberships,
    }


def reconcile_authorship(
    nodes: list[dict[str, Any]],
    profile_name_index: dict[tuple[str, ...], set[str]] | None = None,
    verified_authors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reconcile author assertions without treating reporters as authors.

    A CVLAC owner and a GrupLAC group are reporting sources. Only names that
    appear explicitly in the product are author assertions. Thesis owners are
    the exception because CVLAC describes their role as advisor.

    Books receive a conservative treatment: incompatible author declarations
    are retained as candidates and evidence but are not published as canonical
    authors. This prevents chapter contributors and name aliases from silently
    becoming authors of an entire volume.
    """
    profile_name_index = profile_name_index or {}
    owner_name_keys: dict[tuple[str, ...], set[str]] = {}
    owner_names: dict[str, str] = {}
    for node in nodes:
        profile_id = str(node.get("profile_id") or "")
        full_name = format_person_name(str(node.get("profile_author") or ""))
        key = name_token_key(full_name)
        if profile_id and key:
            owner_name_keys.setdefault(key, set()).add(profile_id)
            if len(full_name) > len(owner_names.get(profile_id, "")):
                owner_names[profile_id] = full_name

    def resolve_profile_id(key: tuple[str, ...]) -> str:
        local_ids = owner_name_keys.get(key, set())
        if len(local_ids) == 1:
            return next(iter(local_ids))
        global_ids = profile_name_index.get(key, set())
        if len(global_ids) == 1:
            return next(iter(global_ids))
        return ""

    people: dict[str, dict[str, Any]] = {}
    people_tokens: dict[str, frozenset[str]] = {}
    raw_claims: list[dict[str, Any]] = []

    def add_person(raw_name: Any, role: str = "") -> str:
        if not isinstance(raw_name, str):
            return ""
        display_name = format_person_name(raw_name)
        key = name_token_key(display_name)
        if not key or len(display_name) > 200 or "@" in display_name:
            return ""
        if role == "author" and len(key) < 2:
            return ""
        profile_id = resolve_profile_id(key)
        person_key = "id:{}".format(profile_id) if profile_id else "name:" + "|".join(key)
        person = people.setdefault(
            person_key,
            {"id": profile_id, "full_name": display_name},
        )
        canonical_name = owner_names.get(profile_id, "") if profile_id else ""
        preferred_name = max(
            (person.get("full_name", ""), display_name, canonical_name),
            key=lambda value: (len(value), value),
        )
        person["full_name"] = preferred_name
        if role == "advisor":
            person["type"] = "advisor"
        elif role == "author" and person.get("type") != "advisor":
            person["type"] = "author"
        people_tokens[person_key] = frozenset(key)
        return person_key

    def add_profile_owner(node: dict[str, Any], role: str) -> str:
        profile_id = str(node.get("profile_id") or "")
        display_name = format_person_name(str(node.get("profile_author") or ""))
        key = name_token_key(display_name)
        if not profile_id or not key:
            return ""
        person_key = "id:{}".format(profile_id)
        person = people.setdefault(
            person_key,
            {"id": profile_id, "full_name": display_name},
        )
        if role == "advisor":
            person["type"] = "advisor"
        people_tokens[person_key] = frozenset(key)
        return person_key

    for node in nodes:
        family = node.get("type_family")
        is_thesis = family in {
            "directed_work",
            "graduate_thesis",
            "undergraduate_thesis",
        }
        claim_keys: list[str] = []
        raw_names: list[str] = []
        if is_thesis and node.get("profile_id"):
            advisor_key = add_profile_owner(
                node, node.get("advisor_role") or "advisor"
            )
            if advisor_key:
                claim_keys.append(advisor_key)
        for raw_name in node.get("authors", []):
            person_key = add_person(raw_name, "author" if is_thesis else "")
            if person_key:
                claim_keys.append(person_key)
                raw_names.append(format_person_name(raw_name))
        for raw_name in node.get("oriented_people", []):
            if is_repeated_work_title(raw_name, node.get("title", "")):
                continue
            person_key = add_person(raw_name, "author")
            if person_key:
                claim_keys.append(person_key)
                raw_names.append(format_person_name(raw_name))
        raw_claims.append(
            {
                "source_kind": node.get("source_kind", "cvlac"),
                "source_id": node.get("source_id") or node.get("profile_id"),
                "reporter": node.get("profile_author") or node.get("group_code"),
                "declared_authors": uniq_strings(raw_names),
                "person_keys": list(dict.fromkeys(claim_keys)),
            }
        )

    # Collapse a short form only when it is contained in exactly one more
    # complete person name inside this work. Ambiguous forms remain candidates.
    aliases: dict[str, str] = {}
    for person_key, tokens in people_tokens.items():
        if not person_key.startswith("name:") or len(tokens) < 2:
            continue
        matches = [
            candidate_key
            for candidate_key, candidate_tokens in people_tokens.items()
            if candidate_key != person_key and tokens < candidate_tokens
        ]
        if len(matches) == 1:
            aliases[person_key] = matches[0]

    for claim in raw_claims:
        claim["person_keys"] = list(
            dict.fromkeys(aliases.get(key, key) for key in claim["person_keys"])
        )
    referenced = {
        key
        for claim in raw_claims
        for key in claim["person_keys"]
    }
    candidates = [people[key] for key in sorted(referenced) if key in people]
    declarations = {
        frozenset(claim["person_keys"])
        for claim in raw_claims
        if claim["person_keys"]
    }
    families = {node.get("type_family") for node in nodes}
    if verified_authors is not None:
        status = "verified"
        authors = []
        for value in verified_authors:
            if not isinstance(value, dict):
                continue
            profile_id = str(value.get("id") or "")
            full_name = format_person_name(str(value.get("full_name") or ""))
            if not full_name:
                continue
            author = {"id": profile_id, "full_name": full_name}
            if value.get("type"):
                author["type"] = value["type"]
            authors.append(author)
    elif not declarations:
        status = "missing"
        authors = []
    elif len(raw_claims) == 1:
        status = "single_source"
        authors = candidates
    elif len(declarations) == 1:
        status = "consistent"
        authors = candidates
    elif families == {"book"}:
        status = "conflict"
        authors = []
    else:
        status = "combined_claims"
        authors = candidates

    evidence = []
    for claim in raw_claims:
        evidence.append(
            {
                key: claim[key]
                for key in (
                    "source_kind",
                    "source_id",
                    "reporter",
                    "declared_authors",
                )
                if claim.get(key) not in (None, "", [])
            }
        )
    return {
        "authors": authors,
        "candidates": candidates,
        "status": status,
        "evidence": evidence,
    }


def materialize_authors(
    nodes: list[dict[str, Any]],
    profile_name_index: dict[tuple[str, ...], set[str]] | None = None,
) -> list[dict[str, Any]]:
    return reconcile_authorship(nodes, profile_name_index)["authors"]


def materialize_work(
    nodes: list[dict[str, Any]],
    timestamp: int,
    profile_name_index: dict[tuple[str, ...], set[str]] | None = None,
    authority: dict[str, Any] | None = None,
    group_affiliation_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _, titles = choose_titles(nodes)
    year = choose_year(nodes)
    dois = sorted({doi for node in nodes for doi in node.get("dois", []) if doi})
    isbns = uniq_strings(value for node in nodes for value in node.get("isbn", []))
    issns = uniq_strings(value for node in nodes for value in node.get("issn", []))
    identity_dois = sorted(
        {value for node in nodes for value in node.get("identity_dois", [])}
    )
    identity_isbns = sorted(
        {value for node in nodes for value in node.get("identity_isbns", [])}
    )
    title_fingerprints = sorted(
        {
            title_fingerprint(node["title_key"], node.get("year"))
            for node in nodes
        }
    )

    families = sorted({node.get("type_family", "unknown") for node in nodes})
    title_keys = sorted({node.get("title_key", "") for node in nodes})
    if len(identity_dois) == 1:
        identity = "|".join(
            [
                GRAPH_VERSION,
                "doi",
                identity_dois[0],
                title_keys[0] if len(title_keys) == 1 else ";".join(title_keys),
                str(year or ""),
                ";".join(families),
            ]
        )
    elif families == ["book"] and len(identity_isbns) == 1:
        identity = "|".join(
            [
                GRAPH_VERSION,
                "book",
                identity_isbns[0],
                title_keys[0] if len(title_keys) == 1 else ";".join(title_keys),
                str(year or ""),
            ]
        )
    elif len(nodes) > 1:
        identity = "|".join(
            [
                GRAPH_VERSION,
                "cluster",
                ";".join(families),
                ";".join(title_keys),
                str(year or ""),
                ";".join(identity_dois),
                ";".join(identity_isbns),
            ]
        )
    else:
        # Unsafe singleton occurrences intentionally remain source-specific.
        identity = f"{GRAPH_VERSION}|node|{nodes[0]['_id']}"
    entry = {
        "_id": sha256(identity.encode("utf-8")).hexdigest(),
        "updated": [{"source": "minciencias", "time": timestamp}],
        "titles": titles,
        "doi": dois[0] if dois else "",
        "keywords": uniq_strings(
            value for node in nodes for value in node.get("keywords", [])
        ),
        "year_published": year,
    }
    verified_authors = authority.get("authors") if authority else None
    authorship = reconcile_authorship(nodes, profile_name_index, verified_authors)
    entry["authors"] = authorship["authors"]
    for author in entry["authors"]:
        # Product-level affiliation is populated only by the strict GrupLAC
        # triangle below; neither CVLAC trajectory nor name-only membership is
        # accepted as evidence.
        author["affiliations"] = []
    entry["author_count"] = len(entry["authors"])
    entry["authorship_status"] = authorship["status"]
    if authorship["status"] == "conflict":
        entry["authorship_candidates"] = authorship["candidates"]
        entry["authorship_evidence"] = authorship["evidence"]
    if authority:
        entry["authorship_authority"] = {
            key: authority[key]
            for key in ("authority_id", "source", "evidence")
            if authority.get(key) not in (None, "", [])
        }

    group_affiliation_index = group_affiliation_index or {}
    groups: dict[str, dict[str, Any]] = {}
    for node in nodes:
        group_code = str(node.get("group_code") or "").strip().upper()
        if not group_code:
            continue
        group = groups.setdefault(
            group_code,
            {
                "id": group_code,
                "name": "",
                "affiliations": group_institution_for_year(
                    (group_affiliation_index.get(group_code) or {}).get(
                        "institutions", []
                    ),
                    year,
                ),
            },
        )
        group_name = str(node.get("group_name") or "").strip()
        if len(group_name) > len(group["name"]):
            group["name"] = group_name
    # Kahi requires a stable array even when no direct product-group evidence
    # exists.  An empty array is explicit uncertainty; it must not trigger an
    # affiliation inference from the researcher's career history.
    entry["groups"] = [groups[key] for key in sorted(groups)]

    for author in entry["authors"]:
        profile_id = str(author.get("id") or "")
        if not profile_id:
            continue
        affiliations = []
        for group_code in sorted(groups):
            institution = str(groups[group_code].get("affiliations") or "")
            context = group_affiliation_index.get(group_code) or {}
            periods = (context.get("members") or {}).get(profile_id, [])
            if not institution:
                continue
            valid_periods = sorted(
                {
                    str(period)
                    for period in periods
                    if membership_covers_publication_year(str(period), year)
                }
            )
            if not valid_periods:
                continue
            affiliations.append(
                {
                    "institution": institution,
                    "source": "gruplac",
                    "group_code": group_code,
                    "membership_period": valid_periods[0],
                    "product_in_group": True,
                }
            )
        author["affiliations"] = affiliations

    impactu_types = uniq_strings(node.get("type_impactu", "") for node in nodes)
    product_types_by_source: dict[str, list[str]] = {}
    for node in nodes:
        source_kind = str(node.get("source_kind") or "cvlac")
        product_types_by_source.setdefault(source_kind, []).append(
            node.get("product_type", "")
        )
    entry["types"] = [
        {
            "provenance": "minciencias",
            "source": "impactu",
            "type": value,
            "level": 0,
        }
        for value in impactu_types
    ] + [
        {
            "provenance": "minciencias",
            "source": source_kind,
            "type": value,
            "level": 1,
        }
        for source_kind in sorted(product_types_by_source)
        for value in uniq_strings(product_types_by_source[source_kind])
    ]

    entry["external_ids"] = [
        {"provenance": "minciencias", "source": "doi", "id": value}
        for value in dois
    ] + [
        {"provenance": "minciencias", "source": "isbn", "id": value}
        for value in isbns
    ] + [
        {"provenance": "minciencias", "source": "issn", "id": value}
        for value in issns
    ] + [
        {
            "provenance": "minciencias",
            "source": "minciencias_title_fingerprint",
            "id": value,
        }
        for value in title_fingerprints
    ]

    # Bibliographic publication context is enrichment-only. It is deliberately
    # materialized after identity selection and therefore cannot merge works.
    materialize_bibliographic_context(entry, nodes, timestamp, isbns)

    areas = uniq_strings(value for node in nodes for value in node.get("areas", []))
    entry["subjects"] = []
    if areas:
        entry["subjects"] = [
            {
                "source": "minciencias",
                "subjects": [
                    {"id": "", "name": area, "level": None}
                    for area in areas
                ],
            }
        ]

    return mongo_safe(entry)


class CvlacWorkGraphBuilder:
    def __init__(
        self,
        db,
        collection: str = "cvlac_works",
        source_collection: str = "cvlac_stage_raw",
        group_source_collection: str | None = None,
        researcher_collections: tuple[str, ...] = (
            "all_researchers",
            "recognized_researchers",
        ),
        recognized_groups_collection: str = "recognized_groups",
        authority_collection: str = "minciencias_bibliographic_authorities",
        batch_size: int = 500,
        max_title_group_size: int = 250,
        source_mode: str = "raw",
        candidate_partitions: int = 1,
    ):
        if source_mode not in {"raw", "normalized"}:
            raise ValueError("source_mode must be 'raw' or 'normalized'")
        self.db = db
        self.collection_name = collection
        self.source_collection_name = source_collection
        self.group_source_collection_name = group_source_collection
        self.researcher_collections = researcher_collections
        self.recognized_groups_collection_name = recognized_groups_collection
        self.authority_collection_name = authority_collection
        self.batch_size = max(1, int(batch_size))
        self.max_title_group_size = max(2, int(max_title_group_size))
        self.source_mode = source_mode
        self.candidate_partitions = max(1, int(candidate_partitions))
        if self.candidate_partitions > 1024:
            raise ValueError("candidate_partitions cannot exceed 1024")
        self.use_compact_graph = False
        self.next_node_seq = 0
        self.graph = UnionFind()
        self.metrics = Counter()
        self.review_buffer: list[dict[str, Any]] = []
        self.profile_name_index: dict[tuple[str, ...], set[str]] = {}
        self.profile_name_by_id: dict[str, str] = {}
        self.authority_index: dict[str, list[dict[str, Any]]] = {}
        self.group_affiliation_index: dict[str, dict[str, Any]] = {}
        self.run_id = ""
        self.review_collection = None
        self.edge_collection = None
        self.edge_buffer: list[dict[str, Any]] = []
        self.current_stage = ""
        self.current_partition: int | None = None

    def _accept_work_route(
        self, source_kind: str, route: dict[str, str] | None
    ) -> bool:
        """Keep only exact ``works`` routes in the bibliographic graph."""
        if route is None:
            self.metrics[f"{source_kind}_routing_unmapped"] += 1
            return False
        entity = route.get("entity") or "unmapped"
        self.metrics[f"{source_kind}_routed_{entity}"] += 1
        if entity != "works":
            self.metrics[f"excluded_{entity}"] += 1
            return False
        return True

    def _prepare_graph_node(self, node: dict[str, Any]) -> None:
        partition_key = f"{node.get('title_key', '')}|{node.get('year') or ''}"
        node["title_partition"] = stable_partition(
            partition_key, self.candidate_partitions
        )
        if self.use_compact_graph:
            node["node_seq"] = self.next_node_seq
            node["cluster_seq"] = self.next_node_seq
            self.next_node_seq += 1

    def _graph_key(self, node: dict[str, Any]) -> str | int:
        if isinstance(self.graph, CompactUnionFind):
            return int(node["node_seq"])
        return node["_id"]

    def build(
        self,
        profile_ids: list[str] | None = None,
        replace: bool = True,
    ) -> dict[str, Any]:
        if self.collection_name == self.source_collection_name:
            raise ValueError("Source and destination collections must be different")
        existing_collections = set(self.db.list_collection_names())
        if self.source_collection_name not in existing_collections:
            raise ValueError(
                f"Collection {self.source_collection_name!r} was not found"
            )
        if not replace and self.collection_name in self.db.list_collection_names():
            raise ValueError(f"Collection {self.collection_name!r} already exists")

        self.run_id = uuid.uuid4().hex
        self.profile_name_index = {}
        self.profile_name_by_id = {}
        self.authority_index = {}
        timestamp = int(time())
        safe_target = re.sub(r"[^a-zA-Z0-9_]", "_", self.collection_name)
        nodes_name = f"__yuku_{safe_target}_nodes_{self.run_id}"
        output_name = f"__yuku_{safe_target}_build_{self.run_id}"
        runs = self.db[f"{self.collection_name}_graph_runs"]
        self.review_collection = self.db[f"{self.collection_name}_graph_review"]
        self.review_collection.create_index([("run_id", ASCENDING), ("reason", ASCENDING)])
        nodes = self.db[nodes_name]
        output = self.db[output_name]
        runs.insert_one(
            {
                "_id": self.run_id,
                "status": "running",
                "started_at": timestamp,
                "source_collection": self.source_collection_name,
                "group_source_collection": self.group_source_collection_name,
                "target_collection": self.collection_name,
                "graph_version": GRAPH_VERSION,
                "publisher_entity_rule_version": PUBLISHER_ENTITY_RULE_VERSION,
            }
        )

        try:
            self._load_researcher_name_index()
            self._load_authority_index()
            self._load_group_affiliation_index()
            self._extract_nodes(nodes, profile_ids)
            if self.group_source_collection_name:
                if self.group_source_collection_name not in existing_collections:
                    raise ValueError(
                        f"Collection {self.group_source_collection_name!r} was not found"
                    )
                self._extract_gruplac_nodes(nodes)
            nodes.create_index([("cluster_id", ASCENDING)])
            nodes.create_index([("title_key", ASCENDING), ("year", ASCENDING)])
            nodes.create_index([("dois", ASCENDING)])
            nodes.create_index([("identity_isbns", ASCENDING)])
            nodes.create_index([("source_kind", ASCENDING), ("source_id", ASCENDING)])

            self._connect_doi_groups(nodes)
            self._flush_edges()
            self._connect_title_groups(nodes)
            self._flush_edges()
            self._flush_reviews()
            self._write_cluster_ids(nodes)
            self._materialize(nodes, output, timestamp)
            self._flush_reviews()
            self._create_output_indexes(output)
            output.rename(self.collection_name, dropTarget=replace)

            summary = {
                "run_id": self.run_id,
                "profiles": self.metrics["profiles"],
                "groups": self.metrics["groups"],
                "nodes": self.metrics["nodes"],
                "works": self.metrics["works"],
                "automatic_edges": self.metrics["automatic_edges"],
                "automatic_unions": self.metrics["automatic_unions"],
                "review_edges": self.metrics["review_edges"],
                "components": self.metrics["components"],
                "parse_errors": self.metrics["parse_errors"],
                "oversized_title_groups": self.metrics["oversized_title_groups"],
                "doi_candidate_groups": self.metrics["doi_candidate_groups"],
                "rejected_doi_groups": self.metrics["rejected_doi_groups"],
                "rejected_doi_occurrences": self.metrics["rejected_doi_occurrences"],
                "authorities_indexed": self.metrics["authorities_indexed"],
                "authorities_applied": self.metrics["authorities_applied"],
                "authority_conflicts": self.metrics["authority_conflicts"],
                "materialized_identity_collisions": self.metrics[
                    "materialized_identity_collisions"
                ],
                "routing": {
                    key: value
                    for key, value in sorted(self.metrics.items())
                    if key.startswith(("cvlac_routed_", "gruplac_routed_", "excluded_"))
                    or key.endswith("_routing_unmapped")
                },
            }
            runs.update_one(
                {"_id": self.run_id},
                {"$set": {**summary, "status": "complete", "finished_at": int(time())}},
            )
            return summary
        except Exception as error:
            runs.update_one(
                {"_id": self.run_id},
                {
                    "$set": {
                        "status": "failed",
                        "finished_at": int(time()),
                        "error": str(error),
                        **dict(self.metrics),
                    }
                },
            )
            raise
        finally:
            existing = set(self.db.list_collection_names())
            if nodes_name in existing:
                self.db[nodes_name].drop()
            if output_name in existing:
                self.db[output_name].drop()

    def _load_researcher_name_index(self) -> None:
        existing = set(self.db.list_collection_names())
        for collection_name in self.researcher_collections:
            if collection_name not in existing:
                continue
            cursor = self.db[collection_name].find(
                {"cod_rh": {"$type": "string"}},
                {"cod_rh": 1, "nombre_completo": 1},
            )
            for record in cursor:
                profile_id = str(record.get("cod_rh") or "").zfill(10)
                key = name_token_key(str(record.get("nombre_completo") or ""))
                if profile_id.isdigit() and len(profile_id) == 10 and key:
                    self.profile_name_index.setdefault(key, set()).add(profile_id)
                    name = str(record.get("nombre_completo") or "").strip()
                    if len(name) > len(self.profile_name_by_id.get(profile_id, "")):
                        self.profile_name_by_id[profile_id] = name
                    self.metrics["researcher_names_indexed"] += 1

    def _load_authority_index(self) -> None:
        if self.authority_collection_name not in self.db.list_collection_names():
            return
        for document in self.db[self.authority_collection_name].find(
            {"status": "verified"}
        ):
            match = document.get("match") or {}
            keys = bibliographic_authority_keys(
                title=str(match.get("title") or ""),
                year=match.get("year"),
                family=str(match.get("type_family") or ""),
                dois=[match.get("doi")] if match.get("doi") else [],
                isbns=[match.get("isbn")] if match.get("isbn") else [],
            )
            authority = {
                "authority_id": str(document.get("_id")),
                "authors": document.get("authors") or [],
                "source": document.get("source"),
                "evidence": document.get("evidence"),
            }
            for key in keys:
                self.authority_index.setdefault(key, []).append(authority)
            self.metrics["authorities_indexed"] += 1

    def _load_group_affiliation_index(self) -> None:
        """Index only GrupLAC evidence needed during work materialization."""
        self.group_affiliation_index = {}
        existing = set(self.db.list_collection_names())
        if (
            not self.group_source_collection_name
            or self.group_source_collection_name not in existing
            or self.recognized_groups_collection_name not in existing
        ):
            return

        for record in self.db[self.recognized_groups_collection_name].find(
            {"codigo_grupo": {"$type": "string"}},
            {"codigo_grupo": 1, "instituciones_historicas": 1},
        ):
            group_code = str(record.get("codigo_grupo") or "").strip().upper()
            if not group_code:
                continue
            history = record.get("instituciones_historicas") or []
            if not isinstance(history, list):
                history = []
            self.group_affiliation_index[group_code] = {
                "institutions": history,
                "members": {},
            }

        cursor = self.db[self.group_source_collection_name].find(
            {}, {"group_code": 1, "members": 1}
        )
        for group in cursor:
            group_code = str(
                group.get("group_code") or group.get("_id") or ""
            ).strip().upper()
            context = self.group_affiliation_index.get(group_code)
            if context is None:
                continue
            members: dict[str, list[str]] = context["members"]
            for member in group.get("members") or []:
                if not isinstance(member, dict):
                    continue
                profile_id = str(member.get("cod_rh") or "").strip()
                if profile_id.isdigit():
                    profile_id = profile_id.zfill(10)
                period = str(member.get("period") or "").strip()
                # Identity and period must both be explicit. Names are never
                # used as a fallback for institutional attribution.
                if (
                    not profile_id.isdigit()
                    or len(profile_id) != 10
                    or parse_group_membership_period(period) is None
                ):
                    continue
                members.setdefault(profile_id, []).append(period)

        self.metrics["affiliation_groups_indexed"] = len(
            self.group_affiliation_index
        )
        self.metrics["affiliation_memberships_indexed"] = sum(
            len(periods)
            for context in self.group_affiliation_index.values()
            for periods in (context.get("members") or {}).values()
        )

    def _authority_for_component(self, nodes: list[dict[str, Any]]) -> dict | None:
        title, _ = choose_titles(nodes)
        year = choose_year(nodes)
        families = sorted({node.get("type_family", "") for node in nodes})
        if len(families) != 1:
            return None
        keys = bibliographic_authority_keys(
            title=title,
            year=year,
            family=families[0],
            dois=[value for node in nodes for value in node.get("identity_dois", [])],
            isbns=[value for node in nodes for value in node.get("identity_isbns", [])],
        )
        candidates: dict[str, dict] = {}
        for key in keys:
            for authority in self.authority_index.get(key, []):
                candidates[authority["authority_id"]] = authority
        if len(candidates) == 1:
            self.metrics["authorities_applied"] += 1
            return next(iter(candidates.values()))
        if len(candidates) > 1:
            self.metrics["authority_conflicts"] += 1
        return None

    def _extract_gruplac_nodes(self, collection) -> None:
        buffer = []
        cursor = self.db[self.group_source_collection_name].find(
            {},
            {
                "group_code": 1,
                "group_name": 1,
                "production": 1,
            },
        ).batch_size(10)
        for group in cursor:
            group_code = str(group.get("group_code") or group.get("_id") or "").upper()
            group_name = str(group.get("group_name") or "")
            self.metrics["groups"] += 1
            for production_index, record in enumerate(group.get("production") or []):
                route = route_gruplac(record)
                if not self._accept_work_route("gruplac", route):
                    continue
                title = str(record.get("title") or "").strip()
                title_key = normalize_title(title)
                if not title_key:
                    self.metrics["empty_titles"] += 1
                    continue
                node_identity = "|".join(
                    [
                        GRAPH_VERSION,
                        "gruplac",
                        group_code,
                        str(production_index),
                        title_key,
                        str(record.get("year") or ""),
                        norm_text(record.get("type_impactu", "")),
                    ]
                )
                node_id = sha256(node_identity.encode("utf-8")).hexdigest()
                record_dois = sorted(
                    {
                        normalize_doi(str(value))
                        for value in record.get("doi", [])
                        if value
                    }
                )
                identity_dois = [doi for doi in record_dois if is_identity_doi(doi)]
                record_isbns = record.get("isbn", [])
                node = {
                    "_id": node_id,
                    "cluster_id": node_id,
                    "profile_id": "",
                    "profile_author": "",
                    "source_kind": "gruplac",
                    "source_id": group_code,
                    "source_record_index": production_index,
                    "group_code": group_code,
                    "group_name": group_name,
                    "title": title,
                    "title_key": title_key,
                    "year": record.get("year"),
                    "type_impactu": record.get("type_impactu", ""),
                    "type_family": type_family(record.get("type_impactu", "")),
                    "product_type": record.get("product_type", ""),
                    "authors": record.get("authors", []),
                    "keywords": record.get("keywords", []),
                    "areas": record.get("areas", []),
                    "advisor_role": "",
                    "oriented_people": [],
                    "dois": record_dois,
                    "identity_dois": identity_dois,
                    "issn": record.get("issn", []),
                    "isbn": record_isbns,
                    "identity_isbns": sorted(
                        {
                            normalized
                            for value in record_isbns
                            if (normalized := normalize_isbn_identity(value))
                        }
                    ),
                    "affiliation": "",
                    "country": record.get("country", ""),
                    "publisher": record.get("publisher", ""),
                    "book_title": record.get("book_title", ""),
                    "edition": record.get("edition", ""),
                    "pages": record.get("pages", ""),
                    "start_page": record.get("start_page", ""),
                    "end_page": record.get("end_page", ""),
                    "volume": record.get("volume", ""),
                    "publication_place": record.get("publication_place", ""),
                    "language": record.get("language", ""),
                    "dissemination_medium": record.get(
                        "dissemination_medium", ""
                    ),
                    "validated": bool(record.get("validated")),
                    "routing_rule": route["rule"],
                    "type_catalog_version": route.get("catalog_version", ""),
                }
                self._prepare_graph_node(node)
                buffer.append(mongo_safe(node))
                self.metrics["nodes"] += 1
                self.metrics["gruplac_nodes"] += 1
                if len(buffer) >= self.batch_size:
                    collection.insert_many(buffer, ordered=False)
                    buffer = []
        if buffer:
            collection.insert_many(buffer, ordered=False)

    def _extract_nodes(self, collection, profile_ids: list[str] | None) -> None:
        if self.source_mode == "normalized":
            self._extract_normalized_cvlac_nodes(collection, profile_ids)
            return
        query = {}
        if profile_ids is not None:
            query = {"_id": {"$in": [str(profile_id) for profile_id in profile_ids]}}
        cursor = self.db[self.source_collection_name].find(query, {"html": 1}).batch_size(50)
        buffer = []
        for raw in cursor:
            profile_id = str(raw["_id"])
            self.metrics["profiles"] += 1
            try:
                profile_author, records = extract_profile_works_from_html(
                    profile_id,
                    raw.get("html", ""),
                )
            except Exception:
                self.metrics["parse_errors"] += 1
                continue
            profile_name_key = name_token_key(profile_author)
            if profile_name_key:
                self.profile_name_index.setdefault(profile_name_key, set()).add(
                    profile_id
                )
            production_index = 0
            for record in records:
                route = route_cvlac("production", record)
                if not self._accept_work_route("cvlac", route):
                    continue
                title = str(record.get("title") or "").strip()
                title_key = normalize_title(title)
                if not title_key:
                    self.metrics["empty_titles"] += 1
                    continue
                node_identity = "|".join(
                    [
                        GRAPH_VERSION,
                        profile_id,
                        str(production_index),
                        title_key,
                        str(record.get("year") or ""),
                        norm_text(record.get("type_impactu", "")),
                    ]
                )
                node_id = sha256(node_identity.encode("utf-8")).hexdigest()
                record_dois = [
                    normalize_doi(str(value))
                    for value in record.get("doi", [])
                    if value
                ]
                record_dois = sorted(set(record_dois))
                identity_dois = [doi for doi in record_dois if is_identity_doi(doi)]
                self.metrics["rejected_doi_occurrences"] += (
                    len(record_dois) - len(identity_dois)
                )
                authors = record.get("authors", [])
                if not isinstance(authors, list):
                    authors = [authors] if authors else []
                node = {
                    "_id": node_id,
                    "cluster_id": node_id,
                    "profile_id": profile_id,
                    "profile_author": profile_author,
                    "source_kind": "cvlac",
                    "source_id": profile_id,
                    "source_record_index": production_index,
                    "title": title,
                    "title_key": title_key,
                    "year": record.get("year"),
                    "type_impactu": record.get("type_impactu", ""),
                    "type_family": type_family(record.get("type_impactu", "")),
                    "product_type": record.get("product_type", ""),
                    "authors": authors,
                    "keywords": record.get("keywords", []),
                    "areas": record.get("areas", []),
                    "advisor_role": record.get("advisor_role", ""),
                    "oriented_people": record.get("oriented_people", []),
                    "dois": record_dois,
                    "identity_dois": identity_dois,
                    "issn": record.get("issn", []),
                    "isbn": record.get("isbn", []),
                    "identity_isbns": sorted(
                        {
                            normalized
                            for value in record.get("isbn", [])
                            if (normalized := normalize_isbn_identity(value))
                        }
                    ),
                    "affiliation": record.get("affiliation", ""),
                    "country": record.get("country", ""),
                    "publisher": record.get("publisher", ""),
                    "book_title": record.get("book_title", ""),
                    "edition": record.get("edition", ""),
                    "pages": record.get("pages", ""),
                    "start_page": record.get("start_page", ""),
                    "end_page": record.get("end_page", ""),
                    "volume": record.get("volume", ""),
                    "publication_place": record.get("publication_place", ""),
                    "language": record.get("language", ""),
                    "dissemination_medium": record.get(
                        "dissemination_medium", ""
                    ),
                    "routing_rule": route["rule"],
                    "type_catalog_version": route.get("catalog_version", ""),
                }
                self._prepare_graph_node(node)
                buffer.append(mongo_safe(node))
                production_index += 1
                self.metrics["nodes"] += 1
                if len(buffer) >= self.batch_size:
                    collection.insert_many(buffer, ordered=False)
                    buffer = []
            if self.metrics["profiles"] % 100 == 0:
                print(
                    f"INFO: extracted {self.metrics['nodes']} CVLAC work occurrences "
                    f"from {self.metrics['profiles']} profiles."
                )
        if buffer:
            collection.insert_many(buffer, ordered=False)

    def _extract_normalized_cvlac_nodes(
        self, collection, profile_ids: list[str] | None
    ) -> None:
        """Create graph occurrences from the already audited CVLAC documents."""
        query = {}
        if profile_ids is not None:
            query = {"_id": {"$in": [str(value) for value in profile_ids]}}
        cursor = self.db[self.source_collection_name].find(
            query,
            {"production": 1, "profile_status": 1},
        ).sort("_id", ASCENDING).batch_size(50)
        buffer = []
        for document in cursor:
            profile_id = str(document["_id"]).zfill(10)
            profile_author = self.profile_name_by_id.get(profile_id, "")
            profile_name_key = name_token_key(profile_author)
            if profile_name_key:
                self.profile_name_index.setdefault(profile_name_key, set()).add(
                    profile_id
                )
            self.metrics["profiles"] += 1
            records = document.get("production") or []
            if not isinstance(records, list):
                self.metrics["invalid_normalized_profiles"] += 1
                continue
            for production_index, record in enumerate(records):
                if not isinstance(record, dict):
                    self.metrics["invalid_normalized_records"] += 1
                    continue
                route = route_cvlac("production", record)
                if not self._accept_work_route("cvlac", route):
                    continue
                title = str(record.get("title") or "").strip()
                title_key = normalize_title(title)
                if not title_key:
                    self.metrics["empty_titles"] += 1
                    continue
                node_identity = "|".join(
                    [
                        GRAPH_VERSION,
                        "cvlac",
                        profile_id,
                        str(production_index),
                        title_key,
                        str(record.get("year") or ""),
                        norm_text(record.get("type_impactu", "")),
                    ]
                )
                node_id = sha256(node_identity.encode("utf-8")).hexdigest()
                record_dois = sorted(
                    {
                        normalize_doi(str(value))
                        for value in record.get("doi", [])
                        if value
                    }
                )
                identity_dois = [
                    doi for doi in record_dois if is_identity_doi(doi)
                ]
                self.metrics["rejected_doi_occurrences"] += (
                    len(record_dois) - len(identity_dois)
                )
                authors = record.get("authors", [])
                if not isinstance(authors, list):
                    authors = [authors] if authors else []
                isbns = record.get("isbn", [])
                if not isinstance(isbns, list):
                    isbns = [isbns] if isbns else []
                node = {
                    "_id": node_id,
                    "cluster_id": node_id,
                    "profile_id": profile_id,
                    "profile_author": profile_author,
                    "source_kind": "cvlac",
                    "source_id": profile_id,
                    "source_record_index": production_index,
                    "title": title,
                    "title_key": title_key,
                    "year": record.get("year"),
                    "type_impactu": record.get("type_impactu", ""),
                    "type_family": type_family(record.get("type_impactu", "")),
                    "product_type": record.get("product_type", ""),
                    "authors": authors,
                    "keywords": record.get("keywords", []),
                    "areas": record.get("areas", []),
                    "advisor_role": record.get("advisor_role", ""),
                    "oriented_people": record.get("oriented_people", []),
                    "dois": record_dois,
                    "identity_dois": identity_dois,
                    "issn": record.get("issn", []),
                    "isbn": isbns,
                    "identity_isbns": sorted(
                        {
                            normalized
                            for value in isbns
                            if (normalized := normalize_isbn_identity(value))
                        }
                    ),
                    "affiliation": record.get("affiliation", ""),
                    "country": record.get("country", ""),
                    "publisher": record.get("publisher", ""),
                    "book_title": record.get("book_title", ""),
                    "edition": record.get("edition", ""),
                    "pages": record.get("pages", ""),
                    "start_page": record.get("start_page", ""),
                    "end_page": record.get("end_page", ""),
                    "volume": record.get("volume", ""),
                    "publication_place": record.get("publication_place", ""),
                    "language": record.get("language", ""),
                    "dissemination_medium": record.get(
                        "dissemination_medium", ""
                    ),
                    "routing_rule": route["rule"],
                    "type_catalog_version": route.get("catalog_version", ""),
                }
                self._prepare_graph_node(node)
                buffer.append(mongo_safe(node))
                self.metrics["nodes"] += 1
                self.metrics["cvlac_nodes"] += 1
                if len(buffer) >= self.batch_size:
                    collection.insert_many(buffer, ordered=False)
                    buffer = []
            if self.metrics["profiles"] % 1000 == 0:
                print(
                    f"INFO: extracted {self.metrics['nodes']} normalized CVLAC "
                    f"occurrences from {self.metrics['profiles']} profiles.",
                    flush=True,
                )
        if buffer:
            collection.insert_many(buffer, ordered=False)

    @staticmethod
    def _batches(cursor, size: int):
        batch = []
        for item in cursor:
            batch.append(item)
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _load_group_nodes(self, collection, records: list[dict]) -> dict[str, dict]:
        member_ids = {
            member_id
            for record in records
            for member_id in record.get("member_ids", [])
        }
        return {
            node["_id"]: node
            for node in collection.find({"_id": {"$in": list(member_ids)}})
        }

    def _connect_doi_groups(self, collection) -> None:
        pipeline = [
            {"$match": {"identity_dois.0": {"$exists": True}}},
            {"$unwind": "$identity_dois"},
            {
                "$group": {
                    "_id": "$identity_dois",
                    "member_ids": {"$addToSet": "$_id"},
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$member_ids"}, 1]}}},
            {"$sort": {"_id": 1}},
        ]
        cursor = collection.aggregate(pipeline, allowDiskUse=True, batchSize=100)
        for records in self._batches(cursor, 100):
            accepted = []
            for record in records:
                if not is_identity_doi(record["_id"]):
                    self.metrics["rejected_doi_groups"] += 1
                    self._review_doi_group(record, "invalid_doi")
                else:
                    accepted.append(record)
            if not accepted:
                continue

            snapshot = self._load_group_nodes(collection, accepted)
            for record in accepted:
                members = sorted(record["member_ids"])
                if len(members) < 2:
                    continue
                self.metrics["doi_candidate_groups"] += 1
                anchors: list[dict[str, Any]] = []
                for member_id in members:
                    node = snapshot[member_id]
                    self.graph.add(node)
                    compatible_anchor = None
                    first_conflict = None
                    for anchor in anchors:
                        allowed, reason = self.graph.can_union_by_doi(
                            self._graph_key(anchor), self._graph_key(node)
                        )
                        if allowed:
                            compatible_anchor = anchor
                            break
                        if first_conflict is None:
                            first_conflict = (anchor, reason)

                    if compatible_anchor is None:
                        if first_conflict is not None:
                            self._review_edge(node, *first_conflict)
                        anchors.append(node)
                        continue

                    self.metrics["automatic_edges"] += 1
                    _, merged = self._union_nodes(
                        compatible_anchor,
                        node,
                        evidence={"doi": record["_id"]},
                    )
                    self.metrics["automatic_unions"] += int(merged)

    def _connect_title_groups(
        self, collection, partition: int | None = None
    ) -> None:
        match = {"year": {"$ne": None}, "title_key": {"$ne": ""}}
        if partition is not None:
            match["title_partition"] = int(partition)
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": {"title": "$title_key", "year": "$year"},
                    "member_ids": {"$addToSet": "$_id"},
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$member_ids"}, 1]}}},
            {"$sort": {"_id.year": 1, "_id.title": 1}},
        ]
        cursor = collection.aggregate(pipeline, allowDiskUse=True, batchSize=100)
        for records in self._batches(cursor, 100):
            accepted = []
            for record in records:
                if len(record["member_ids"]) > self.max_title_group_size:
                    self.metrics["oversized_title_groups"] += 1
                    self._review_oversized_group(record)
                else:
                    accepted.append(record)
            if not accepted:
                continue
            snapshot = self._load_group_nodes(collection, accepted)
            for record in accepted:
                self.metrics["title_candidate_groups"] += 1
                members = [snapshot[member_id] for member_id in sorted(record["member_ids"])]
                anchors = self._connect_title_group_members(members)
                family_anchors = [anchors[key][0] for key in sorted(anchors)]
                if len(family_anchors) > 1:
                    first = family_anchors[0]
                    for other in family_anchors[1:]:
                        self._review_edge(first, other, "type_conflict")

    def _connect_title_group_members(
        self, members: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Connect an exact title/year group using multiple evidence anchors.

        A single anchor is unsafe for books because the same title can have
        several ISBN editions. Keeping every incompatible component as an
        anchor lets later occurrences find the matching ISBN without allowing
        one edition to bridge into another.
        """
        anchors: dict[str, list[dict[str, Any]]] = {}
        for node in members:
            family = node["type_family"]
            family_anchors = anchors.setdefault(family, [])
            self.graph.add(node)
            if not family_anchors:
                family_anchors.append(node)
                continue

            compatible: list[dict[str, Any]] = []
            first_conflict: tuple[dict[str, Any], str] | None = None
            for anchor in family_anchors:
                anchor_isbns = set(anchor.get("identity_isbns") or [])
                node_isbns = set(node.get("identity_isbns") or [])
                shared_isbn = bool(anchor_isbns.intersection(node_isbns))
                has_author_evidence = author_evidence(anchor, node)
                generic = is_generic_title(node["title_key"])
                if generic and not (
                    has_author_evidence or (family == "book" and shared_isbn)
                ):
                    if first_conflict is None:
                        first_conflict = (
                            anchor,
                            "generic_title_without_author_evidence",
                        )
                    continue
                if family == "book" and not shared_isbn and not has_author_evidence:
                    if first_conflict is None:
                        first_conflict = (
                            anchor,
                            "book_title_without_isbn_or_author_evidence",
                        )
                    continue
                allowed, reason = self.graph.can_union_by_title(
                    self._graph_key(anchor), self._graph_key(node)
                )
                if allowed:
                    compatible.append(anchor)
                elif first_conflict is None:
                    first_conflict = (anchor, reason)

            if not compatible:
                if first_conflict is not None:
                    self._review_edge(first_conflict[0], node, first_conflict[1])
                family_anchors.append(node)
                continue

            for anchor in compatible:
                allowed, _ = self.graph.can_union_by_title(
                    self._graph_key(anchor), self._graph_key(node)
                )
                if not allowed:
                    continue
                self.metrics["automatic_edges"] += 1
                _, merged = self._union_nodes(
                    anchor,
                    node,
                    evidence={
                        "title_key": node.get("title_key"),
                        "year": node.get("year"),
                    },
                )
                self.metrics["automatic_unions"] += int(merged)
        return anchors

    def _union_nodes(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        evidence: dict[str, Any],
    ) -> tuple[str, bool]:
        root, merged = self.graph.union(
            self._graph_key(left), self._graph_key(right)
        )
        if merged and self.edge_collection is not None:
            pair = sorted((left["_id"], right["_id"]))
            partition = self.current_partition
            edge_id = sha256(
                (
                    f"{self.run_id}|{self.current_stage}|{partition}|"
                    f"{pair[0]}|{pair[1]}"
                ).encode("utf-8")
            ).hexdigest()
            edge = {
                "_id": edge_id,
                "run_id": self.run_id,
                "stage": self.current_stage,
                "left_id": pair[0],
                "right_id": pair[1],
                "evidence": evidence,
            }
            if partition is not None:
                edge["partition"] = partition
            self.edge_buffer.append(edge)
            if len(self.edge_buffer) >= self.batch_size:
                self._flush_edges()
        return root, merged

    def _review_edge(self, left: dict, right: dict, reason: str) -> None:
        pair = sorted((left["_id"], right["_id"]))
        partition = self.current_partition
        review_id = sha256(
            (
                f"{self.run_id}|{self.current_stage}|{partition}|{reason}|"
                f"{pair[0]}|{pair[1]}"
            ).encode("utf-8")
        ).hexdigest()
        review = {
            "_id": review_id,
            "run_id": self.run_id,
            "stage": self.current_stage,
            "status": "review",
            "reason": reason,
            "left": compact_node(left),
            "right": compact_node(right),
        }
        if partition is not None:
            review["partition"] = partition
        self.review_buffer.append(review)
        self.metrics["review_edges"] += 1
        if len(self.review_buffer) >= self.batch_size:
            self._flush_reviews()

    def _review_oversized_group(self, record: dict) -> None:
        partition = self.current_partition
        review_id = sha256(
            (
                f"{self.run_id}|{self.current_stage}|{partition}|oversized|"
                f"{repr(record['_id'])}"
            ).encode("utf-8")
        ).hexdigest()
        review = {
            "_id": review_id,
            "run_id": self.run_id,
            "stage": self.current_stage,
            "status": "review",
            "reason": "oversized_title_group",
            "candidate_key": record["_id"],
            "members_count": len(record["member_ids"]),
        }
        if partition is not None:
            review["partition"] = partition
        self.review_buffer.append(review)

    def _review_doi_group(self, record: dict, reason: str) -> None:
        partition = self.current_partition
        review_id = sha256(
            (
                f"{self.run_id}|{self.current_stage}|{partition}|{reason}|"
                f"{record['_id']}"
            ).encode("utf-8")
        ).hexdigest()
        review = {
            "_id": review_id,
            "run_id": self.run_id,
            "stage": self.current_stage,
            "status": "review",
            "reason": reason,
            "doi": record["_id"],
            "members_count": len(record["member_ids"]),
        }
        if partition is not None:
            review["partition"] = partition
        self.review_buffer.append(review)

    def _flush_reviews(self) -> None:
        if self.review_buffer and self.review_collection is not None:
            documents = list(
                {
                    document["_id"]: document
                    for document in self.review_buffer
                }.values()
            )
            self.review_buffer = []
            operations = [
                ReplaceOne({"_id": document["_id"]}, document, upsert=True)
                for document in documents
            ]
            try:
                self.review_collection.bulk_write(operations, ordered=False)
            except TypeError:
                # Compatibility with mongomock versions whose bulk operation
                # signature lags PyMongo. Production continues to use one bulk.
                for document in documents:
                    self.review_collection.replace_one(
                        {"_id": document["_id"]}, document, upsert=True
                    )
            except Exception:
                # Upserts are idempotent, so preserving the deduplicated buffer
                # makes a caller retry safe even after a partially applied bulk.
                self.review_buffer.extend(documents)
                raise

    def _flush_edges(self) -> None:
        if self.edge_buffer and self.edge_collection is not None:
            self.edge_collection.insert_many(self.edge_buffer, ordered=False)
            self.edge_buffer = []

    def _write_cluster_ids(self, collection) -> None:
        updates: list[tuple[str, str]] = []
        roots = Counter()
        for node_id in self.graph.parent:
            root = self.graph.find(node_id)
            roots[root] += 1
            if root != node_id:
                updates.append((node_id, root))
            if len(updates) >= self.batch_size:
                self._apply_cluster_updates(collection, updates)
                updates = []
        if updates:
            self._apply_cluster_updates(collection, updates)
        self.metrics["components"] = sum(size > 1 for size in roots.values())

    @staticmethod
    def _apply_cluster_updates(collection, updates: list[tuple[str, str]]) -> None:
        operations = [
            UpdateOne({"_id": node_id}, {"$set": {"cluster_id": cluster_id}})
            for node_id, cluster_id in updates
        ]
        try:
            collection.bulk_write(operations, ordered=False)
        except TypeError:
            # Compatibility fallback for mongomock releases lagging PyMongo's
            # UpdateOne signature. Real MongoDB continues to use bulk writes.
            for node_id, cluster_id in updates:
                collection.update_one(
                    {"_id": node_id}, {"$set": {"cluster_id": cluster_id}}
                )

    def _materialize(
        self,
        nodes,
        output,
        timestamp: int,
        cluster_field: str = "cluster_id",
    ) -> None:
        cursor = nodes.find().sort(cluster_field, ASCENDING).batch_size(
            self.batch_size
        )
        current_cluster = None
        component = []
        output_buffer: list[tuple[dict, str, list[dict]]] = []

        def flush_output_buffer() -> None:
            if not output_buffer:
                return
            base_ids = [entry["_id"] for entry, _, _ in output_buffer]
            claimed = {
                item["_id"]
                for item in output.find(
                    {"_id": {"$in": list(set(base_ids))}}, {"_id": 1}
                )
            }
            documents = []
            for entry, anchor, component_snapshot in output_buffer:
                original_id = entry["_id"]
                if original_id in claimed:
                    entry["_id"] = sha256(
                        (
                            f"{GRAPH_VERSION}|collision|{original_id}|{anchor}"
                        ).encode("utf-8")
                    ).hexdigest()
                    entry["identity_collision_of"] = original_id
                    self.metrics["materialized_identity_collisions"] += 1
                    review_id = sha256(
                        (
                            f"{self.run_id}|{self.current_stage}|"
                            f"materialized_identity_collision|{original_id}|{anchor}"
                        ).encode("utf-8")
                    ).hexdigest()
                    self.review_buffer.append(
                        {
                            "_id": review_id,
                            "run_id": self.run_id,
                            "stage": self.current_stage,
                            "status": "review",
                            "reason": "materialized_identity_collision",
                            "work_id": original_id,
                            "component": component_snapshot,
                        }
                    )
                claimed.add(entry["_id"])
                documents.append(entry)
            output.insert_many(documents, ordered=False)
            output_buffer.clear()
            if len(self.review_buffer) >= self.batch_size:
                self._flush_reviews()

        def append_component() -> None:
            if not component:
                return
            entry = materialize_work(
                component,
                timestamp,
                self.profile_name_index,
                self._authority_for_component(component),
                self.group_affiliation_index,
            )
            self.metrics["author_affiliations_materialized"] += sum(
                len(author.get("affiliations") or [])
                for author in entry.get("authors") or []
            )
            self.metrics["group_affiliations_materialized"] += sum(
                bool(group.get("affiliations"))
                for group in entry.get("groups") or []
            )
            anchor = min(node["_id"] for node in component)
            output_buffer.append(
                (entry, anchor, [compact_node(node) for node in component])
            )
            self.metrics["works"] += 1
            if len(output_buffer) >= self.batch_size:
                flush_output_buffer()

        for node in cursor:
            cluster_id = node[cluster_field]
            if current_cluster is not None and cluster_id != current_cluster:
                append_component()
                component = []
            current_cluster = cluster_id
            component.append(node)
        append_component()
        flush_output_buffer()

    @staticmethod
    def _create_output_indexes(collection) -> None:
        collection.create_index("year_published")
        collection.create_index("titles.title")
        collection.create_index("authors.id")
        collection.create_index("authors.affiliations.institution")
        collection.create_index("groups.affiliations")
        collection.create_index([("external_ids.source", 1), ("external_ids.id", 1)])


class CheckpointedNormalizedWorkGraphBuilder(CvlacWorkGraphBuilder):
    """Build a versioned graph from audited normalized sources with resume."""

    def __init__(
        self,
        *args,
        gate: dict[str, Any],
        publish_pointer: bool = True,
        **kwargs,
    ):
        kwargs["source_mode"] = "normalized"
        kwargs.setdefault(
            "recognized_groups_collection",
            gate.get("recognized_groups_collection", "recognized_groups"),
        )
        super().__init__(*args, **kwargs)
        self.use_compact_graph = True
        self.gate = dict(gate)
        self.publish_pointer = bool(publish_pointer)
        self.runs = self.db[f"{self.collection_name}_graph_runs"]
        self.nodes_name = ""
        self.edges_name = ""
        self.output_name = ""

    @property
    def config(self) -> dict[str, Any]:
        stable_gate = {
            key: value
            for key, value in self.gate.items()
            if key
            not in {
                "validated_at",
                "cvlac_audit_finished_at",
                "gruplac_audit_finished_at",
            }
        }
        return {
            "graph_version": GRAPH_VERSION,
            "affiliation_rule_version": AFFILIATION_RULE_VERSION,
            "publisher_entity_rule_version": PUBLISHER_ENTITY_RULE_VERSION,
            "source_mode": self.source_mode,
            "cvlac_normalized_collection": self.source_collection_name,
            "gruplac_normalized_collection": self.group_source_collection_name,
            "recognized_groups_collection": self.recognized_groups_collection_name,
            "target_collection": self.collection_name,
            "authority_collection": self.authority_collection_name,
            "researcher_collections": list(self.researcher_collections),
            "batch_size": self.batch_size,
            "max_title_group_size": self.max_title_group_size,
            "candidate_partitions": self.candidate_partitions,
            "publish_pointer": self.publish_pointer,
            "union_find": "numpy-int32-v1",
            "gate": stable_gate,
        }

    def _artifact_names(self, run_name: str) -> dict[str, str]:
        safe_target = re.sub(r"[^A-Za-z0-9_]", "_", self.collection_name)
        safe_run = re.sub(r"[^A-Za-z0-9_]", "_", run_name)
        prefix = f"__yuku_{safe_target}_{safe_run}"
        return {
            "nodes": f"{prefix}_nodes",
            "edges": f"{prefix}_edges",
            "output": f"{prefix}_build",
        }

    def _stage_status(self, stage: str) -> str:
        run = self.runs.find_one({"_id": self.run_id}, {f"stages.{stage}": 1}) or {}
        return str(((run.get("stages") or {}).get(stage) or {}).get("status") or "")

    def _start_stage(self, stage: str) -> None:
        now = int(time())
        self.current_stage = stage
        self.metrics = Counter()
        self.review_buffer = []
        self.edge_buffer = []
        self.current_partition = None
        self.runs.update_one(
            {"_id": self.run_id},
            {
                "$set": {
                    "current_stage": stage,
                    f"stages.{stage}.status": "running",
                    f"stages.{stage}.started_at": now,
                },
                "$inc": {f"stages.{stage}.attempts": 1},
                "$unset": {
                    f"stages.{stage}.error": "",
                    f"stages.{stage}.failed_at": "",
                },
            },
        )
        print(
            f"INFO: work graph run={self.run_id} stage={stage} started.",
            flush=True,
        )

    def _complete_stage(self, stage: str, result: dict[str, Any]) -> None:
        now = int(time())
        self.runs.update_one(
            {"_id": self.run_id},
            {
                "$set": {
                    f"stages.{stage}.status": "complete",
                    f"stages.{stage}.finished_at": now,
                    f"stages.{stage}.metrics": dict(self.metrics),
                    f"stages.{stage}.result": result,
                    "last_checkpoint": stage,
                    "last_checkpoint_at": now,
                }
            },
        )
        print(
            f"INFO: work graph run={self.run_id} stage={stage} complete, "
            f"result={result}.",
            flush=True,
        )

    def _fail_stage(self, stage: str, error: Exception) -> None:
        now = int(time())
        message = f"{type(error).__name__}: {error}"
        self.runs.update_one(
            {"_id": self.run_id},
            {
                "$set": {
                    "status": "failed",
                    "failed_at": now,
                    "error": message,
                    "current_stage": stage,
                    f"stages.{stage}.status": "failed",
                    f"stages.{stage}.failed_at": now,
                    f"stages.{stage}.error": message,
                    f"stages.{stage}.metrics": dict(self.metrics),
                }
            },
        )

    @staticmethod
    def _partition_key(partition: int) -> str:
        return "p{:04d}".format(int(partition))

    def _partition_status(self, stage: str, partition: int) -> str:
        key = self._partition_key(partition)
        run = self.runs.find_one(
            {"_id": self.run_id},
            {f"stages.{stage}.partitions.{key}": 1},
        ) or {}
        partition_state = (
            (((run.get("stages") or {}).get(stage) or {}).get("partitions") or {})
            .get(key, {})
        )
        return str(partition_state.get("status") or "")

    def _start_partition(self, stage: str, partition: int) -> Counter:
        key = self._partition_key(partition)
        now = int(time())
        self.current_partition = partition
        before = Counter(self.metrics)
        self.runs.update_one(
            {"_id": self.run_id},
            {
                "$set": {
                    f"stages.{stage}.partitions.{key}.status": "running",
                    f"stages.{stage}.partitions.{key}.started_at": now,
                },
                "$inc": {f"stages.{stage}.partitions.{key}.attempts": 1},
                "$unset": {
                    f"stages.{stage}.partitions.{key}.error": "",
                    f"stages.{stage}.partitions.{key}.failed_at": "",
                },
            },
        )
        print(
            f"INFO: work graph run={self.run_id} stage={stage} "
            f"partition={partition + 1}/{self.candidate_partitions} started.",
            flush=True,
        )
        return before

    def _complete_partition(
        self,
        stage: str,
        partition: int,
        before: Counter,
        result: dict[str, Any],
    ) -> None:
        key = self._partition_key(partition)
        metrics = {
            name: value - before.get(name, 0)
            for name, value in self.metrics.items()
            if value - before.get(name, 0)
        }
        self.runs.update_one(
            {"_id": self.run_id},
            {
                "$set": {
                    f"stages.{stage}.partitions.{key}.status": "complete",
                    f"stages.{stage}.partitions.{key}.finished_at": int(time()),
                    f"stages.{stage}.partitions.{key}.metrics": metrics,
                    f"stages.{stage}.partitions.{key}.result": result,
                }
            },
        )
        print(
            f"INFO: work graph run={self.run_id} stage={stage} "
            f"partition={partition + 1}/{self.candidate_partitions} complete, "
            f"result={result}.",
            flush=True,
        )

    def _fail_partition(
        self, stage: str, partition: int, error: Exception
    ) -> None:
        key = self._partition_key(partition)
        self.runs.update_one(
            {"_id": self.run_id},
            {
                "$set": {
                    f"stages.{stage}.partitions.{key}.status": "failed",
                    f"stages.{stage}.partitions.{key}.failed_at": int(time()),
                    f"stages.{stage}.partitions.{key}.error": (
                        f"{type(error).__name__}: {error}"
                    ),
                }
            },
        )

    def _prepare_run(self, run_name: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", run_name or ""):
            raise ValueError("graph run name must contain only letters, numbers and underscores")
        self.run_id = run_name
        artifacts = self._artifact_names(run_name)
        self.nodes_name = artifacts["nodes"]
        self.edges_name = artifacts["edges"]
        self.output_name = artifacts["output"]
        existing = self.runs.find_one({"_id": run_name})
        if existing:
            if (existing.get("config") or {}) != self.config:
                raise ValueError(
                    f"graph run {run_name!r} exists with a different config"
                )
            return existing
        if self.collection_name in self.db.list_collection_names():
            raise ValueError(
                f"versioned graph target {self.collection_name!r} already exists, "
                "use a new target collection name"
            )
        now = int(time())
        document = {
            "_id": run_name,
            "status": "created",
            "created_at": now,
            "config": self.config,
            "gate_evidence": self.gate,
            "artifacts": artifacts,
            "target_existed_at_start": False,
            "stages": {},
        }
        self.runs.insert_one(document)
        return document

    def _reset_cluster_ids(self, nodes) -> None:
        try:
            nodes.update_many({}, [{"$set": {"cluster_seq": "$node_seq"}}])
            invalid = nodes.count_documents({"$expr": {"$ne": ["$cluster_seq", "$node_seq"]}})
            if invalid:
                raise RuntimeError("MongoDB did not reset compact cluster identifiers")
            return
        except (TypeError, NotImplementedError, RuntimeError):
            pass
        updates: list[tuple[int, int]] = []
        for item in nodes.find({}, {"node_seq": 1}).batch_size(self.batch_size):
            node_seq = int(item["node_seq"])
            updates.append((node_seq, node_seq))
            if len(updates) >= self.batch_size:
                self._apply_compact_cluster_updates(nodes, updates)
                updates = []
        if updates:
            self._apply_compact_cluster_updates(nodes, updates)

    @staticmethod
    def _apply_compact_cluster_updates(
        collection, updates: list[tuple[int, int]]
    ) -> None:
        operations = [
            UpdateOne(
                {"node_seq": node_seq},
                {"$set": {"cluster_seq": cluster_seq}},
            )
            for node_seq, cluster_seq in updates
        ]
        try:
            collection.bulk_write(operations, ordered=False)
        except TypeError:
            for node_seq, cluster_seq in updates:
                collection.update_one(
                    {"node_seq": node_seq},
                    {"$set": {"cluster_seq": cluster_seq}},
                )

    def _new_compact_graph(self, nodes) -> CompactUnionFind:
        count = nodes.count_documents({})
        if count:
            bounds = next(
                nodes.aggregate(
                    [
                        {
                            "$group": {
                                "_id": None,
                                "minimum": {"$min": "$node_seq"},
                                "maximum": {"$max": "$node_seq"},
                                "count": {"$sum": 1},
                            }
                        },
                    ],
                    allowDiskUse=True,
                ),
                {},
            )
            if (
                int(bounds.get("minimum", -1)) != 0
                or int(bounds.get("maximum", -1)) != count - 1
                or int(bounds.get("count", 0)) != count
            ):
                raise RuntimeError("node_seq values are not dense and unique")
        return CompactUnionFind(count)

    def _restore_graph(self, nodes, edges, stages: list[str]) -> None:
        self.graph = self._new_compact_graph(nodes)
        cursor = edges.find(
            {"run_id": self.run_id, "stage": {"$in": stages}},
            {"left_id": 1, "right_id": 1},
        ).sort([("stage", ASCENDING), ("_id", ASCENDING)])
        for batch in self._batches(cursor, self.batch_size):
            node_ids = {
                value
                for edge in batch
                for value in (edge["left_id"], edge["right_id"])
            }
            snapshot = {
                item["_id"]: item
                for item in nodes.find({"_id": {"$in": list(node_ids)}})
            }
            if len(snapshot) != len(node_ids):
                raise RuntimeError("checkpoint edge references a missing graph node")
            for edge in batch:
                left = snapshot[edge["left_id"]]
                right = snapshot[edge["right_id"]]
                self.graph.add(left)
                self.graph.add(right)
                self.graph.union(
                    self._graph_key(left), self._graph_key(right)
                )

    def _write_compact_cluster_ids(self, nodes) -> dict[str, int]:
        if not isinstance(self.graph, CompactUnionFind):
            raise RuntimeError("compact graph state is unavailable")
        updates: list[tuple[int, int]] = []
        active_nodes = 0
        for node_seq in range(self.graph.capacity):
            if not self.graph.active[node_seq]:
                continue
            active_nodes += 1
            root = self.graph.find(node_seq)
            if root != node_seq:
                updates.append((node_seq, root))
            if len(updates) >= self.batch_size:
                self._apply_compact_cluster_updates(nodes, updates)
                updates = []
        if updates:
            self._apply_compact_cluster_updates(nodes, updates)
        components = sum(
            int(self.graph.size[root]) > 1 for root in self.graph.summary
        )
        self.metrics["components"] = components
        return {
            "capacity": self.graph.capacity,
            "active_nodes": active_nodes,
            "components": components,
            "array_bytes": self.graph.storage_bytes,
        }

    @staticmethod
    def _sum_field(collection, field: str) -> int:
        result = next(
            collection.aggregate(
                [
                    {
                        "$group": {
                            "_id": None,
                            "total": {"$sum": {"$ifNull": [f"${field}", 0]}},
                        }
                    }
                ]
            ),
            {},
        )
        return int(result.get("total") or 0)

    def _audit_build(self, nodes, output) -> dict[str, Any]:
        cluster_field = "cluster_seq" if self.use_compact_graph else "cluster_id"
        source_profiles = self.db[self.source_collection_name].count_documents({})
        source_groups = self.db[self.group_source_collection_name].count_documents({})
        cvlac_records = self._sum_field(
            self.db[self.source_collection_name], "production_counts"
        )
        gruplac_records = self._sum_field(
            self.db[self.group_source_collection_name], "production_count"
        )
        node_counts = {
            str(item["_id"]): int(item["count"])
            for item in nodes.aggregate(
                [{"$group": {"_id": "$source_kind", "count": {"$sum": 1}}}]
            )
        }
        nodes_count = sum(node_counts.values())
        cluster_result = next(
            nodes.aggregate(
                [
                    {"$group": {"_id": f"${cluster_field}"}},
                    {"$count": "count"},
                ],
                allowDiskUse=True,
            ),
            {},
        )
        clusters = int(cluster_result.get("count") or 0)
        works = output.count_documents({})
        run = self.runs.find_one({"_id": self.run_id}, {"stages.extract_nodes": 1}) or {}
        extraction_metrics = (
            (((run.get("stages") or {}).get("extract_nodes") or {}).get("metrics"))
            or {}
        )
        empty_titles = int(extraction_metrics.get("empty_titles") or 0)
        invalid_records = int(
            extraction_metrics.get("invalid_normalized_records") or 0
        )
        invalid_profiles = int(
            extraction_metrics.get("invalid_normalized_profiles") or 0
        )
        routing_exclusions = sum(
            int(value)
            for key, value in extraction_metrics.items()
            if key.startswith("excluded_") or key.endswith("_routing_unmapped")
        )
        expected_nodes = (
            cvlac_records + gruplac_records - empty_titles
            - invalid_records - routing_exclusions
        )
        affiliation_evidence = group_affiliation_evidence_fingerprint(
            self.db,
            self.recognized_groups_collection_name,
            self.group_source_collection_name,
        )
        critical = {
            "cvlac_profile_coverage": abs(
                source_profiles - int(self.gate["cvlac_profiles"])
            ),
            "gruplac_group_coverage": abs(
                source_groups - int(self.gate["gruplac_groups"])
            ),
            "node_count_mismatch": abs(nodes_count - expected_nodes),
            "invalid_normalized_profiles": invalid_profiles,
            "invalid_normalized_records": invalid_records,
            "missing_node_seq": (
                nodes.count_documents({"node_seq": {"$exists": False}})
                if self.use_compact_graph
                else 0
            ),
            "missing_node_cluster": nodes.count_documents(
                {cluster_field: {"$exists": False}}
            ),
            "work_cluster_mismatch": abs(works - clusters),
            "works_without_title": output.count_documents(
                {"titles.0": {"$exists": False}}
            ),
            "works_without_authors_field": output.count_documents(
                {"authors": {"$exists": False}}
            ),
            "authors_without_affiliations_field": output.count_documents(
                {
                    "authors": {
                        "$elemMatch": {"affiliations": {"$exists": False}}
                    }
                }
            ),
            "groups_without_affiliations_field": output.count_documents(
                {
                    "groups": {
                        "$elemMatch": {"affiliations": {"$exists": False}}
                    }
                }
            ),
            "affiliation_evidence_changed": int(
                affiliation_evidence["sha256"]
                != self.gate.get("affiliation_evidence_sha256")
            ),
        }
        critical_count = sum(critical.values())
        summary = {
            "status": "passed" if critical_count == 0 else "failed",
            "critical_anomalies": critical_count,
            "critical_counts": critical,
            "source_profiles": source_profiles,
            "source_groups": source_groups,
            "source_production": {
                "cvlac": cvlac_records,
                "gruplac": gruplac_records,
                "total": cvlac_records + gruplac_records,
            },
            "empty_titles": empty_titles,
            "routing_exclusions": routing_exclusions,
            "routing": {
                key: int(value)
                for key, value in sorted(extraction_metrics.items())
                if key.startswith(("cvlac_routed_", "gruplac_routed_", "excluded_"))
                or key.endswith("_routing_unmapped")
            },
            "node_counts": node_counts,
            "nodes": nodes_count,
            "clusters": clusters,
            "works": works,
            "affiliation_evidence": affiliation_evidence,
        }
        self.runs.update_one(
            {"_id": self.run_id},
            {"$set": {"audit": summary, "audit_at": int(time())}},
        )
        if critical_count:
            raise RuntimeError(f"work graph audit failed: {critical}")
        return summary

    def _publish(self, output) -> dict[str, Any]:
        audit = (self.runs.find_one({"_id": self.run_id}) or {}).get("audit") or {}
        expected_works = int(audit.get("works") or 0)
        existing = set(self.db.list_collection_names())
        if self.collection_name in existing:
            if self.output_name in existing:
                raise RuntimeError(
                    "both versioned target and temporary output exist; refusing overwrite"
                )
            if self.db[self.collection_name].count_documents({}) != expected_works:
                raise RuntimeError("existing target does not match the audited output")
        else:
            if self.output_name not in existing:
                raise RuntimeError("audited temporary output is missing")
            output.rename(self.collection_name, dropTarget=False)

        publications = self.db[WORK_GRAPH_PUBLICATIONS]
        pointer = publications.find_one({"_id": "current"}) or {}
        previous = str(pointer.get("current_collection") or "")
        if previous == self.collection_name:
            previous = str(pointer.get("previous_collection") or "")
        published_at = int(time())
        publications.replace_one(
            {"_id": self.run_id},
            {
                "_id": self.run_id,
                "status": "published",
                "graph_version": GRAPH_VERSION,
                "publisher_entity_rule_version": PUBLISHER_ENTITY_RULE_VERSION,
                "collection": self.collection_name,
                "previous_collection": previous,
                "works": expected_works,
                "published_at": published_at,
                "gate": self.config["gate"],
            },
            upsert=True,
        )
        if self.publish_pointer:
            publications.replace_one(
                {"_id": "current"},
                {
                    "_id": "current",
                    "current_collection": self.collection_name,
                    "current_run_name": self.run_id,
                    "previous_collection": previous,
                    "published_at": published_at,
                },
                upsert=True,
            )
        return {
            "collection": self.collection_name,
            "previous_collection": previous,
            "pointer_published": self.publish_pointer,
            "works": expected_works,
            "published_at": published_at,
        }

    def build_checkpointed(self, run_name: str) -> dict[str, Any]:
        run = self._prepare_run(run_name)
        if run.get("status") == "complete":
            return dict(run.get("summary") or {})

        now = int(time())
        self.runs.update_one(
            {"_id": self.run_id},
            {
                "$set": {
                    "status": "running",
                    "last_started_at": now,
                    "gate_validated_at": self.gate.get("validated_at"),
                },
                "$inc": {"execution_attempts": 1},
                "$unset": {"error": "", "failed_at": ""},
            },
        )
        self.review_collection = self.db[f"{self.collection_name}_graph_review"]
        self.review_collection.create_index(
            [
                ("run_id", ASCENDING),
                ("stage", ASCENDING),
                ("partition", ASCENDING),
                ("reason", ASCENDING),
            ]
        )
        nodes = self.db[self.nodes_name]
        edges = self.db[self.edges_name]
        output = self.db[self.output_name]
        self.edge_collection = edges
        self.profile_name_index = {}
        self.profile_name_by_id = {}
        self.authority_index = {}
        self.group_affiliation_index = {}
        self._load_researcher_name_index()
        self._load_authority_index()
        self._load_group_affiliation_index()

        def extract_nodes() -> dict[str, Any]:
            nodes.drop()
            edges.drop()
            output.drop()
            self.review_collection.delete_many({"run_id": self.run_id})
            self.edge_collection = edges
            self.next_node_seq = 0
            edges.create_index(
                [
                    ("run_id", ASCENDING),
                    ("stage", ASCENDING),
                    ("partition", ASCENDING),
                    ("_id", ASCENDING),
                ]
            )
            self._extract_nodes(nodes, None)
            self._extract_gruplac_nodes(nodes)
            nodes.create_index([("node_seq", ASCENDING)], unique=True)
            nodes.create_index([("cluster_seq", ASCENDING)])
            nodes.create_index(
                [
                    ("title_partition", ASCENDING),
                    ("title_key", ASCENDING),
                    ("year", ASCENDING),
                ]
            )
            nodes.create_index([("identity_isbns", ASCENDING)])
            nodes.create_index([("identity_dois", ASCENDING)])
            nodes.create_index(
                [("source_kind", ASCENDING), ("source_id", ASCENDING)]
            )
            nodes.create_index(
                [
                    ("source_kind", ASCENDING),
                    ("source_id", ASCENDING),
                    ("source_record_index", ASCENDING),
                ],
                unique=True,
            )
            return {
                "nodes": nodes.count_documents({}),
                "profiles": self.metrics["profiles"],
                "groups": self.metrics["groups"],
                "candidate_partitions": self.candidate_partitions,
                "node_seq_count": self.next_node_seq,
                "routing": {
                    key: value
                    for key, value in sorted(self.metrics.items())
                    if key.startswith(("cvlac_routed_", "gruplac_routed_", "excluded_"))
                    or key.endswith("_routing_unmapped")
                },
            }

        def connect_doi() -> dict[str, Any]:
            edges.delete_many({"run_id": self.run_id, "stage": "connect_doi"})
            self.review_collection.delete_many(
                {"run_id": self.run_id, "stage": "connect_doi"}
            )
            self.graph = self._new_compact_graph(nodes)
            self.current_partition = None
            self._connect_doi_groups(nodes)
            self._flush_edges()
            self._flush_reviews()
            return {
                "edges": edges.count_documents(
                    {"run_id": self.run_id, "stage": "connect_doi"}
                ),
                "reviews": self.review_collection.count_documents(
                    {"run_id": self.run_id, "stage": "connect_doi"}
                ),
                "compact_array_bytes": self.graph.storage_bytes,
            }

        def connect_title_year() -> dict[str, Any]:
            run_state = self.runs.find_one(
                {"_id": self.run_id},
                {"stages.connect_title_year.cluster_write_started": 1},
            ) or {}
            title_state = (
                (run_state.get("stages") or {}).get("connect_title_year") or {}
            )
            completed = [
                partition
                for partition in range(self.candidate_partitions)
                if self._partition_status("connect_title_year", partition)
                == "complete"
            ]
            stale_filter = {
                "run_id": self.run_id,
                "stage": "connect_title_year",
            }
            if completed:
                stale_filter["$or"] = [
                    {"partition": {"$exists": False}},
                    {"partition": {"$nin": completed}},
                ]
            edges.delete_many(stale_filter)
            self.review_collection.delete_many(stale_filter)
            if title_state.get("cluster_write_started"):
                self._reset_cluster_ids(nodes)
            self._restore_graph(
                nodes, edges, ["connect_doi", "connect_title_year"]
            )
            for partition in range(self.candidate_partitions):
                if partition in completed:
                    continue
                before = self._start_partition(
                    "connect_title_year", partition
                )
                try:
                    self._connect_title_groups(nodes, partition=partition)
                    self._flush_edges()
                    self._flush_reviews()
                    result = {
                        "edges": edges.count_documents(
                            {
                                "run_id": self.run_id,
                                "stage": "connect_title_year",
                                "partition": partition,
                            }
                        ),
                        "reviews": self.review_collection.count_documents(
                            {
                                "run_id": self.run_id,
                                "stage": "connect_title_year",
                                "partition": partition,
                            }
                        ),
                    }
                    self._complete_partition(
                        "connect_title_year", partition, before, result
                    )
                    completed.append(partition)
                except Exception as error:
                    self._flush_edges()
                    self._flush_reviews()
                    self._fail_partition(
                        "connect_title_year", partition, error
                    )
                    raise
            self.current_partition = None
            self.runs.update_one(
                {"_id": self.run_id},
                {
                    "$set": {
                        "stages.connect_title_year.cluster_write_started": True,
                        "stages.connect_title_year.cluster_write_started_at": int(time()),
                    }
                },
            )
            compact = self._write_compact_cluster_ids(nodes)
            self.graph = UnionFind()
            return {
                "edges": edges.count_documents(
                    {"run_id": self.run_id, "stage": "connect_title_year"}
                ),
                "reviews": self.review_collection.count_documents(
                    {"run_id": self.run_id, "stage": "connect_title_year"}
                ),
                "partitions": len(completed),
                "compact_graph": compact,
            }

        def materialize() -> dict[str, Any]:
            output.drop()
            self.review_collection.delete_many(
                {"run_id": self.run_id, "stage": "materialize"}
            )
            self._materialize(
                nodes, output, int(time()), cluster_field="cluster_seq"
            )
            self._flush_reviews()
            self._create_output_indexes(output)
            return {"works": output.count_documents({})}

        handlers = {
            "extract_nodes": extract_nodes,
            "connect_doi": connect_doi,
            "connect_title_year": connect_title_year,
            "materialize": materialize,
            "audit": lambda: self._audit_build(nodes, output),
            "publish": lambda: self._publish(output),
        }
        current_stage = ""
        try:
            for stage in CHECKPOINT_STAGES:
                if self._stage_status(stage) == "complete":
                    print(
                        f"INFO: work graph run={self.run_id} stage={stage} "
                        "already complete; resuming after checkpoint.",
                        flush=True,
                    )
                    continue
                current_stage = stage
                self._start_stage(stage)
                result = handlers[stage]()
                self._complete_stage(stage, result)

            final = self.runs.find_one({"_id": self.run_id}) or {}
            stages = final.get("stages") or {}
            audit = final.get("audit") or {}
            summary = {
                "run_id": self.run_id,
                "status": "complete",
                "graph_version": GRAPH_VERSION,
                "publisher_entity_rule_version": PUBLISHER_ENTITY_RULE_VERSION,
                "collection": self.collection_name,
                "previous_collection": (
                    ((stages.get("publish") or {}).get("result") or {}).get(
                        "previous_collection", ""
                    )
                ),
                "pointer_published": bool(
                    ((stages.get("publish") or {}).get("result") or {}).get(
                        "pointer_published"
                    )
                ),
                "profiles": int(audit.get("source_profiles") or 0),
                "groups": int(audit.get("source_groups") or 0),
                "nodes": int(audit.get("nodes") or 0),
                "works": int(audit.get("works") or 0),
                "critical_anomalies": int(audit.get("critical_anomalies") or 0),
                "routing": audit.get("routing") or {},
                "review_edges": self.review_collection.count_documents(
                    {"run_id": self.run_id}
                ),
            }
            self.runs.update_one(
                {"_id": self.run_id},
                {
                    "$set": {
                        "status": "complete",
                        "summary": summary,
                        "finished_at": int(time()),
                        "current_stage": "",
                    }
                },
            )
            nodes.drop()
            edges.drop()
            return summary
        except Exception as error:
            for name, flush in (
                ("edges", self._flush_edges),
                ("reviews", self._flush_reviews),
            ):
                try:
                    flush()
                except Exception as flush_error:
                    print(
                        f"WARNING: failed to flush {name} while handling "
                        f"{type(error).__name__}: {type(flush_error).__name__}: "
                        f"{flush_error}",
                        flush=True,
                    )
            self._fail_stage(current_stage or self.current_stage or "unknown", error)
            print(
                f"ERROR: checkpointed work graph run={self.run_id} "
                f"stage={current_stage} failed: {type(error).__name__}: {error}",
                flush=True,
            )
            raise
