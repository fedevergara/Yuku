"""Names, monotonic current pointers and bounded retention for final entities."""

from copy import deepcopy
from datetime import datetime
import logging
import re
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from datetime import timedelta, timezone
from uuid import uuid4

from pymongo.errors import DuplicateKeyError


ENTITIES = ("works", "person", "projects", "events", "patents", "affiliations")
ALIASES = {"persons": "person"}
CATALOG = "minciencias_entity_versions"
LOCKS = "minciencias_publication_locks"
PERMANENT_CONTROLS = frozenset({"minciencias_doi_resolution_history"})
# Writers include the owner in both the row and its generated _id. Only
# these proven schemas may be pruned; arbitrary field/value deletion is forbidden.
ROW_OWNERS = {
    "scienti_doi_identity_checks": ("run_name", "scienti_doi_reconciliation_runs"),
    "scienti_entity_graph_reviews": ("run_name", "scienti_entity_graph_runs"),
    "scienti_project_semantic_audit_anomalies": ("audit_name", "scienti_project_semantic_audits"),
    "scienti_auxiliary_semantic_audit_anomalies": ("audit_name", "scienti_auxiliary_semantic_audits"),
}
NAME = re.compile(r"minciencias_(works|person|projects|events|patents|affiliations)_v([0-9]{2,})_([0-9]{8})(_filtered)?")
LEGACY = re.compile(r"_v([0-9]+)_([0-9]{8})(?:_v[0-9]+)?$")


def publication_date(value=None):
    value = value or datetime.now(ZoneInfo("America/Bogota")).strftime("%Y%m%d")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise ValueError("publication date must be aaaammdd")
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as error:
        raise ValueError("publication date is not a calendar date") from error
    if parsed.strftime("%Y%m%d") != value:
        raise ValueError("publication date must be aaaammdd")
    return value


def version_number(value):
    if isinstance(value, str) and re.fullmatch(r"v[0-9]{2,}", value):
        value = int(value[1:])
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("entity version must be a positive integer or vXX")
    return value


def collection_name(entity, version, date, *, filtered=False):
    entity = ALIASES.get(entity, entity)
    if entity not in ENTITIES or (filtered and entity != "works"):
        raise ValueError("unsupported final entity or filtered collection")
    return f"minciencias_{entity}_v{version_number(version):02d}_{publication_date(date)}" + ("_filtered" if filtered else "")


def parse_name(value, *, required=False):
    match = NAME.fullmatch(value) if isinstance(value, str) else None
    if not match:
        if required:
            raise ValueError("final collection must follow minciencias_entidad_vXX_aaaammdd[_filtered]")
        return None
    entity, version, date, filtered = match.groups()
    if filtered and entity != "works":
        raise ValueError("only works can have a filtered collection")
    return {"entity": entity, "version": version_number(int(version)),
            "date": publication_date(date), "filtered": bool(filtered)}


def rank(value):
    parsed = parse_name(value)
    if parsed:
        return parsed["version"], parsed["date"]
    match = LEGACY.search(value or "")
    return (int(match[1]), publication_date(match[2])) if match else None


def _pointer_collections(pointer):
    result = {ALIASES.get(key, key): value for key, value in pointer.get("collections", {}).items()}
    direct = pointer.get("current_collection") or pointer.get("collection")
    parsed = parse_name(direct)
    if direct:
        # Legacy direct pointers still participate in the transition guard.
        entity = parsed["entity"] if parsed else pointer.get("target_entity", "direct")
        result[(entity, bool(parsed and parsed["filtered"]))] = direct
    if pointer.get("full_graph"):
        result[("works", False)] = pointer["full_graph"]
    return result


def assert_current_transition(previous, candidate):
    old, new = _pointer_collections(previous), _pointer_collections(candidate)
    if previous.get("collections") and set(previous["collections"]) - set(candidate.get("collections", {})):
        raise RuntimeError("current cannot lose published entities")
    # Direct pointers can change from legacy to canonical names.
    if len(old) == len(new) == 1:
        old = {next(iter(new)): next(iter(old.values()))}
    for entity, target in new.items():
        origin = old.get(entity)
        if not origin or origin == target:
            continue
        target_rank, origin_rank = rank(target), rank(origin)
        if target_rank and origin_rank:
            if target_rank[0] <= origin_rank[0] or target_rank[1] < origin_rank[1]:
                raise RuntimeError(f"current cannot regress or reuse a version: {origin} -> {target}")
        elif origin_rank and parse_name(origin):
            raise RuntimeError("a canonical current cannot be replaced by an unversioned output")


def publish_current(db, namespace, pointer_id, value):
    """Compare and swap a pointer; stale concurrent publications cannot win."""
    collection = db[namespace]
    previous = collection.find_one({"_id": pointer_id}) or {}
    assert_current_transition(previous, value)
    if previous == value:
        return
    if not previous:
        try:
            collection.insert_one(value)
        except DuplicateKeyError as error:
            raise RuntimeError("current changed concurrently; retry publication") from error
        return
    conditions = [{key: item} for key, item in previous.items() if key != "_id"]
    query = {"_id": pointer_id, **({"$and": conditions} if conditions else {})}
    result = collection.replace_one(query, value)
    if result.matched_count != 1:
        raise RuntimeError("current changed concurrently; retry publication")


def current_collection(db, entity, *, filtered=False):
    entity = ALIASES.get(entity, entity)
    if filtered:
        return (db.scienti_identified_work_publications.find_one({"_id": "current"}) or {}).get("collection")
    joint = db.scienti_final_release_publications.find_one({"_id": "current"}) or {}
    key = "persons" if entity == "person" else entity
    joint_collection = joint.get("collections", {}).get(key)
    namespace, pointer = {
        "works": ("scienti_work_graph_publications", "current"),
        "person": ("scienti_person_publications", "current"),
        "affiliations": ("scienti_affiliation_publications", "current"),
    }.get(entity, ("scienti_final_entity_publications", "current_" + entity))
    doc = db[namespace].find_one({"_id": pointer}) or {}
    direct = doc.get("current_collection") or doc.get("collection")
    if direct and joint_collection and rank(direct) and rank(joint_collection):
        return max((direct, joint_collection), key=rank)
    return joint_collection or direct


def resolve_target(db, entity, *, target="", version="auto", date=None,
                   run_name="", run_namespace=""):
    """Pin naming to an existing checkpoint when resuming an entity build."""
    existing = db[run_namespace].find_one({"_id": run_name}) if run_namespace and run_name else None
    pinned = (existing or {}).get("config", {}).get("target_collection")
    if not target and pinned:
        target = pinned
    if target:
        spec = parse_name(target, required=True)
        if spec["entity"] != ALIASES.get(entity, entity) or spec["filtered"]:
            raise ValueError("target does not match the requested final entity")
        if version != "auto" and spec["version"] != version_number(version):
            raise ValueError("target and requested entity version differ")
        if date and spec["date"] != publication_date(date):
            raise ValueError("target and requested publication date differ")
        return target
    return collection_name(entity, next_version(db, entity) if version == "auto" else version,
                           publication_date(date))


def filtered_target(db, release_name, target=""):
    release = db.scienti_final_release_publications.find_one({"_id": release_name}) or {}
    full = release.get("collections", {}).get("works")
    spec = parse_name(full, required=True)
    if spec["entity"] != "works" or spec["filtered"]:
        raise ValueError("filtered works require a full works release")
    expected = full + "_filtered"
    if target and target != expected:
        raise ValueError("filtered works must share the full collection's version and date")
    return expected


def publish_filtered_current(db, release_name, identified):
    """An independent selection can promote only the current full release."""
    current = db.scienti_final_release_publications.find_one({"_id": "current"}) or {}
    if current.get("current_release") != release_name:
        return False
    release = db.scienti_final_release_publications.find_one({"_id": release_name}) or {}
    release_audit = db.scienti_final_release_audits.find_one({"_id": release.get("audit")}) or {}
    if (release.get("status") != "published" or release_audit.get("status") != "passed"
            or release_audit.get("critical_anomalies") != 0
            or release_audit.get("collections") != release.get("collections")
            or current.get("collections") != release.get("collections")):
        raise RuntimeError("filtered current requires the matching passed current release")
    filtered_target(db, release_name, identified)
    proof = db.scienti_identified_work_publications.find_one({"_id": identified}) or {}
    audit = db.scienti_identified_work_audits.find_one({"_id": proof.get("audit")}) or {}
    run = db.scienti_identified_work_runs.find_one({"_id": proof.get("run_name")}) or {}
    source = audit.get("config", {}).get("source", {})
    if (proof.get("release_name") != release_name or audit.get("status") != "passed"
            or run.get("status") != "complete" or audit.get("config") != run.get("config")
            or source.get("collections") != release["collections"]
            or audit.get("config", {}).get("target") != identified
            or not audit.get("documents_digest") or audit.get("documents_digest") != run.get("documents_digest")
            or db[identified].count_documents({}) != proof.get("documents")
            or audit.get("documents") != proof.get("documents")):
        raise RuntimeError("filtered current requires a matching passed selection audit")
    for role in ("works", "persons"):
        if db[release["collections"][role]].count_documents({}) != source.get("counts", {}).get(role):
            raise RuntimeError("selection source changed before filtered publication")
    if db.scienti_final_release_publications.find_one({"_id": "current"}) != current:
        raise RuntimeError("current changed before filtered publication")
    publish_current(db, "scienti_identified_work_publications", "current", {
        "_id": "current", "release_name": release_name, "full_graph": release["collections"]["works"],
        "collection": identified, "run_name": proof["run_name"], "audit": proof["audit"],
        "documents": proof["documents"], "published_at": release["published_at"]})
    register_release(db, release)
    return True


def next_version(db, entity):
    entity = ALIASES.get(entity, entity)
    versions = [0]
    current = rank(current_collection(db, entity))
    if current:
        versions.append(current[0])
    for name in db.list_collection_names():
        spec = parse_name(name)
        if spec and spec["entity"] == entity:
            versions.append(spec["version"])
    return max(versions) + 1


def resolve_outputs(db, *, date, versions=None):
    """Read-only resolution; controllers pin the result before building."""
    versions = versions or {}
    if set(versions) - set(ENTITIES):
        raise ValueError("publication versions contain unknown entities")
    outputs = {entity: collection_name(entity,
                next_version(db, entity) if versions.get(entity, "auto") == "auto" else versions[entity], date)
            for entity in ENTITIES}
    for entity, target in outputs.items():
        assert_current_transition({"current_collection": current_collection(db, entity)},
                                  {"current_collection": target})
    return outputs


class EntityRetention:
    """Own artifacts explicitly; keep current/previous and live proof dependencies."""

    def __init__(self, db):
        self.db = db

    def register(self, collection, *, controls=(), temporaries=(), source_collections=(), row_controls=()):
        spec = parse_name(collection, required=True)
        if spec["filtered"]:
            return  # the filtered projection belongs to the same works version
        record = {"_id": collection, **spec, "collection": collection,
                  "controls": [dict(value) for value in controls],
                  "row_controls": [dict(value) for value in row_controls],
                  "temporaries": sorted(set(temporaries)),
                  "source_collections": sorted(set(source_collections))}
        previous = self.db[CATALOG].find_one({"_id": collection})
        if previous and previous != record:
            raise RuntimeError("registered entity ownership changed")
        self.db[CATALOG].replace_one({"_id": collection}, record, upsert=True)

    @contextmanager
    def lock(self):
        token = uuid4().hex
        now = datetime.now(timezone.utc)
        try:
            self.db[LOCKS].update_one({"_id": "retention"}, {"$setOnInsert": {"until": now}}, upsert=True)
        except DuplicateKeyError:
            pass
        result = self.db[LOCKS].update_one({"_id": "retention", "until": {"$lte": now}},
            {"$set": {"owner": token, "until": now + timedelta(minutes=15)}})
        if result.matched_count != 1:
            raise RuntimeError("another retention operation is active")
        try:
            yield
        finally:
            self.db[LOCKS].delete_one({"_id": "retention", "owner": token})

    @staticmethod
    def _strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                if key not in {"previous_run", "previous_run_name", "previous_release", "previous_collection"}:
                    yield from EntityRetention._strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from EntityRetention._strings(item)

    def plan(self):
        records = list(self.db[CATALOG].find({}))
        available = set(self.db.list_collection_names())
        kept, expired = [], []
        for entity in ENTITIES:
            group = sorted([r for r in records if r["entity"] == entity],
                           key=lambda r: (r["version"], r["date"]), reverse=True)
            current = current_collection(self.db, entity)
            # A newer build can exist without being current. Never prune it or
            # its predecessor as though it had already been published.
            current_record = next((r for r in group if r["collection"] == current), None)
            if not current_record:
                kept.extend(group)
                continue
            boundary = self._order(current_record)
            group.sort(key=self._order, reverse=True)
            previous = [r for r in group if self._order(r) < boundary][:1]
            retained = [r for r in group if self._order(r) >= boundary] + previous
            kept.extend(retained)
            expired.extend(r for r in group if r not in retained)
        controls = {}
        for record in records:
            for ref in record["controls"]:
                key = (ref["collection"], ref["id"])
                controls[key] = self.db[ref["collection"]].find_one({"_id": ref["id"]}) or {}
        live = {(r["collection"], r["id"]) for version in kept for r in version["controls"]}
        # Resolve reference chains by IDs and exact collection names. Shared
        # controls remain live if any retained entity depends on them.
        by_id = {}
        for key in controls:
            by_id.setdefault(str(key[1]), set()).add(key)
        sources = {name for r in kept for name in r["source_collections"]}
        frontier = list(live)
        while frontier:
            key = frontier.pop()
            for value in self._strings(controls.get(key, {})):
                if value in available:
                    sources.add(value)
                for dependency in by_id.get(value, ()):
                    if dependency not in live:
                        live.add(dependency)
                        frontier.append(dependency)
        removed_controls = sorted({(ref["collection"], ref["id"])
                                   for r in expired for ref in r["controls"]} - live)
        retained_temporaries = {name for r in kept for name in r["temporaries"]}
        retired_temporaries = sorted({name for r in expired for name in r["temporaries"]}
                                    - retained_temporaries - sources)
        rows = {}
        for r in expired:
            for ref in r.get("row_controls", []):
                field, namespace = ROW_OWNERS[ref["collection"]]
                if (namespace, ref["owner"]) not in live:
                    rows[(ref["collection"], ref["owner"])] = ref
        old_finals = []
        for record in records:
            latest = current_collection(self.db, record["entity"])
            current_record = next((r for r in records if r["collection"] == latest), None)
            if current_record and record["collection"] != latest and self._order(record) < self._order(current_record):
                old_finals.append(record["collection"])
                if record["entity"] == "works":
                    old_finals.extend(record.get("filtered_collections", [record["collection"] + "_filtered"]))
        # Completed source proofs can mention a retired dataset; they do not
        # require its data to stay queryable. Current data are always protected.
        return {"retained_versions": sorted(r["collection"] for r in kept),
                "expired_versions": sorted(r["collection"] for r in expired),
                "deferred_versions": sorted(r["collection"] for r in expired
                    if {(ref["collection"], ref["id"]) for ref in r["controls"]} & live
                    or set(r["temporaries"]) & sources),
                "controls": [{"collection": c, "id": i} for c, i in removed_controls],
                "row_controls": list(rows.values()),
                "temporaries": retired_temporaries, "finals": sorted(set(old_finals))}

    @staticmethod
    def _order(record):
        return (0 if record.get("legacy") else 1,
                record["date"] if record.get("legacy") else record["version"],
                record.get("published_at", 0) if record.get("legacy") else record["date"])

    def cleanup(self):
        with self.lock():
            return self._cleanup()

    def _cleanup(self):
        plan = self.plan()
        candidates = set(plan["finals"] + plan["temporaries"])
        if candidates & PERMANENT_CONTROLS or any(
                ref["collection"] in PERMANENT_CONTROLS
                for ref in plan["controls"] + plan["row_controls"]):
            raise RuntimeError("permanent DOI history is excluded from entity retention")
        current_data = {current_collection(self.db, entity) for entity in ENTITIES}
        current_data.add(current_collection(self.db, "works", filtered=True))
        joint = self.db.scienti_final_release_publications.find_one({"_id": "current"}) or {}
        current_data.update(joint.get("collections", {}).values())
        for namespace in ("scienti_work_graph_publications", "scienti_person_publications",
                          "scienti_affiliation_publications", "scienti_final_entity_publications",
                          "scienti_identified_work_publications"):
            for pointer in self.db[namespace].find({"_id": {"$regex": "^current"}}):
                current_data.update(_pointer_collections(pointer).values())
        if candidates & current_data:
            raise RuntimeError("retention would remove current data")
        if candidates:
            audit = self.db.scienti_final_release_audits.find_one({"_id": joint.get("audit")}) or {}
            if audit.get("status") != "passed" or audit.get("collections") != joint.get("collections"):
                raise RuntimeError("retention requires a matching passed current release audit")
        for name in self.db.list_collection_names():
            if not name.endswith("_runs"):
                continue
            for run in self.db[name].find({"status": {"$nin": ["complete", "passed", "published", "superseded_encoding_issue"]}}, {"config": 1}):
                refs = set(self._strings(run.get("config", {})))
                control_ids = {str(ref["id"]) for ref in plan["controls"]}
                if refs & (candidates | control_ids):
                    raise RuntimeError(f"unfinished run still depends on retention inputs: {name}/{run['_id']}")
        if (self.db.scienti_final_release_publications.find_one({"_id": "current"}) or {}) != joint:
            raise RuntimeError("current changed during retention planning")
        if any(ref["collection"] == "scienti_doi_identity_checks" for ref in plan["row_controls"]):
            # Retention can run before the next reconciliation. Seed historic
            # proofs before deleting the old per-run checks or their contexts.
            from yuku.scienti_doi_history import DOIResolutionHistory
            DOIResolutionHistory(self.db, "scienti_doi_identity_checks").bootstrap()
        # Match the exact BSON _id and proven owner on each deletion. A row
        # belonging to another run cannot match, even in shared namespaces.
        for ref in plan["row_controls"]:
            field, owner_namespace = ROW_OWNERS[ref["collection"]]
            if {"collection": owner_namespace, "id": ref["owner"]} not in plan["controls"]:
                raise RuntimeError("row owner is not an expired control")
            for row in self.db[ref["collection"]].find({field: ref["owner"]}, {"_id": 1}):
                self.db[ref["collection"]].delete_one({"_id": row["_id"], field: ref["owner"]})
        for ref in plan["controls"]:
            self.db[ref["collection"]].delete_one({"_id": ref["id"]})
        for namespace in {ref["collection"] for ref in plan["controls"] + plan["row_controls"]}:
            if not self.db[namespace].count_documents({}):
                self.db[namespace].drop()
        for name in candidates:
            self.db[name].drop()
        for collection in set(plan["expired_versions"]) - set(plan["deferred_versions"]):
            self.db[CATALOG].delete_one({"_id": collection})
        return {"status": "complete", **plan}


def _control_index(db):
    """Read bounded metadata namespaces, never graph/HTML/DOI row collections."""
    available = set(db.list_collection_names())
    controls, by_id = {}, {}
    for namespace in sorted(available):
        if not namespace.startswith(("scienti_", "minciencias_")) or not namespace.endswith(
                ("_runs", "_audits", "_publications", "_comparisons")):
            continue
        for doc in db[namespace].find({"_id": {"$not": {"$regex": "^current"}}}):
            if not isinstance(doc.get("_id"), str):
                continue
            key = (namespace, doc["_id"])
            controls[key] = doc
            by_id.setdefault(doc["_id"], set()).add(key)
    return available, controls, by_id


def _complete_materialization_proof(release):
    collections = release.get("collections")
    runs = release.get("materialization_runs")
    return (
        isinstance(collections, dict) and bool(collections)
        and isinstance(runs, dict)
        and all(isinstance(runs.get(role), str) and bool(runs[role].strip())
                for role in collections)
    )


def register_release(db, release):
    """Own audited final versions and the transitive proofs they actually use.

    Legacy adoption is metadata only. It does not rewrite immutable proofs or
    rename/drop any data. Legacy chronology follows date, avoiding the old
    mixed version counters (v11 in September versus v2 in October).
    """
    audit = db.scienti_final_release_audits.find_one({"_id": release.get("audit")}) or {}
    if (release.get("status") != "published" or audit.get("status") != "passed"
            or audit.get("critical_anomalies") != 0
            or audit.get("collections") != release.get("collections")
            or audit.get("materialization_runs") != release.get("materialization_runs")):
        raise RuntimeError("ownership registration requires a matching passed release with complete materialization proofs")
    collections = release.get("collections")
    if not isinstance(collections, dict) or not collections:
        raise RuntimeError("ownership registration requires final collections")
    if not any(parse_name(collection) or rank(collection) for collection in collections.values()):
        # Legacy callers also finalize unversioned datasets. They have no
        # version ownership to register and must not be added to retention.
        return
    if not _complete_materialization_proof(release):
        raise RuntimeError("ownership registration requires complete materialization proofs")
    available, documents, by_id = _control_index(db)
    for role, collection in release["collections"].items():
        entity = ALIASES.get(role, role)
        spec = parse_name(collection)
        if not spec:
            legacy_rank = rank(collection)
            if not legacy_rank:
                continue
            spec = {"entity": entity, "version": legacy_rank[0], "date": legacy_rank[1],
                    "filtered": False, "legacy": True}
        elif spec["entity"] != entity or spec["filtered"]:
            raise RuntimeError("release collection does not match its final entity")
        run_id = release["materialization_runs"][role]
        roots = set(by_id.get(run_id, ()))
        roots.update(by_id.get(release["_id"], ()))
        roots.update(by_id.get(release["audit"], ()))
        filtered = []
        extra = {}
        if entity == "works":
            for publication in db.scienti_identified_work_publications.find({"release_name": release["_id"], "_id": {"$ne": "current"}}):
                selection_audit = db.scienti_identified_work_audits.find_one({"_id": publication.get("audit")}) or {}
                selected_run = db.scienti_identified_work_runs.find_one({"_id": publication.get("run_name")}) or {}
                if (selection_audit.get("status") != "passed" or selected_run.get("status") != "complete"
                        or selection_audit.get("config", {}).get("target") != publication["_id"]
                        or selection_audit.get("config", {}).get("source", {}).get("collections", {}).get("works") != collection):
                    continue
                filtered.append(publication["_id"])
                roots.update(by_id.get(publication["_id"], ()))
                roots.update(by_id.get(publication["run_name"], ()))
                reservation = db.scienti_identified_work_targets.find_one({"_id": publication["_id"]})
                if reservation:
                    key = ("scienti_identified_work_targets", reservation["_id"])
                    roots.add(key)
                    extra[key] = reservation
        controls = {**documents, **extra}
        live, frontier = set(roots), list(roots)
        sources, temporaries = set(), set()
        while frontier:
            key = frontier.pop()
            doc = controls.get(key, {})
            for value in EntityRetention._strings(doc):
                if value in available:
                    sources.add(value)
                for dependency in by_id.get(value, ()):
                    if dependency not in live:
                        live.add(dependency)
                        frontier.append(dependency)
            config = doc.get("config", {})
            if key[0] == "minciencias_measurement_runs":
                temporaries.update(config[k] for k in ("graph_collection", "measured_collection", "links_collection") if config.get(k))
                graph = config.get("graph_collection", "")
                if graph + "_graph_runs" in available:
                    for graph_run in db[graph + "_graph_runs"].find({"status": "complete"}, {"_id": 1}):
                        dependency = (graph + "_graph_runs", graph_run["_id"])
                        if dependency not in live:
                            live.add(dependency)
                            frontier.append(dependency)
                    temporaries.add(graph + "_graph_review")
            if key[0] == "scienti_doi_reconciliation_runs":
                owner = doc["_id"]
                temporaries.update((f"__yuku_{owner}_doi_rows", owner + "_doi_candidates", owner + "_doi_corrections"))
                temporaries.update(config.get("targets", {}).values())
            if key[0] in {"scienti_cvlac_normalization_runs", "scienti_gruplac_normalization_runs"}:
                temporaries.update(config[k] for k in ("destination_collection", "error_collection") if config.get(k))
            if key[0] in {"scienti_cvlac_normalization_audits", "scienti_gruplac_normalization_audits"}:
                temporaries.add(doc["_id"] + "_anomalies")
                owner = config.get("verification_run_name")
                if owner:
                    temporaries.update(owner + "_" + suffix for suffix in ("targets", "raw", "downloads", "results"))
            if key[0] == "scienti_entity_normalization_runs":
                temporaries.update(config.get("destinations", {}).values())
        sources -= set(release["collections"].values()) | set(filtered) | temporaries
        record = {"_id": collection, **spec, "collection": collection,
                  "published_at": release.get("published_at", 0),
                  "controls": [{"collection": c, "id": i} for c, i in sorted(live)],
                  "row_controls": [{"collection": namespace, "owner": i}
                      for namespace, (_, owner_namespace) in ROW_OWNERS.items()
                      for c, i in sorted(live) if c == owner_namespace],
                  "temporaries": sorted(temporaries), "source_collections": sorted(sources),
                  "filtered_collections": sorted(filtered)}
        previous = db[CATALOG].find_one({"_id": collection})
        # The same entity can participate in a later joint release. Keep its
        # latest proof roots, rather than retaining every historical release.
        if previous and previous.get("published_at", 0) > record["published_at"]:
            continue
        db[CATALOG].replace_one({"_id": collection}, record, upsert=True)


def adopt_published_history(db):
    """Register existing proven outputs for bounded future retention (no drops)."""
    releases = db.scienti_final_release_publications.find({"status": "published"}).sort("published_at", 1)
    adopted = []
    for release in releases:
        if not _complete_materialization_proof(release):
            logging.getLogger(__name__).warning(
                "Skipping historical release %s: incomplete materialization_runs; "
                "data and audit remain unchanged", release.get("_id")
            )
            continue
        audit = db.scienti_final_release_audits.find_one({"_id": release.get("audit")}) or {}
        if (audit.get("status") != "passed" or audit.get("critical_anomalies") != 0
                or audit.get("collections") != release.get("collections")
                or audit.get("materialization_runs") != release.get("materialization_runs")):
            continue
        register_release(db, release)
        adopted.append(release["_id"])
    return adopted


def promote_release(db, release, *, identified=""):
    """Publish the audited six-role reference first, then consumer pointers."""
    audit = db.scienti_final_release_audits.find_one({"_id": release.get("audit")}) or {}
    if (not _complete_materialization_proof(release)
            or release.get("status") != "published" or release.get("entity_count") != 6
            or audit.get("status") != "passed" or audit.get("critical_anomalies") != 0
            or audit.get("collections") != release.get("collections")
            or audit.get("materialization_runs") != release.get("materialization_runs")):
        raise RuntimeError("current requires a matching passed six-entity release")
    if set(release["collections"]) != {"works", "persons", "projects", "events", "patents", "affiliations"}:
        raise RuntimeError("current requires all six entity roles")
    candidates = []
    for role, collection in release["collections"].items():
        entity = ALIASES.get(role, role)
        spec = parse_name(collection, required=True)
        if spec["entity"] != entity or spec["filtered"]:
            raise RuntimeError("release entity does not match its canonical collection")
        evidence = audit.get("evidence", {}).get(role, {})
        if (evidence.get("collection") != collection or collection not in db.list_collection_names()
                or db[collection].count_documents({}) != evidence.get("documents")):
            raise RuntimeError("audited final collection is missing or its count changed")
        run_id = release["materialization_runs"][role]
        if role in {"persons", "affiliations"}:
            stem = "person" if role == "persons" else "affiliation"
            namespace, pointer_id = f"scienti_{stem}_publications", "current"
            proof = db[namespace].find_one({"_id": run_id}) or {}
            if proof.get("collection") != collection or proof.get("status") != "published":
                raise RuntimeError("identity publication proof is missing")
            value = {"_id": "current", "current_run": run_id, "collection": collection,
                     "audit": proof["audit"], "published_at": proof["published_at"]}
        else:
            namespace = "scienti_work_graph_publications" if entity == "works" else "scienti_final_entity_publications"
            pointer_id = "current" if entity == "works" else "current_" + entity
            value = {"_id": pointer_id, "target_entity": entity, "current_collection": collection,
                     "current_run_name": run_id, "published_at": release["published_at"]}
        candidates.append((namespace, pointer_id, value))
    if identified:
        if identified != release["collections"]["works"] + "_filtered":
            raise RuntimeError("filtered collection must match the full version")
        proof = db.scienti_identified_work_publications.find_one({"_id": identified}) or {}
        selected_audit = db.scienti_identified_work_audits.find_one({"_id": proof.get("audit")}) or {}
        run = db.scienti_identified_work_runs.find_one({"_id": proof.get("run_name")}) or {}
        if (proof.get("release_name") != release["_id"] or selected_audit.get("status") != "passed"
                or run.get("status") != "complete" or not selected_audit.get("documents_digest")
                or selected_audit.get("documents_digest") != run.get("documents_digest")
                or selected_audit.get("config") != run.get("config")
                or selected_audit.get("config", {}).get("source", {}).get("collections") != release["collections"]
                or selected_audit.get("config", {}).get("target") != identified
                or db[identified].count_documents({}) != proof.get("documents")):
            raise RuntimeError("filtered publication proof is missing or changed")
        candidates.append(("scienti_identified_work_publications", "current", {
            "_id": "current", "release_name": release["_id"], "full_graph": release["collections"]["works"],
            "collection": identified, "run_name": proof["run_name"], "audit": proof["audit"],
            "documents": proof["documents"], "published_at": release["published_at"]}))
    else:
        old_filtered = db.scienti_identified_work_publications.find_one({"_id": "current"}) or {}
        if old_filtered.get("full_graph") and old_filtered["full_graph"] != release["collections"]["works"]:
            raise RuntimeError("a published filtered pair requires rebuilding its filtered successor")
    previous = db.scienti_final_release_publications.find_one({"_id": "current"}) or {}
    value = {"_id": "current", "current_release": release["_id"], "entity_count": 6,
             "collections": release["collections"], "audit": release["audit"],
             "previous_release": previous.get("previous_release", "") if previous.get("current_release") == release["_id"]
                                 else previous.get("current_release", ""), "published_at": release["published_at"]}
    assert_current_transition(previous, value)
    for namespace, pointer_id, candidate in candidates:
        assert_current_transition(db[namespace].find_one({"_id": pointer_id}) or {}, candidate)
    publish_current(db, "scienti_final_release_publications", "current", value)
    for namespace, pointer_id, candidate in candidates:
        publish_current(db, namespace, pointer_id, candidate)
    register_release(db, release)
    return value
